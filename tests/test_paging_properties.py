"""Paging properties, checked on a scratch store.

The resumption cursor is a keyset order on (datestamp, identifier). The hazard
it exists to prevent is the quiet one: with a non-unique cursor key a record can
be skipped, and the harvest still ends on an empty page, so the harvester
believes it finished. These tests hammer the boundary where the datestamp alone
is *not* unique - more records tied on one second than fit on a page.

`SAMPLE` holds three records with distinct timestamps; `tied` rewrites them all
to one value. Both are seeded into their own scratch databases, so nothing here
touches the shipped `sync_*` store.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

import support


pytestmark = pytest.mark.integration


def _seeded(database: str, dump: Path = support.SAMPLE) -> Iterator[support.Config]:
    cfg = support.object_config(database)
    if not support.basex_live(cfg):
        pytest.skip("BaseX is not answering")
    support.seed(cfg, dump, reset=True)
    try:
        yield cfg
    finally:
        support.drop(cfg)


@pytest.fixture(scope="module")
def store() -> Iterator[support.Config]:
    yield from _seeded("oai_provider_paging")


@pytest.fixture(scope="module")
def tied_store(tmp_path_factory) -> Iterator[support.Config]:
    """Every record given the same __lastModified, so the datestamp ties."""
    text = support.SAMPLE.read_text()
    stamps = re.findall(r'name="__lastModified">\s*<value>([^<]+)</value>', text)
    assert len(stamps) >= 2, f"expected several records, found {stamps}"
    for stamp in stamps[1:]:
        text = text.replace(stamp, stamps[0])
    dump = tmp_path_factory.mktemp("paging") / "tied.xml"
    dump.write_text(text)
    yield from _seeded("oai_provider_paging_tied", dump)


def _pages(cfg, verb: str = "ListIdentifiers"):
    """Every page of a harvest, as (token element, headers on that page)."""
    out = []
    token = ""
    for _ in range(100):
        if token:
            root = support.serve(cfg, ("verb", verb), ("resumptionToken", token))
        else:
            root = support.serve(cfg, ("verb", verb), ("metadataPrefix", "ria"))
        assert not support.errors(root), support.errors(root)
        el = root.find(f"{support.q(verb)}/{support.q('resumptionToken')}")
        assert el is not None, "every page carries a resumptionToken element"
        out.append((el, support.headers(root)))
        token = (el.text or "").strip()
        if not token:
            return out
    raise AssertionError("resumption loop did not terminate")


def _all_ids(cfg) -> list[str]:
    ids: list[str] = []
    for _, hdrs in _pages(cfg):
        ids += [h["identifier"] for h in hdrs]
    return ids


@pytest.mark.parametrize("page_size", [1, 2, 3, 10])
def test_every_record_is_delivered_exactly_once(store, page_size: int) -> None:
    expected = _all_ids(support.with_page_size(store, 1000))
    seen = _all_ids(support.with_page_size(store, page_size))
    assert len(seen) == len(set(seen)), f"a record was delivered twice: {seen}"
    assert sorted(seen) == sorted(expected), (seen, expected)


def test_the_cursor_increments_by_the_page_size(store) -> None:
    pages = _pages(support.with_page_size(store, 2))
    cursors = [int(el.get("cursor")) for el, _ in pages]
    assert cursors == [0, 2], cursors


def test_complete_list_size_is_the_whole_set_and_never_shrinks(store) -> None:
    expected = len(_all_ids(support.with_page_size(store, 1000)))
    pages = _pages(support.with_page_size(store, 2))
    sizes = [int(el.get("completeListSize")) for el, _ in pages]
    assert sizes == [expected] * len(sizes), sizes


def test_the_last_page_carries_an_empty_token(store) -> None:
    pages = _pages(store)
    assert (pages[-1][0].text or "").strip() == ""
    assert all((el.text or "").strip() for el, _ in pages[:-1])


def test_a_non_unique_datestamp_loses_nothing(tied_store) -> None:
    """The dangerous case: more records tied on one instant than fit on a page."""
    expected = len(_all_ids(support.with_page_size(tied_store, 1000)))
    assert expected >= 3
    seen = _all_ids(support.with_page_size(tied_store, 2))
    assert len(seen) == len(set(seen)), f"a peer was repeated: {seen}"
    assert len(seen) == expected, f"a peer was skipped: {seen} != {expected}"


def test_every_identifier_a_page_serves_is_gettable(store) -> None:
    """The two verbs have to agree on the identifier space: a header that cannot
    be fetched is a broken harvest."""
    for ident in _all_ids(store):
        root = support.serve(
            store,
            ("verb", "GetRecord"),
            ("identifier", ident),
            ("metadataPrefix", "ria"),
        )
        assert not support.errors(root), (ident, support.errors(root))

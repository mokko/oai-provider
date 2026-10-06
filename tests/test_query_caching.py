"""Rendered queries are cached, and the cache is invisible.

`QueryBuilder` re-rendered every template on every request: a file read plus a
lot of string splicing, per page. The provider is now built once per app and
the builder memoises, so a page pays for the render once. The property that
matters is that the cached text is *identical* to an uncached render - a cache
that changed the query would be a silent mapping change.

Pure Python: no BaseX.
"""

from __future__ import annotations

import pytest

from oai.config import Config
from oai.mapping import QueryBuilder


@pytest.fixture()
def builder(shipped_config: Config) -> QueryBuilder:
    return QueryBuilder(shipped_config.modules, shipped_config.timezone_offset)


def _ria(config: Config):
    return next(f for f in config.formats if f.prefix == "ria")


def _count_renders(builder: QueryBuilder, monkeypatch) -> dict:
    calls = {"n": 0}
    real = builder.render

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(builder, "render", counting)
    return calls


def test_a_repeated_page_query_renders_once(
    monkeypatch, builder, shipped_config: Config
) -> None:
    fmt = _ria(shipped_config)
    calls = _count_renders(builder, monkeypatch)
    first = builder.page_query(fmt)
    second = builder.page_query(fmt)
    assert first == second
    assert calls["n"] == 1, "the second call re-rendered"


def test_the_cached_text_equals_an_uncached_render(shipped_config: Config) -> None:
    fmt = _ria(shipped_config)
    cached = QueryBuilder(shipped_config.modules, shipped_config.timezone_offset)
    text = cached.page_query(fmt)
    fresh = QueryBuilder(shipped_config.modules, shipped_config.timezone_offset)
    assert text == fresh.render("page.xq.tmpl", fmt=fmt)


def test_each_format_gets_its_own_query(shipped_config: Config) -> None:
    """A format restricts the module set, so its query is genuinely different -
    the cache key is the prefix, not a shared slot."""
    b = QueryBuilder(shipped_config.modules, shipped_config.timezone_offset)
    by_prefix = {f.prefix: b.page_query(f) for f in shipped_config.formats}
    assert len(set(by_prefix.values())) == len(by_prefix)


def test_the_fingerprint_is_stable_and_cached(builder) -> None:
    first = builder.fingerprint()
    assert builder.fingerprint() == first
    fresh = QueryBuilder(builder.modules, builder.timezone_offset)
    assert fresh.fingerprint() == first


def test_ingest_queries_are_cached_too(builder, monkeypatch) -> None:
    calls = _count_renders(builder, monkeypatch)
    builder.module_ingest_query()
    builder.module_ingest_query()
    builder.module_counts_query()
    builder.module_counts_query()
    assert calls["n"] == 2, "one render per template expected"

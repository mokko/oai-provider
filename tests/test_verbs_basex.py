"""Integration tests for the six verbs against live BaseX."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path

import pytest

from oai.basex import BaseXClient
from oai.config import Config, ModuleConfig, SetRule
from oai.mapping import QueryBuilder
from oai.protocol import OAI_NS, Provider, q

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ria-dump.xml"
TEST_DB = "oai_provider_verbs"
OAI_ERROR = q("error")
OAI_HEADER = q("header")

pytestmark = pytest.mark.integration



def base_config() -> Config:
    cfg = Config.load(ROOT / "oai.toml")
    # One Object module over the sample, in a scratch database. The sample is
    # Object-only, so this is the same result set the old single-database path
    # served, and keeping the sample's group ids as sets keeps the set
    # behaviour testable.
    obj = ModuleConfig(
        name="Object",
        database=TEST_DB,
        identifier_prefix="spk-berlin.de:object-",
        sets=(
            # The KK owner reference: every real record carries it, unlike the
            # group ids the old hand-written fixture used.
            SetRule(
                spec="KK",
                label="Kupferstichkabinett, Staatliche Museen zu Berlin",
                xpath="moduleReference[@name='ObjOwnerRef']"
                "/moduleReferenceItem[@moduleItemId='112264']",
            ),
        ),
    )
    return dataclasses.replace(cfg, modules=(obj,))


def _client(cfg: Config) -> BaseXClient:
    return BaseXClient(
        cfg.basex.url, cfg.basex.user, cfg.basex.password, cfg.basex.timeout
    )


def _live(cfg: Config) -> bool:
    async def check() -> bool:
        async with _client(cfg) as bx:
            return await bx.ping()

    try:
        return asyncio.run(check())
    except Exception:
        return False


@pytest.fixture(scope="module")
def cfg() -> Config:
    c = base_config()
    if not _live(c):
        pytest.skip("BaseX not answering")
    return c


def first_identifier(cfg: Config) -> str:
    """An identifier the fixture actually holds, read from the repository.

    Derived rather than hard-coded: the fixture is now real records, whose ids
    are whatever the export says.
    """
    root = call(cfg, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    hdr = root.find(f"{q('ListIdentifiers')}/{q('header')}")
    assert hdr is not None, "no identifiers served"
    return hdr.findtext(q("identifier")) or ""


def seed(cfg: Config, dump: Path, reset: bool = True) -> None:
    """Ingest `dump` into the module databases.

    `reset=False` re-ingests into the same database, which is what the real
    ingest does: it is incremental and additive, so a record this dump does not
    mention is left alone.
    """
    async def go() -> None:
        builder = QueryBuilder(cfg.modules, cfg.timezone_offset)
        async with _client(cfg) as bx:
            for module in cfg.modules:
                if reset and await bx.database_exists(module.database):
                    await bx.drop_database(module.database)
                if not await bx.database_exists(module.database):
                    await bx.create_database(module.database)
                await bx.query(
                    builder.module_ingest_query(),
                    path=str(dump),
                    db=module.database,
                    moduleName=module.name,
                )

    asyncio.run(go())


def call(cfg: Config, *pairs: tuple[str, str]):
    async def go():
        async with _client(cfg) as bx:
            provider = Provider(cfg, bx)
            return await provider.handle(list(pairs))

    return asyncio.run(go())


def errors(root) -> list[tuple[str, str]]:
    return [(e.get("code", ""), e.text or "") for e in root.iter(OAI_ERROR)]


def headers(root) -> list[dict]:
    out = []
    for h in root.iter(OAI_HEADER):
        out.append(
            {
                "identifier": h.findtext(q("identifier"), ""),
                "datestamp": h.findtext(q("datestamp"), ""),
                "status": h.get("status", ""),
                "sets": [s.text for s in h.findall(q("setSpec"))],
            }
        )
    return out


# -- identification and static verbs ---------------------------------------


def test_identify(cfg: Config) -> None:
    root = call(cfg, ("verb", "Identify"))
    ident = root.find(q("Identify"))
    assert ident is not None
    assert ident.findtext(q("protocolVersion")) == "2.0"
    assert ident.findtext(q("deletedRecord")) == "no"
    assert ident.findtext(q("granularity")) == "YYYY-MM-DDThh:mm:ssZ"
    assert root.findtext(f"{q('request')}") == cfg.identity.base_url


def test_list_metadata_formats(cfg: Config) -> None:
    root = call(cfg, ("verb", "ListMetadataFormats"))
    prefixes = [e.text for e in root.iter(q("metadataPrefix"))]
    # the stored payload, the Dublin Core view derived from it, and LIDO from
    # the stylesheet
    assert prefixes == ["ria", "oai_dc", "lido"]


def test_list_metadata_formats_unknown_identifier(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    root = call(
        cfg, ("verb", "ListMetadataFormats"), ("identifier", "does-not-exist")
    )
    assert errors(root) == [("idDoesNotExist", "unknown identifier: does-not-exist")]


def test_list_sets_uses_config_labels(cfg: Config) -> None:
    root = call(cfg, ("verb", "ListSets"))
    sets = {
        s.findtext(q("setSpec")): s.findtext(q("setName"))
        for s in root.iter(q("set"))
    }
    assert sets == {"KK": "Kupferstichkabinett, Staatliche Museen zu Berlin"}


# -- GetRecord --------------------------------------------------------------


def test_get_record_returns_header_and_payload(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    ident = first_identifier(cfg)
    root = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", ident),
        ("metadataPrefix", "ria"),
    )
    hdrs = headers(root)
    assert hdrs[0]["identifier"] == ident
    # every real record in this collection is held by the Kupferstichkabinett
    assert hdrs[0]["sets"] == ["KK"]
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    assert md is not None
    payload = list(md)[0]
    # the store is namespace-stripped; the wire is not. A namespace-free payload
    # is not admissible under <metadata>'s namespace="##other" wildcard, so the
    # RIA namespace is rebuilt at serve time (local:zetcom), as for the XSLT path.
    # And the payload is the whole record document (application root), not the
    # bare `moduleItem`, which is a LOCAL element in Zetcom's schema.
    z = "{http://www.zetcom.com/ria/ws/module}"
    assert payload.tag == f"{z}application"
    item = payload.find(f"{z}modules/{z}module/{z}moduleItem")
    assert item is not None
    assert item.get("id") == ident.rsplit("-", 1)[-1]


def test_records_are_wrapped_as_the_schema_requires(cfg: Config) -> None:
    """GetRecordType and ListRecordsType each require a <record> element, so a
    bare <header>/<metadata> under the verb is schema-invalid and a conformant
    harvester cannot parse it. ListIdentifiers is the exception - its type takes
    headers directly - which is why the builder branches rather than duplicating."""
    seed(cfg, SAMPLE)
    gr = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", first_identifier(cfg)),
        ("metadataPrefix", "ria"),
    )
    assert gr.find(f"{q('GetRecord')}/{q('record')}") is not None
    assert gr.find(f"{q('GetRecord')}/{q('header')}") is None

    lr = call(cfg, ("verb", "ListRecords"), ("metadataPrefix", "ria"))
    assert lr.findall(f"{q('ListRecords')}/{q('record')}"), "no <record> elements"
    assert lr.find(f"{q('ListRecords')}/{q('header')}") is None

    li = call(cfg, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    assert li.findall(f"{q('ListIdentifiers')}/{q('header')}"), "headers go bare here"


def test_get_record_unknown_identifier(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    root = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", "nope"),
        ("metadataPrefix", "ria"),
    )
    assert errors(root)[0][0] == "idDoesNotExist"


def test_get_record_unsupported_format(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    root = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:object-1001"),
        ("metadataPrefix", "mods"),
    )
    assert errors(root)[0][0] == "cannotDisseminateFormat"


# -- listing ----------------------------------------------------------------


def test_list_identifiers_no_records_match(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("from", "2030-01-01"),
    )
    assert errors(root)[0][0] == "noRecordsMatch"


def test_unknown_set_is_no_records_match(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("set", "not-a-set"),
    )
    assert errors(root)[0][0] == "noRecordsMatch"


def test_bad_datestamp_is_bad_argument(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("from", "19-09-2026"),
    )
    assert errors(root)[0][0] == "badArgument"


def test_day_granularity_until_covers_the_whole_day(cfg: Config) -> None:
    """`until=<a day>` must include records *during* that day, not just at its
    start. Derived from the fixture: the last datestamp's day is used as `until`,
    so a plain `le` comparison that ignored the time would have dropped it."""
    seed(cfg, SAMPLE)
    every = call(cfg, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    stamps = [
        h.findtext(q("datestamp"))
        for h in every.iter(q("header"))
        if h.findtext(q("datestamp"))
    ]
    assert stamps, "no datestamps served"
    day = max(stamps)[:10]
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("until", day),
    )
    ids = {h["identifier"] for h in headers(root)}
    assert len(ids) == len(stamps), (day, sorted(ids), sorted(stamps))


def test_bad_verb_and_token_exclusivity(cfg: Config) -> None:
    root = call(cfg, ("verb", "Nope"))
    assert errors(root)[0][0] == "badVerb"
    root = call(
        cfg, ("verb", "ListRecords"), ("resumptionToken", "x"), ("set", "mimo")
    )
    assert errors(root)[0][0] == "badArgument"


def test_forged_token_is_bad_resumption_token(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    root = call(
        cfg, ("verb", "ListIdentifiers"), ("resumptionToken", "bm90LWEtdG9rZW4")
    )
    assert errors(root)[0][0] == "badResumptionToken"


# -- paging -----------------------------------------------------------------


def all_pages(cfg: Config, verb: str) -> list[dict]:
    """Walk the whole result set the way a harvester does and assert the
    token discipline: each page either has a token or ends with an empty one."""
    seen: list[dict] = []
    token = ""
    guard = 0
    while True:
        guard += 1
        assert guard < 50, "resumption loop did not terminate"
        if token:
            root = call(cfg, ("verb", verb), ("resumptionToken", token))
        else:
            root = call(cfg, ("verb", verb), ("metadataPrefix", "ria"))
        assert not errors(root), errors(root)
        seen += headers(root)
        node = root.find(f"{q(verb)}/{q('resumptionToken')}")
        assert node is not None, "every page must carry a resumptionToken element"
        token = (node.text or "").strip()
        if not token:
            break
    return seen


def test_paging_delivers_every_record_exactly_once(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    small = dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=1)
    )
    seen = all_pages(small, "ListIdentifiers")
    ids = [h["identifier"] for h in seen]
    assert len(ids) == 3, f"expected 3 records, got {ids}"
    assert len(set(ids)) == 3, f"a record was delivered twice: {ids}"


def test_paging_survives_records_sharing_a_datestamp(cfg: Config, tmp_path) -> None:
    """The failure the keyset cursor exists to prevent: with a non-unique
    cursor key a peer is skipped, and the harvest still ends as if complete.
    """
    text = SAMPLE.read_text()
    # give every record the first record's __lastModified, whatever they are:
    # derived from the file so it keeps working when the fixture is replaced
    stamps = re.findall(r'name="__lastModified">\s*<value>([^<]+)</value>', text)
    assert len(stamps) >= 2, f"expected several records, found {stamps}"
    for stamp in stamps[1:]:
        text = text.replace(stamp, stamps[0])
    tied = tmp_path / "tied.xml"
    tied.write_text(text)

    seed(cfg, tied, "dump-ties")
    small = dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=1)
    )
    seen = all_pages(small, "ListIdentifiers")
    ids = [h["identifier"] for h in seen]
    assert len(set(ids)) == 3, f"expected 3 distinct records, got {ids}"


def test_complete_list_size_and_cursor(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    small = dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=1)
    )
    root = call(small, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    token_el = root.find(f"{q('ListIdentifiers')}/{q('resumptionToken')}")
    # completeListSize is the size of the whole pinned result set, not the page
    assert token_el.get("completeListSize") == "3"
    # cursor is "the number of elements of the complete list thus far returned"
    # - the count BEFORE the page, so the first page is 0, not the page size
    assert token_el.get("cursor") == "0"

    second = call(
        small, ("verb", "ListIdentifiers"), ("resumptionToken", token_el.text)
    )
    second_el = second.find(f"{q('ListIdentifiers')}/{q('resumptionToken')}")
    assert second_el.get("cursor") == "1"


def test_paging_with_payloads(cfg: Config) -> None:
    seed(cfg, SAMPLE)
    small = dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=1)
    )
    root = call(small, ("verb", "ListRecords"), ("metadataPrefix", "ria"))
    assert len(root.findall(f"{q('ListRecords')}/{q('record')}/{q('metadata')}")) == 1
    assert len(headers(root)) == 1


class _Counting(BaseXClient):
    """Counts HTTP round trips, to prove a page does no redundant work.

    The whole point of taking the total from the first page's own scan is that
    a harvest no longer pays for a separate COUNT query per page.
    """

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.posts = 0

    async def _post(self, query, variables):  # type: ignore[override]
        self.posts += 1
        return await super()._post(query, variables)


def _call_counting(cfg: Config, *pairs: tuple[str, str]):
    async def go():
        async with _Counting(
            cfg.basex.url, cfg.basex.user, cfg.basex.password, cfg.basex.timeout
        ) as bx:  # type: ignore[assignment]
            provider = Provider(cfg, bx)
            bx.posts = 0
            root = await provider.handle(list(pairs))
            return bx.posts, root

    return asyncio.run(go())


def test_resumption_costs_one_query_per_page(cfg: Config) -> None:
    """completeListSize comes from the first page's scan, and a resume carries
    it in the token - so neither the first page nor a resume issues a second,
    separate COUNT query. Two queries per page is the regression this locks
    out."""
    seed(cfg, SAMPLE)
    small = dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=2)
    )

    trips, root = _call_counting(
        small, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria")
    )
    assert trips == 1, f"first page should be one query, made {trips}"
    tok = root.find(f"{q('ListIdentifiers')}/{q('resumptionToken')}")
    assert tok is not None and tok.text, "expected a token with another page"
    assert tok.get("completeListSize") == "3"

    trips2, root2 = _call_counting(
        small, ("verb", "ListIdentifiers"), ("resumptionToken", tok.text)
    )
    assert trips2 == 1, f"resume should be one query, made {trips2}"
    # the total was carried in the token, not recomputed
    final = root2.find(f"{q('ListIdentifiers')}/{q('resumptionToken')}")
    assert final is not None and final.get("completeListSize") == "3"

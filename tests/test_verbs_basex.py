"""Integration tests for the six verbs against live BaseX."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path

import pytest

from oai.basex import BaseXClient
from oai.config import ENVELOPE_NS, Config
from oai.mapping import QueryBuilder
from oai.protocol import OAI_NS, Provider, q

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ria-dump.xml"
TEST_DB = "oai_provider_verbs"
ENV = f"{{{ENVELOPE_NS}}}"

OAI_ERROR = q("error")
OAI_HEADER = q("header")


def base_config() -> Config:
    cfg = Config.load(ROOT / "oai.toml")
    # These tests exercise the **enveloped** path: one database, env:record
    # documents. The real oai.toml is module mode with deletedRecord="no";
    # [[modules]] is cleared and the persistent policy pinned back on here on
    # purpose, because reconcile can tombstone in this mode. The module path
    # has its own test.
    return dataclasses.replace(
        cfg,
        basex=dataclasses.replace(cfg.basex, database=TEST_DB),
        modules=(),
        identity=dataclasses.replace(cfg.identity, deleted_record="persistent"),
    )


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


def seed(
    cfg: Config, dump: Path, dump_id: str, reset: bool = True
) -> None:
    """Ingest `dump`, then reconcile so deletions happen.

    `reset=False` for a second dump into the same database, which is what the
    real ingest does - it is incremental. Dropping the database first would
    wipe the records the next dump is supposed to find missing, and the
    tombstone tests would pass against a database that never had them.
    """
    variables = {
        "path": str(dump),
        "db": cfg.basex.database,
        "idPrefix": cfg.mapping.identifier_prefix,
        "tzOffset": cfg.mapping.timezone_offset,
        "dumpId": dump_id,
        "now": "2026-09-29T12:00:00Z",
        "policy": cfg.identity.deleted_record,
    }

    async def go() -> None:
        builder = QueryBuilder(cfg.mapping)
        async with _client(cfg) as bx:
            if reset:
                await bx.command(f"DROP DB {cfg.basex.database}")
                await bx.command(f"CREATE DB {cfg.basex.database}")
            await bx.query(builder.ingest_query(), **variables)
            await bx.query(builder.reconcile_query(), **variables)

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
    assert ident.findtext(q("deletedRecord")) == "persistent"
    assert ident.findtext(q("granularity")) == "YYYY-MM-DDThh:mm:ssZ"
    assert root.findtext(f"{q('request')}") == cfg.identity.base_url


def test_list_metadata_formats(cfg: Config) -> None:
    root = call(cfg, ("verb", "ListMetadataFormats"))
    prefixes = [e.text for e in root.iter(q("metadataPrefix"))]
    # the stored payload, and the Dublin Core view derived from it
    assert prefixes == ["ria", "oai_dc"]


def test_list_metadata_formats_unknown_identifier(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
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
    assert sets == {"mimo": "Musikinstrumente", "78": "Schellackplatten"}


# -- GetRecord --------------------------------------------------------------


def test_get_record_returns_header_and_payload(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
    root = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:EM-objId-1001"),
        ("metadataPrefix", "ria"),
    )
    hdrs = headers(root)
    assert hdrs[0]["identifier"] == "spk-berlin.de:EM-objId-1001"
    assert hdrs[0]["sets"] == ["mimo"]
    md = root.find(f"{q('GetRecord')}/{q('metadata')}")
    assert md is not None
    payload = list(md)[0]
    assert payload.tag == "{http://www.zetcom.com/ria/ws/module}moduleItem"
    assert payload.get("id") == "1001"


def test_get_record_unknown_identifier(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
    root = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", "nope"),
        ("metadataPrefix", "ria"),
    )
    assert errors(root)[0][0] == "idDoesNotExist"


def test_get_record_unsupported_format(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
    root = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:EM-objId-1001"),
        ("metadataPrefix", "mods"),
    )
    assert errors(root)[0][0] == "cannotDisseminateFormat"


def test_get_record_on_a_deleted_record_has_no_metadata(cfg: Config, tmp_path) -> None:
    seed(cfg, SAMPLE, "dump-a")
    seed(cfg, reduced_dump(tmp_path), "dump-b", reset=False)
    root = call(
        cfg,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:EM-objId-1002"),
        ("metadataPrefix", "ria"),
    )
    assert headers(root)[0]["status"] == "deleted"
    assert root.find(f"{q('GetRecord')}/{q('metadata')}") is None


def reduced_dump(tmp_path: Path) -> Path:
    text = SAMPLE.read_text()
    partial = re.sub(
        r"\s*<moduleItem[^>]*id=\"1002\".*?</moduleItem>", "", text, flags=re.S
    )
    out = tmp_path / "reduced.xml"
    out.write_text(partial)
    return out


# -- listing ----------------------------------------------------------------


def test_list_identifiers_includes_deleted_with_status(cfg: Config, tmp_path) -> None:
    seed(cfg, SAMPLE, "dump-a")
    seed(cfg, reduced_dump(tmp_path), "dump-b", reset=False)
    root = call(cfg, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    by_id = {h["identifier"]: h for h in headers(root)}
    assert len(by_id) == 3
    assert by_id["spk-berlin.de:EM-objId-1002"]["status"] == "deleted"
    assert by_id["spk-berlin.de:EM-objId-1001"]["status"] == ""


def test_list_records_omits_metadata_for_deleted(cfg: Config, tmp_path) -> None:
    seed(cfg, SAMPLE, "dump-a")
    seed(cfg, reduced_dump(tmp_path), "dump-b", reset=False)
    root = call(cfg, ("verb", "ListRecords"), ("metadataPrefix", "ria"))
    assert len(root.findall(f"{q('ListRecords')}/{q('metadata')}")) == 2
    assert len(headers(root)) == 3


def test_list_identifiers_no_records_match(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("from", "2030-01-01"),
    )
    assert errors(root)[0][0] == "noRecordsMatch"


def test_unknown_set_is_no_records_match(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("set", "not-a-set"),
    )
    assert errors(root)[0][0] == "noRecordsMatch"


def test_bad_datestamp_is_bad_argument(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("from", "19-09-2026"),
    )
    assert errors(root)[0][0] == "badArgument"


def test_day_granularity_until_covers_the_whole_day(cfg: Config) -> None:
    """until=2026-09-19 must include a record at 06:15 on the 19th."""
    seed(cfg, SAMPLE, "dump-a")
    root = call(
        cfg,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("until", "2026-09-19"),
    )
    ids = {h["identifier"] for h in headers(root)}
    assert "spk-berlin.de:EM-objId-1001" in ids


def test_bad_verb_and_token_exclusivity(cfg: Config) -> None:
    root = call(cfg, ("verb", "Nope"))
    assert errors(root)[0][0] == "badVerb"
    root = call(
        cfg, ("verb", "ListRecords"), ("resumptionToken", "x"), ("set", "mimo")
    )
    assert errors(root)[0][0] == "badArgument"


def test_forged_token_is_bad_resumption_token(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
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
    seed(cfg, SAMPLE, "dump-a")
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
    # give 1002 and 1003 the same __lastModified as 1001
    text = text.replace("2026-08-01 09:00:00.0", "2026-09-19 08:15:00.0")
    text = text.replace("2026-07-02 12:30:45.5", "2026-09-19 08:15:00.0")
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
    seed(cfg, SAMPLE, "dump-a")
    small = dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=1)
    )
    root = call(small, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    token_el = root.find(f"{q('ListIdentifiers')}/{q('resumptionToken')}")
    # completeListSize is the size of the whole pinned result set, not the page
    assert token_el.get("completeListSize") == "3"
    assert token_el.get("cursor") == "1"


def test_paging_with_payloads(cfg: Config) -> None:
    seed(cfg, SAMPLE, "dump-a")
    small = dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=1)
    )
    root = call(small, ("verb", "ListRecords"), ("metadataPrefix", "ria"))
    assert len(root.findall(f"{q('ListRecords')}/{q('metadata')}")) == 1
    assert len(headers(root)) == 1

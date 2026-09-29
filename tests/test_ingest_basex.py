"""Integration tests against a live BaseX.

Skipped unless BaseX answers on the configured REST port. These are the tests
that would have caught the four traps found while building the ingest.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path

import pytest

from oai.basex import BaseXClient
from oai.config import ENVELOPE_NS, Config, Mapping, SetRule
from oai.mapping import QueryBuilder

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ria-dump.xml"
ENV = f"{{{ENVELOPE_NS}}}"

TEST_DB = "oai_provider_test"
TEST_DB_STRIPPED = "oai_provider_test_stripped"


def base_config() -> Config:
    cfg = Config.load(ROOT / "oai.toml")
    return dataclasses.replace(
        cfg,
        basex=dataclasses.replace(cfg.basex, database=TEST_DB),
    )


async def _reset(bx: BaseXClient, db: str) -> None:
    await bx.command(f"DROP DB {db}")
    await bx.command(f"CREATE DB {db}")


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
        pytest.skip("BaseX not answering on the configured REST port")
    return c


def ingest(cfg: Config, path: Path, **overrides) -> None:
    variables = {
        "path": str(path),
        "db": cfg.basex.database,
        "idPrefix": cfg.mapping.identifier_prefix,
        "tzOffset": cfg.mapping.timezone_offset,
        "dumpId": overrides.pop("dumpId", "test-dump-1"),
        "now": "2026-09-29T12:00:00Z",
        "policy": cfg.identity.deleted_record,
        **overrides,
    }

    async def go() -> None:
        builder = QueryBuilder(cfg.mapping)
        async with _client(cfg) as bx:
            await _reset(bx, cfg.basex.database)
            await bx.query(builder.validate_query(), **variables)
            await bx.query(builder.ingest_query(), **variables)

    asyncio.run(go())


def headers(cfg: Config) -> dict[str, dict[str, str]]:
    q = (
        f'declare namespace env = "{ENVELOPE_NS}";\n'
        f"for $r in collection({cfg.basex.database!r})/env:record\n"
        "return <h id='{$r/@env:identifier}' ds='{$r/@env:datestamp}' "
        "status='{string($r/@env:status)}' "
        "sets='{string-join($r/env:set, \",\")}'/>"
    )

    async def go() -> dict[str, dict[str, str]]:
        async with _client(cfg) as bx:
            nodes = await bx.query_nodes(q)
        return {
            el.get("id", ""): {
                "ds": el.get("ds", ""),
                "status": el.get("status", ""),
                "sets": el.get("sets", ""),
            }
            for el in nodes
        }

    return asyncio.run(go())


def test_ingest_produces_one_envelope_per_record(cfg: Config) -> None:
    ingest(cfg, SAMPLE)
    got = headers(cfg)
    assert set(got) == {
        "spk-berlin.de:EM-objId-1001",
        "spk-berlin.de:EM-objId-1002",
        "spk-berlin.de:EM-objId-1003",
    }


def test_datestamp_is_shifted_to_utc_not_relabelled(cfg: Config) -> None:
    ingest(cfg, SAMPLE)
    got = headers(cfg)
    # 08:15 local at +02:00 is 06:15Z. Appending Z to the local time would
    # have produced 08:15Z and silently shifted every incremental harvest.
    assert got["spk-berlin.de:EM-objId-1001"]["ds"] == "2026-09-19T06:15:00Z"
    assert got["spk-berlin.de:EM-objId-1002"]["ds"] == "2026-08-01T07:00:00Z"


def test_sets_come_from_org_unit_and_object_groups(cfg: Config) -> None:
    ingest(cfg, SAMPLE)
    got = headers(cfg)
    assert got["spk-berlin.de:EM-objId-1001"]["sets"] == (
        "EM-Süd- und Südostasien,group-6054"
    )
    # 1003 has no __orgUnit and no group reference: sets must be empty, not
    # an empty string masquerading as a set.
    assert got["spk-berlin.de:EM-objId-1003"]["sets"] == ""


def test_payload_keeps_its_namespace(cfg: Config) -> None:
    ingest(cfg, SAMPLE)

    async def go() -> str:
        async with _client(cfg) as bx:
            return await bx.query(
                f'declare namespace m = "http://www.zetcom.com/ria/ws/module";\n'
                f"string-join(distinct-values("
                f"collection({cfg.basex.database!r})//m:moduleItem/namespace-uri()"
                f"), ',')"
            )

    assert asyncio.run(go()).strip().startswith("http://www.zetcom.com")


def test_reconcile_tombstones_records_missing_from_the_dump(cfg: Config, tmp_path) -> None:
    """The work setup has no delete signal. Ingest a 3-record dump, then a
    2-record dump; the vanished record must not stay live."""
    ingest(cfg, SAMPLE, dumpId="dump-a")
    assert len(headers(cfg)) == 3

    text = SAMPLE.read_text()
    # drop the 1002 moduleItem entirely
    partial = re.sub(
        r"\s*<moduleItem[^>]*id=\"1002\".*?</moduleItem>", "", text, flags=re.S
    )
    assert partial.count("<moduleItem") == 2
    reduced = tmp_path / "reduced.xml"
    reduced.write_text(partial)

    async def go() -> None:
        builder = QueryBuilder(cfg.mapping)
        variables = {
            "path": str(reduced),
            "db": cfg.basex.database,
            "idPrefix": cfg.mapping.identifier_prefix,
            "tzOffset": cfg.mapping.timezone_offset,
            "dumpId": "dump-b",
            "now": "2026-09-29T12:00:00Z",
            "policy": cfg.identity.deleted_record,
        }
        async with _client(cfg) as bx:
            await bx.query(builder.ingest_query(), **variables)
            await bx.query(builder.reconcile_query(), **variables)

    asyncio.run(go())

    got = headers(cfg)
    assert len(got) == 3, "tombstone must be kept, not dropped, for persistent"
    gone = got["spk-berlin.de:EM-objId-1002"]
    assert gone["status"] == "deleted"
    assert gone["ds"] == "2026-09-29T12:00:00Z"
    assert got["spk-berlin.de:EM-objId-1001"]["status"] == ""


def test_transient_policy_removes_instead(cfg: Config, tmp_path) -> None:
    cfg = dataclasses.replace(
        cfg,
        identity=dataclasses.replace(cfg.identity, deleted_record="transient"),
    )
    ingest(cfg, SAMPLE, dumpId="dump-a")

    text = SAMPLE.read_text()
    partial = re.sub(
        r"\s*<moduleItem[^>]*id=\"1002\".*?</moduleItem>", "", text, flags=re.S
    )
    reduced = tmp_path / "reduced.xml"
    reduced.write_text(partial)

    async def go() -> None:
        builder = QueryBuilder(cfg.mapping)
        variables = {
            "path": str(reduced),
            "db": cfg.basex.database,
            "idPrefix": cfg.mapping.identifier_prefix,
            "tzOffset": cfg.mapping.timezone_offset,
            "dumpId": "dump-b",
            "now": "2026-09-29T12:00:00Z",
            "policy": "transient",
        }
        async with _client(cfg) as bx:
            await bx.query(builder.ingest_query(), **variables)
            await bx.query(builder.reconcile_query(), **variables)

    asyncio.run(go())
    got = headers(cfg)
    assert set(got) == {
        "spk-berlin.de:EM-objId-1001",
        "spk-berlin.de:EM-objId-1003",
    }


def test_records_without_a_seen_stamp_are_treated_as_stale(
    cfg: Config, tmp_path
) -> None:
    """Adopting an existing database: records stored before the seen stamp
    existed must still be reconciled.

    @seen ne $dumpId looks right and is not: a missing attribute yields the
    empty sequence, which is falsy, so such records would be invisible to
    reconcile forever and would keep serving as live.
    """
    ingest(cfg, SAMPLE, dumpId="dump-a")

    async def strip_stamps() -> None:
        async with _client(cfg) as bx:
            await bx.query(
                f'declare namespace env = "{ENVELOPE_NS}";\n'
                f"for $r in collection({cfg.basex.database!r})/env:record\n"
                "return delete node $r/@env:seen"
            )

    asyncio.run(strip_stamps())

    async def reconcile() -> None:
        builder = QueryBuilder(cfg.mapping)
        variables = {
            "path": str(SAMPLE),
            "db": cfg.basex.database,
            "idPrefix": cfg.mapping.identifier_prefix,
            "tzOffset": cfg.mapping.timezone_offset,
            "dumpId": "dump-b",
            "now": "2026-09-29T12:00:00Z",
            "policy": "persistent",
        }
        async with _client(cfg) as bx:
            await bx.query(builder.reconcile_query(), **variables)

    asyncio.run(reconcile())

    got = headers(cfg)
    assert len(got) == 3
    assert all(h["status"] == "deleted" for h in got.values()), got


def stale_count(cfg: Config, dump_id: str) -> int:
    async def go() -> int:
        async with _client(cfg) as bx:
            return int(
                (
                    await bx.query(
                        QueryBuilder(cfg.mapping).stale_query(),
                        db=cfg.basex.database,
                        dumpId=dump_id,
                    )
                ).strip()
                or 0
            )

    return asyncio.run(go())


def test_stale_count_reports_what_reconcile_will_do(cfg: Config) -> None:
    """A document-count delta cannot report this: under "persistent" a
    tombstone keeps its document, so the delta is always 0."""
    ingest(cfg, SAMPLE, dumpId="dump-a")
    assert stale_count(cfg, "dump-a") == 0
    assert stale_count(cfg, "dump-b") == 3


def test_stripped_dump_behaves_identically(cfg: Config, tmp_path) -> None:
    """Namespace stripping is not a fork in the road: same code, empty
    prefix table, unprefixed XPaths, same headers."""
    stripped_text = re.sub(
        r'\s+xmlns(?::[\w.-]+)?="[^"]*"', "", SAMPLE.read_text()
    )
    stripped = tmp_path / "stripped.xml"
    stripped.write_text(stripped_text)

    mapping = Mapping(
        records="/application/modules/module[@name='Object']/moduleItem",
        identifier="@id",
        identifier_prefix=cfg.mapping.identifier_prefix,
        datestamp="systemField[@name='__lastModified']/value",
        timezone_offset=cfg.mapping.timezone_offset,
        namespaces={},
        sets=(
            SetRule("orgUnit", "systemField[@name='__orgUnit']/value"),
            SetRule(
                "objectGroup",
                "moduleReference[@name='ObjObjectGroupsRef']"
                "/moduleReferenceItem/@moduleItemId",
                spec="group-{value}",
            ),
        ),
    )
    s_cfg = dataclasses.replace(
        cfg,
        mapping=mapping,
        basex=dataclasses.replace(cfg.basex, database=TEST_DB_STRIPPED),
    )

    ingest(s_cfg, stripped)
    got = headers(s_cfg)
    assert set(got) == {
        "spk-berlin.de:EM-objId-1001",
        "spk-berlin.de:EM-objId-1002",
        "spk-berlin.de:EM-objId-1003",
    }
    assert got["spk-berlin.de:EM-objId-1001"]["ds"] == "2026-09-19T06:15:00Z"
    assert got["spk-berlin.de:EM-objId-1001"]["sets"] == (
        "EM-Süd- und Südostasien,group-6054"
    )

"""Helpers shared by the test modules.

Kept out of `conftest.py` so a test module can `import support` and use the
helpers directly; `conftest.py` owns only the fixtures. Nothing here touches the
shipped `sync_*` databases - integration tests seed their own scratch database,
which is what makes them hermetic and fast.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

from oai.basex import BaseXClient
from oai.config import Config, ModuleConfig, SetRule
from oai.mapping import QueryBuilder
from oai.protocol import Provider, q

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ria-dump.xml"

OAI_ERROR = q("error")
OAI_HEADER = q("header")

# The Kupferstichkabinett owner reference. Every record in the real export and
# in the sample carries it, which is what makes it usable as a set.
KK = SetRule(
    spec="KK",
    label="Kupferstichkabinett, Staatliche Museen zu Berlin",
    xpath="moduleReference[@name='ObjOwnerRef']/moduleReferenceItem[@moduleItemId='112264']",
)


def run(coro):
    """Drive a coroutine from a synchronous test."""
    return asyncio.run(coro)


def client_for(cfg: Config) -> BaseXClient:
    return BaseXClient(
        cfg.basex.url, cfg.basex.user, cfg.basex.password, cfg.basex.timeout
    )


def basex_live(cfg: Config) -> bool:
    """True when BaseX answers a trivial query."""

    async def check() -> bool:
        async with client_for(cfg) as bx:
            return await bx.ping()

    try:
        return run(check())
    except Exception:
        return False


def object_config(database: str, *, page_size: int | None = None) -> Config:
    """The shipped config with its modules replaced by one Object module.

    The sample is Object-only, so this is the same result set the old
    single-database path served, in a scratch database - and it keeps the set
    behaviour testable.
    """
    cfg = Config.load(ROOT / "oai.toml")
    obj = ModuleConfig(
        name="Object",
        database=database,
        identifier_prefix="spk-berlin.de:object-",
        sets=(KK,),
    )
    cfg = dataclasses.replace(cfg, modules=(obj,))
    if page_size is not None:
        cfg = dataclasses.replace(
            cfg, protocol=dataclasses.replace(cfg.protocol, page_size=page_size)
        )
    return cfg


def with_page_size(cfg: Config, page_size: int) -> Config:
    return dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=page_size)
    )


def seed(cfg: Config, dump: Path = SAMPLE, *, reset: bool = True) -> None:
    """Ingest `dump` into each configured module's database.

    `reset=False` re-ingests into the same database, which is what the real
    ingest does: it is incremental and additive, so a record this dump does not
    mention is left alone.
    """

    async def go() -> None:
        builder = QueryBuilder(cfg.modules, cfg.timezone_offset)
        async with client_for(cfg) as bx:
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

    run(go())


def drop(cfg: Config) -> None:
    async def go() -> None:
        async with client_for(cfg) as bx:
            for module in cfg.modules:
                if await bx.database_exists(module.database):
                    await bx.drop_database(module.database)

    run(go())


def serve(cfg: Config, *pairs: tuple[str, str]):
    """Run a request through the Provider and return the response root."""

    async def go():
        async with client_for(cfg) as bx:
            return await Provider(cfg, bx).handle(list(pairs))

    return run(go())


def errors(root) -> list[tuple[str, str]]:
    return [(e.get("code", ""), e.text or "") for e in root.iter(OAI_ERROR)]


def error_codes(root) -> list[str]:
    return [code for code, _ in errors(root)]


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


def identifiers(root) -> list[str]:
    return [h["identifier"] for h in headers(root)]


def walk(cfg: Config, verb: str, *, guard: int = 100) -> list[dict]:
    """Harvest every page the way a harvester does, asserting the discipline.

    Each page must carry a resumptionToken; the last one is empty. Returns the
    headers seen across every page, so a caller can check for loss and
    duplication.
    """
    seen: list[dict] = []
    token = ""
    steps = 0
    while True:
        steps += 1
        assert steps < guard, "resumption loop did not terminate"
        if token:
            root = serve(cfg, ("verb", verb), ("resumptionToken", token))
        else:
            root = serve(cfg, ("verb", verb), ("metadataPrefix", "ria"))
        assert not errors(root), errors(root)
        seen += headers(root)
        node = root.find(f"{q(verb)}/{q('resumptionToken')}")
        assert node is not None, "every page must carry a resumptionToken element"
        token = (node.text or "").strip()
        if not token:
            break
    return seen

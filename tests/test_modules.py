"""Module-per-database ingest: the colleague's `sync_<Type>` layout.

Unit tests need no BaseX; the integration tests are skipped unless one answers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from oai.basex import BaseXClient
from oai.config import Config, ConfigError
from oai.mapping import QueryBuilder

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ria-dump.xml"
TEST_DB = "oai_module_check"


@pytest.fixture()
def config() -> Config:
    return Config.load(ROOT / "oai.toml")


# -- config ---------------------------------------------------------------


def test_modules_come_from_the_config(config: Config) -> None:
    assert [m.name for m in config.modules] == ["Object", "Person", "Multimedia"]
    assert [m.database for m in config.modules] == [
        "sync_Object",
        "sync_Person",
        "sync_Multimedia",
    ]
    assert [m.identifier_prefix for m in config.modules] == [
        "spk-berlin.de:EM-object-",
        "spk-berlin.de:EM-person-",
        "spk-berlin.de:EM-asset-",
    ]


def _with_modules(text: str, block: str) -> str:
    """The real config with its [[modules]] block replaced."""
    start = text.index("\n[[modules]]\n")
    return text[: start + 1] + block


def test_two_modules_may_not_share_a_database(tmp_path) -> None:
    text = (ROOT / "oai.toml").read_text()
    bad = tmp_path / "bad.toml"
    bad.write_text(
        _with_modules(
            text,
            '[[modules]]\nname = "Object"\ndatabase = "same"\n'
            '[[modules]]\nname = "Person"\ndatabase = "same"\n',
        )
    )
    with pytest.raises(ConfigError, match="share a database"):
        Config.load(bad)


def test_a_module_name_may_not_repeat(tmp_path) -> None:
    text = (ROOT / "oai.toml").read_text()
    bad = tmp_path / "bad.toml"
    bad.write_text(
        _with_modules(
            text,
            '[[modules]]\nname = "Object"\ndatabase = "a"\n'
            '[[modules]]\nname = "Object"\ndatabase = "b"\n',
        )
    )
    with pytest.raises(ConfigError, match="duplicate module name"):
        Config.load(bad)


def test_a_module_without_a_database_is_rejected(tmp_path) -> None:
    text = (ROOT / "oai.toml").read_text()
    bad = tmp_path / "bad.toml"
    bad.write_text(_with_modules(text, '[[modules]]\nname = "Object"\n'))
    with pytest.raises(ConfigError):
        Config.load(bad)


# -- the generated queries ------------------------------------------------


def test_module_ingest_strips_namespaces(config: Config) -> None:
    """The one deliberate exception to "payload verbatim": the tree is rebuilt
    with local-name(), so element and attribute names survive and namespace
    URIs do not."""
    q = QueryBuilder(config.mapping).module_ingest_query()
    assert "local:strip" in q
    assert "element { local-name($n) }" in q
    assert "attribute { local-name($a) }" in q
    # the module is located by a wildcard prefix, so the same query serves the
    # source whether or not it still carries its namespace
    assert "<module name=" not in q  # no literal path that assumes a dialect
    assert "/*:module[@name = $moduleName]" in q


def test_module_ingest_writes_the_colleague_path(config: Config) -> None:
    """Records must land where `collection('sync_X')/application/modules/
    module[@name='X']/moduleItem` finds them."""
    q = QueryBuilder(config.mapping).module_ingest_query()
    assert "element application" in q
    assert "element modules" in q
    assert "element module" in q


def test_module_count_is_read_only(config: Config) -> None:
    q = QueryBuilder(config.mapping).module_count_query()
    assert "db:put" not in q
    assert "totalSize" in q


# -- integration ----------------------------------------------------------


def _client(config: Config) -> BaseXClient:
    return BaseXClient(
        config.basex.url, config.basex.user, config.basex.password, config.basex.timeout
    )


@pytest.fixture(scope="module")
def live() -> Config:
    config = Config.load(ROOT / "oai.toml")
    try:
        async def check() -> bool:
            async with _client(config) as bx:
                return await bx.ping()

        ok = asyncio.run(check())
    except Exception:
        ok = False
    if not ok:
        pytest.skip("BaseX is not answering")
    return config


def _run(config: Config, coro):
    async def go():
        async with _client(config) as bx:
            return await coro(bx)

    return asyncio.run(go())


def test_ingest_a_module_and_ready_it_back_the_colleagues_way(live: Config) -> None:
    builder = QueryBuilder(live.mapping)

    async def ingest_and_count(bx: BaseXClient):
        if await bx.database_exists(TEST_DB):
            await bx.drop_database(TEST_DB)
        await bx.create_database(TEST_DB)
        await bx.query(
            builder.module_ingest_query(),
            path=str(SAMPLE),
            db=TEST_DB,
            moduleName="Object",
        )
        count = await bx.query(
            f"count(collection('{TEST_DB}')/application/modules/"
            "module[@name='Object']/moduleItem)"
        )
        uris = await bx.query(
            f"distinct-values(for $m in collection('{TEST_DB}')"
            "/application/modules return namespace-uri($m))"
        )
        one = await bx.query(
            f"string((collection('{TEST_DB}')/application/modules/module/"
            "moduleItem[@id='1001']/dataField[@name='ObjObjectNumberTxt']/value)[1])"
        )
        return count.strip(), uris.strip(), one.strip()

    count, uris, value = _run(live, ingest_and_count)
    assert count == "3"
    assert uris == ""  # namespaces are gone
    assert value == "EM-1001"

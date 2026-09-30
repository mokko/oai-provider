"""HTTP layer tests: the app over ASGI, against live BaseX.

The verb tests call Provider directly, which leaves the HTTP layer and the
spec's requirement that GET and POST behave identically untested.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from oai.app import app as app_factory
from oai.app import create_app
from oai.basex import BaseXClient
from oai.config import Config
from oai.mapping import QueryBuilder

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ria-dump.xml"
TEST_DB = "oai_provider_http"


def base_config() -> Config:
    cfg = Config.load(ROOT / "oai.toml")
    return dataclasses.replace(
        cfg, basex=dataclasses.replace(cfg.basex, database=TEST_DB)
    )


@pytest.fixture(scope="module")
def cfg() -> Config:
    c = base_config()

    async def check() -> bool:
        async with BaseXClient(
            c.basex.url, c.basex.user, c.basex.password
        ) as bx:
            return await bx.ping()

    try:
        if not asyncio.run(check()):
            pytest.skip("BaseX not answering")
    except Exception:
        pytest.skip("BaseX not answering")
    return c


@pytest.fixture(scope="module")
def client(cfg: Config):
    """TestClient runs the lifespan, which is what creates the BaseX client."""
    with TestClient(create_app(cfg)) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def seeded(cfg: Config) -> None:
    variables = {
        "path": str(SAMPLE),
        "db": cfg.basex.database,
        "idPrefix": cfg.mapping.identifier_prefix,
        "tzOffset": cfg.mapping.timezone_offset,
        "dumpId": "http-dump",
        "now": "2026-09-29T12:00:00Z",
        "policy": cfg.identity.deleted_record,
    }

    async def go() -> None:
        builder = QueryBuilder(cfg.mapping)
        async with BaseXClient(
            cfg.basex.url, cfg.basex.user, cfg.basex.password
        ) as bx:
            await bx.command(f"DROP DB {cfg.basex.database}")
            await bx.command(f"CREATE DB {cfg.basex.database}")
            await bx.query(builder.ingest_query(), **variables)

    asyncio.run(go())


def test_identify_over_get(client) -> None:
    r = client.get("/oai", params={"verb": "Identify"})
    assert r.status_code == 200
    assert "text/xml" in r.headers["content-type"]
    assert "Museum collection" in r.text
    # the request element echoes the arguments and names the base URL
    assert 'verb="Identify"' in r.text


def test_get_and_post_are_equivalent(client) -> None:
    """The spec requires the two methods to behave identically, so the same
    request should produce the same body up to the response timestamp."""
    params = {
        "verb": "ListIdentifiers",
        "metadataPrefix": "ria",
        "set": "mimo",
    }
    get = client.get("/oai", params=params)
    post = client.post("/oai", data=params)
    assert get.status_code == post.status_code == 200

    def strip_timestamp(body: str) -> str:
        return re.sub(r"<[^>]*responseDate>[^<]*<", "<responseDate/>", body)

    assert strip_timestamp(get.text) == strip_timestamp(post.text)
    assert "spk-berlin.de:EM-objId-1001" in get.text


def test_post_with_a_duplicate_argument_is_rejected(client) -> None:
    """Form bodies can carry repeated keys, and repeating a single-valued
    argument is badArgument rather than a silent last-one-wins.

    The body is written out by hand: a harvester posts urlencoded bytes, and
    letting an HTTP client do the encoding tests the client, not the server.
    """
    r = client.post(
        "/oai",
        content="verb=Identify&verb=ListSets",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    assert 'code="badArgument"' in r.text


def test_trailing_slash_and_errors_still_answer_xml(client) -> None:
    r = client.get("/oai/", params={"verb": "Nope"})
    assert r.status_code == 200
    assert 'code="badVerb"' in r.text
    # OAI errors are part of a valid response, not an HTTP failure
    assert "<OAI-PMH" in r.text or ":OAI-PMH" in r.text


def test_healthz(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text.strip() == "ok"


def test_the_uvicorn_factory_builds_a_real_app(monkeypatch) -> None:
    """The factory is what `uvicorn --factory` calls; a broken import there
    would only show up when someone tried to run the server."""
    monkeypatch.setenv("OAI_CONFIG", str(ROOT / "oai.toml"))
    built = app_factory()
    assert built is not None
    routes = {route.path for route in built.routes}
    assert "/oai" in routes

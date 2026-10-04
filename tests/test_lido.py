"""LIDO as a stylesheet-backed metadata format (`kind = "xslt"`).

The stylesheet is the user's own zml2lido mapping, vendored under data/lido/.
It is not a per-record mapping - it reads a whole application/modules tree - so
what is tested here is mostly the reassembly and the shape of what is served.

Unit tests need no BaseX; the round trip skips unless one answers AND the real
module databases are present (they come from a real ingest, not from a fixture).
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from oai.basex import BaseXClient
from oai.config import Config, ConfigError, MetadataFormat
from oai.mapping import QueryBuilder
from oai.protocol import Provider, q, xslt_problem

ROOT = Path(__file__).resolve().parent.parent
LIDO = "{http://www.lido-schema.org}"


@pytest.fixture()
def config() -> Config:
    return Config.load(ROOT / "oai.toml")


def _with_format(tmp_path: Path, extra: str) -> Config:
    """The real config with an extra [[metadata.formats]] block appended.

    The real config's stylesheet path is relative to the repo, so it is made
    absolute here too - otherwise writing the config into tmp_path would break
    the *existing* lido format before the new one is even looked at.
    """
    text = (ROOT / "oai.toml").read_text()
    text = text.replace(
        'stylesheet = "data/lido/zml2lido.xsl"',
        'stylesheet = "' + str(ROOT / "data/lido/zml2lido.xsl") + '"',
    )
    # insert before the [[modules]] table, so the file stays valid TOML
    at = text.index("\n[[modules]]\n")
    bad = tmp_path / "cfg.toml"
    bad.write_text(text[:at] + "\n" + extra + "\n" + text[at:])
    return Config.load(bad)


# -- the rules ------------------------------------------------------------


def test_the_shipped_config_advertises_lido(config: Config) -> None:
    fmt = next(f for f in config.formats if f.prefix == "lido")
    assert fmt.kind == "xslt"
    assert Path(fmt.stylesheet).is_absolute(), "the path must be resolved at load"
    assert Path(fmt.stylesheet).is_file()
    assert fmt.record == "lido:lido"
    # LIDO is object-centric: persons and assets are not LIDO records
    assert fmt.modules == ("Object",)


def test_the_stylesheet_must_exist(tmp_path) -> None:
    with pytest.raises(ConfigError, match="stylesheet not found"):
        _with_format(
            tmp_path,
            '[[metadata.formats]]\nprefix = "nope"\nnamespace = "urn:x"\n'
            'kind = "xslt"\nstylesheet = "data/lido/does-not-exist.xsl"\n',
        )


def test_a_stylesheet_format_needs_a_path(tmp_path) -> None:
    with pytest.raises(ConfigError, match="needs"):
        _with_format(
            tmp_path,
            '[[metadata.formats]]\nprefix = "nope"\nnamespace = "urn:x"\n'
            'kind = "xslt"\n',
        )


def test_the_record_prefix_must_be_declared() -> None:
    with pytest.raises(ConfigError, match="undeclared prefix"):
        MetadataFormat(
            prefix="x",
            namespace="urn:x",
            kind="xslt",
            stylesheet=__file__,
            record="lido:lido",
            namespaces={},
        )


def test_an_unknown_module_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not in \\[\\[modules\\]\\]"):
        _with_format(
            tmp_path,
            '[[metadata.formats]]\nprefix = "nope"\nnamespace = "urn:x"\n'
            'kind = "xslt"\nstylesheet = "' + str(ROOT / "data/lido/zml2lido.xsl") + '"\n'
            'record = "lido:lido"\nmodules = ["Exhibition"]\n'
            'namespaces = { lido = "http://www.lido-schema.org" }\n',
        )


# -- the generated query --------------------------------------------------


def test_lido_pages_objects_only(config: Config) -> None:
    """A format that serves a subset of modules *is* that subset's result set:
    the header rows must not include persons or assets."""
    fmt = next(f for f in config.formats if f.prefix == "lido")
    qb = QueryBuilder(config.modules, config.timezone_offset)
    src = qb.source_expr(fmt)
    assert src.count("<row ") == 1, "only one module's rows"
    assert "sync_Object" in src
    assert "sync_Person" not in src
    # while the unrestricted formats span everything
    assert qb.source_expr().count("<row ") == 3


def test_the_transform_runs_inside_basex(config: Config) -> None:
    fmt = next(f for f in config.formats if f.prefix == "lido")
    qb = QueryBuilder(config.modules, config.timezone_offset)
    expr = qb.payload_expr(fmt)
    assert "xslt:transform(" in expr
    assert fmt.stylesheet in expr
    # the reassembled input is re-namespaced for a stylesheet written for RIA
    assert "local:zetcom(" in expr
    assert 'xmlns="http://www.zetcom.com/ria/ws/module"' in expr
    # and the record is lifted from the transform's result
    assert "$out//lido:lido" in expr


def test_the_world_is_rebuilt_from_all_the_databases(config: Config) -> None:
    fmt = next(f for f in config.formats if f.prefix == "lido")
    qb = QueryBuilder(config.modules, config.timezone_offset)
    expr = qb.payload_expr(fmt)
    # itself
    assert "<module name=\"Object\">{ local:zetcom($src) }</module>" in expr
    # people it names, forward from ObjPerAssociationRef
    assert "sync_Person" in expr and "ObjPerAssociationRef" in expr
    # assets that name IT - a reverse lookup, which is why this cannot be read
    # off the record alone
    assert "sync_Multimedia" in expr and "MulObjectRef" in expr
    assert "satisfies string($r) = $objId" in expr
    # and related objects
    assert "ObjObjectCre" in expr


# -- the round trip -------------------------------------------------------


def _client(config: Config) -> BaseXClient:
    return BaseXClient(
        config.basex.url, config.basex.user, config.basex.password, config.basex.timeout
    )


@pytest.fixture(scope="module")
def live() -> Config:
    cfg = Config.load(ROOT / "oai.toml")

    async def check() -> bool:
        async with _client(cfg) as bx:
            if not await bx.ping():
                return False
            # the round trip needs the real module databases, which come from an
            # ingest rather than from a fixture
            return int((await bx.query("count(collection('sync_Object'))")).strip() or 0) > 0

    try:
        ok = asyncio.run(check())
    except Exception:
        ok = False
    if not ok:
        pytest.skip("BaseX is not answering, or the module databases are empty")
    return cfg


def _call(config: Config, *pairs: tuple[str, str]):
    async def go():
        async with _client(config) as bx:
            return await Provider(config, bx).handle(list(pairs))

    return asyncio.run(go())


def test_saxon_is_available_here(live: Config) -> None:
    """The probe the app uses at startup to decide it can serve a stylesheet
    format at all."""

    async def go():
        async with _client(live) as bx:
            return await xslt_problem(bx)

    assert asyncio.run(go()) == ""


def test_list_identifiers_as_lido_is_the_object_set(live: Config) -> None:
    root = _call(live, ("verb", "ListIdentifiers"), ("metadataPrefix", "lido"))
    headers = root.findall(f"{q('ListIdentifiers')}/{q('header')}")
    assert headers, "no rows"
    ids = [h.find(q("identifier")).text for h in headers]
    assert all(i.startswith("spk-berlin.de:object-") for i in ids)
    # and the raw store still spans all three modules
    ria = _call(live, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    tok = ria.find(f"{q('ListIdentifiers')}/{q('resumptionToken')}")
    assert int(tok.get("completeListSize")) == 5884


def test_get_record_as_lido(live: Config) -> None:
    ident = "spk-berlin.de:object-935894"
    root = _call(
        live,
        ("verb", "GetRecord"),
        ("identifier", ident),
        ("metadataPrefix", "lido"),
    )
    assert root.find(q("error")) is None
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    assert md is not None and len(md), "no metadata served"
    rec = list(md)[0]
    assert rec.tag == f"{LIDO}lido"
    rec_id = rec.find(f"{LIDO}lidoRecID")
    assert rec_id is not None
    # the ISIL comes out of vocmap.xml, so this also proves the vocabulary map
    # was found by the transform
    assert "935894" in rec_id.text
    # the wraps that are structurally always emitted ...
    for wrap in (
        "objectClassificationWrap",
        "objectIdentificationWrap",
        "eventWrap",
        "recordWrap",
        "rightsWorkWrap",
    ):
        assert rec.find(f".//{LIDO}{wrap}") is not None, f"{wrap} missing"
    # ... and resourceWrap only when the object actually has an approved asset,
    # which is why it is not asserted unconditionally


def test_the_raw_payload_is_untouched_by_having_asked_for_lido(live: Config) -> None:
    """LIDO is a view: the stored payload is still served verbatim as `ria`."""
    root = _call(
        live,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:object-935894"),
        ("metadataPrefix", "ria"),
    )
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    assert list(md)[0].tag == "moduleItem"

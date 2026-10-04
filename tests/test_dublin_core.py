"""Dublin Core as a *derived* metadata format: the rules, the generated query,
and a round trip through the real store.

Unit tests need no BaseX; the round trip skips unless one answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from oai.basex import BaseXClient
from oai.config import Config, ConfigError, MetadataFormat, ModuleConfig, TermRule
from oai.mapping import QueryBuilder
from oai.protocol import Provider, q

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ria-dump.xml"
TEST_DB = "oai_dc_check"

DC_NS = "http://purl.org/dc/elements/1.1/"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"


@pytest.fixture()
def config() -> Config:
    return Config.load(ROOT / "oai.toml")


# -- the rules ------------------------------------------------------------


def test_the_shipped_config_advertises_dublin_core(config: Config) -> None:
    fmt = next(f for f in config.formats if f.prefix == "oai_dc")
    assert fmt.kind == "derived"
    assert fmt.wrapper == "oai_dc:dc"
    assert fmt.namespaces["dc"] == DC_NS
    assert fmt.namespace == OAI_DC_NS


def test_every_module_has_its_own_dc_terms(config: Config) -> None:
    """The three modules are three record shapes, so none may be left without
    terms - an empty oai_dc would be disseminated silently otherwise. The whole
    mapping is one block under the format, each term naming its module."""
    fmt = next(f for f in config.formats if f.prefix == "oai_dc")
    qb = _builder(config)
    for module in config.modules:
        terms = qb.terms_for(module, fmt)
        assert terms, f"{module.name} has no dc terms"
        assert {t.term for t in terms} >= {"dc:language"}


def test_a_module_scoped_term_applies_only_to_that_module(config: Config) -> None:
    """Scoping is the whole point of keeping the mapping in one block: an Object
    term must not leak into a Person record's metadata."""
    fmt = next(f for f in config.formats if f.prefix == "oai_dc")
    qb = _builder(config)
    obj, per = config.modules[0], config.modules[1]
    assert "dc:format" in {t.term for t in qb.terms_for(obj, fmt)}
    assert "dc:format" not in {t.term for t in qb.terms_for(per, fmt)}


def test_a_module_key_under_modules_terms_is_rejected(tmp_path) -> None:
    """[[modules.terms]] is already scoped to its module, so a `module` key
    there would silently do nothing - a mistake, not a narrowing."""
    with pytest.raises(ConfigError, match="redundant"):
        _derived_config(
            tmp_path,
            '[[metadata.formats]]\nprefix = "oai_dc"\nnamespace = "urn:x"\n'
            'kind = "derived"\nwrapper = "oai_dc:dc"\n'
            'namespaces = { oai_dc = "urn:oai_dc", dc = "urn:dc" }\n'
            '[[modules]]\nname = "Object"\ndatabase = "d"\n'
            '[[modules.terms]]\nmodule = "Object"\nterm = "dc:title"\nxpath = "a"\n',
        )


def test_a_term_needs_exactly_one_source() -> None:
    with pytest.raises(ConfigError, match="exactly one"):
        TermRule(term="dc:title")
    with pytest.raises(ConfigError, match="exactly one"):
        TermRule(term="dc:title", xpath="a", literal="b")


def test_a_term_must_be_a_prefixed_name() -> None:
    with pytest.raises(ConfigError, match="prefixed name"):
        TermRule(term="title", xpath="a")


def test_a_derived_format_needs_a_wrapper() -> None:
    with pytest.raises(ConfigError, match="wrapper"):
        MetadataFormat(
            prefix="x", namespace="urn:x", kind="derived", terms=(TermRule(term="dc:a", xpath="a"),)
        )


def test_a_term_prefix_must_be_declared() -> None:
    """A prefix resolves against the query's prolog - an undeclared one is
    XPST0081 at run time, so it is caught at load time instead."""
    with pytest.raises(ConfigError, match="undeclared prefix"):
        MetadataFormat(
            prefix="x",
            namespace="urn:x",
            kind="derived",
            wrapper="oai_dc:dc",
            namespaces={"oai_dc": OAI_DC_NS},
            terms=(TermRule(term="dc:title", xpath="a"),),
        )


def _builder(config: Config | None = None) -> QueryBuilder:
    """The shipped config's builder. The DB names live on the modules."""
    config = config or Config.load(ROOT / "oai.toml")
    return QueryBuilder(config.modules, config.timezone_offset)


def _derived_config(tmp_path: Path, body: str) -> Config:
    text = (ROOT / "oai.toml").read_text()
    # drop the real formats and modules so the test's own are the only ones
    head = text[: text.index("# Dublin Core, assembled")]
    bad = tmp_path / "cfg.toml"
    bad.write_text(head + body)
    return Config.load(bad)


def test_a_derived_format_without_terms_anywhere_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError, match="no terms of its own|is derived but"):
        _derived_config(
            tmp_path,
            '[[metadata.formats]]\nprefix = "oai_dc"\nnamespace = "urn:x"\n'
            'kind = "derived"\nwrapper = "oai_dc:dc"\n'
            'namespaces = { oai_dc = "urn:oai_dc", dc = "urn:dc" }\n',
        )


# -- the generated query --------------------------------------------------


def _builder_with_dc() -> MetadataFormat:
    fmt = MetadataFormat(
        prefix="oai_dc",
        namespace=OAI_DC_NS,
        kind="derived",
        wrapper="oai_dc:dc",
        namespaces={"oai_dc": OAI_DC_NS, "dc": DC_NS},
        terms=(
            TermRule(term="dc:identifier", xpath="//dataField[@name='A']/value"),
            TermRule(term="dc:language", literal="de"),
        ),
    )
    return fmt


def test_the_terms_sit_in_an_enclosed_expression() -> None:
    """A direct element constructor treats everything between the tags as TEXT
    unless it is inside { }. An unbraced `for $v ... return` there is literal
    text and its $v is undeclared - the bug this pins."""
    fmt = _builder_with_dc()
    qb = _builder()
    expr = qb.payload_expr(fmt)
    assert "<oai_dc:dc>{ " in expr
    assert "$v" in expr


def test_an_empty_value_emits_nothing() -> None:
    """The core promise: no blank dc elements."""
    fmt = _builder_with_dc()
    qb = _builder()
    expr = qb.payload_expr(fmt)
    assert "normalize-space(string(.)) ne ''" in expr


def test_the_source_never_carries_a_payload() -> None:
    """The whole point of the split: the order-by sorts rows that hold only
    headers, and the payload is fetched afterwards for the page alone.
    Measured before the fix: page_size=1 and page_size=100 cost the same,
    because every matching payload was attached and then discarded."""
    cfg = Config.load(ROOT / "oai.toml")
    qb = QueryBuilder(cfg.modules, cfg.timezone_offset)
    src = qb.source_expr()
    assert "<row " in src
    assert "$withPayload" not in src, "the source must not depend on withPayload"
    assert "$src" not in src and ":source/node()" not in src
    # and every header row is empty
    assert src.count("/>") == src.count("<row ")


def test_the_payload_phase_keys_on_the_page() -> None:
    cfg = Config.load(ROOT / "oai.toml")
    qb = QueryBuilder(cfg.modules, cfg.timezone_offset)
    for fmt in (None, next(f for f in cfg.formats if f.prefix == "ria")):
        expr = qb.payload_expr(fmt)
        assert "$wanted/@identifier" in expr, "payload must be filtered to the page"
        assert "<p id=" in expr


def test_a_literal_is_xml_escaped() -> None:
    qb = _builder()
    expr = qb._term_exprs((TermRule(term="dc:language", literal="a & b <c>"),))
    assert "a &amp; b &lt;c&gt;" in expr


def test_terms_are_relative_to_the_variable_that_holds_the_record() -> None:
    """In the payload phase the record is $src (module mode) or $d/env:source
    (envelope mode), never $r - a stale name is an XPST0008 at query time."""
    qb = _builder()
    expr = qb._term_exprs((TermRule(term="dc:type", xpath="a/b"),), "$src")
    assert "($src/a/b)" in expr
    assert "$r/" not in expr


# -- the round trip -------------------------------------------------------


def _client(config: Config) -> BaseXClient:
    return BaseXClient(
        config.basex.url, config.basex.user, config.basex.password, config.basex.timeout
    )


@pytest.fixture(scope="module")
def live() -> Config:
    cfg = Config.load(ROOT / "oai.toml")
    try:
        async def check() -> bool:
            async with _client(cfg) as bx:
                return await bx.ping()

        ok = asyncio.run(check())
    except Exception:
        ok = False
    if not ok:
        pytest.skip("BaseX is not answering")
    return cfg


@pytest.fixture()
def dc_config(live: Config) -> Config:
    """One module over the sample, with terms that hit and terms that miss."""
    module = ModuleConfig(
        name="Object",
        database=TEST_DB,
        identifier_prefix="x:obj-",
        terms=(
            TermRule(term="dc:title", xpath="//dataField[@name='ObjObjectTitleClb']/value"),
            TermRule(
                term="dc:identifier",
                xpath="//dataField[@name='ObjObjectNumberTxt']/value",
            ),
            # present in the sample's record 1001, absent from 1003
            TermRule(
                term="dc:type",
                xpath="//dataField[@name='ObjObjectNumberSortedTxt']/value",
            ),
            TermRule(term="dc:date", xpath="//dataField[@name='NotInThisData']/value"),
            TermRule(term="dc:language", literal="de"),
        ),
    )
    cfg = dataclasses.replace(live, modules=(module,))
    builder = QueryBuilder(cfg.modules, cfg.timezone_offset)

    async def seed(bx: BaseXClient):
        if await bx.database_exists(TEST_DB):
            await bx.drop_database(TEST_DB)
        await bx.create_database(TEST_DB)
        await bx.query(
            builder.module_ingest_query(),
            path=str(SAMPLE),
            db=TEST_DB,
            moduleName="Object",
        )

    async def go():
        async with _client(cfg) as bx:
            await seed(bx)

    asyncio.run(go())
    return cfg


def _call(config: Config, *pairs: tuple[str, str]):
    async def go(bx: BaseXClient):
        return await Provider(config, bx).handle(list(pairs))

    async def run():
        async with _client(config) as bx:
            return await go(bx)

    return asyncio.run(run())


def _dc_of(root) -> list[tuple[str, str]]:
    md = root.find(f"{q('GetRecord')}/{q('metadata')}")
    assert md is not None and len(md), "no metadata element"
    dc = list(md)[0]
    return [
        (child.tag.split("}")[-1], (child.text or "").strip()) for child in dc
    ]


def test_get_record_as_dublin_core(dc_config: Config) -> None:
    root = _call(
        dc_config,
        ("verb", "GetRecord"),
        ("identifier", "x:obj-1001"),
        ("metadataPrefix", "oai_dc"),
    )
    terms = _dc_of(root)
    assert ("identifier", "EM-1001") in terms
    assert ("title", "Trommel aus Ghana") in terms
    assert ("language", "de") in terms


def test_a_term_with_no_source_is_absent_not_empty(dc_config: Config) -> None:
    """NotInThisData matches nothing, and dc:date must therefore not appear."""
    root = _call(
        dc_config,
        ("verb", "GetRecord"),
        ("identifier", "x:obj-1002"),
        ("metadataPrefix", "oai_dc"),
    )
    terms = dict(_dc_of(root))
    assert "date" not in terms
    assert all(value for _, value in _dc_of(root)), "a blank dc element was served"


def test_the_shipped_mapping_serves_dublin_core_from_the_real_modules(
    dc_config: Config,
) -> None:
    """And the payload view still works: the same record as 'ria'."""
    ria = _call(
        dc_config,
        ("verb", "GetRecord"),
        ("identifier", "x:obj-1001"),
        ("metadataPrefix", "ria"),
    )
    md = ria.find(f"{q('GetRecord')}/{q('metadata')}")
    assert list(md)[0].tag == "moduleItem"

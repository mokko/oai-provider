"""Dublin Core as a *derived* metadata format: the rules, the generated query,
and a round trip through the real store.

Unit tests need no BaseX; the round trip skips unless one answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path
from xml.etree import ElementTree as ET

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
ZETCOM = "{http://www.zetcom.com/ria/ws/module}"


def _sample_ids() -> list[str]:
    """The Object record ids in the fixture, in document order.

    Derived rather than hard-coded: the fixture is real records now, so their ids
    are the export's, and asserting on a made-up one would be asserting on a
    fixture rather than on the provider.
    """
    root = ET.parse(SAMPLE).getroot()
    return [i.get("id") or "" for i in root.iter(ZETCOM + "moduleItem")]



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
    with pytest.raises(ConfigError, match="exactly one"):
        TermRule(term="dc:title", xpath="a", expression="b")


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
            TermRule(term="dc:title", xpath="//virtualField[@name='ObjObjectTitleVrt']/value"),
            TermRule(
                term="dc:identifier",
                xpath="//dataField[@name='ObjObjectNumberTxt']/value",
            ),
            # present in the sample's record 1001, absent from 1003
            TermRule(
                term="dc:type",
                xpath="//dataField[@name='ObjTechnicalTermClb']/value",
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
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    assert md is not None and len(md), "no metadata element"
    dc = list(md)[0]
    return [
        (child.tag.split("}")[-1], (child.text or "").strip()) for child in dc
    ]


def test_get_record_as_dublin_core(dc_config: Config) -> None:
    root = _call(
        dc_config,
        ("verb", "GetRecord"),
        ("identifier", f"x:obj-{_sample_ids()[0]}"),
        ("metadataPrefix", "oai_dc"),
    )
    terms = _dc_of(root)
    assert ("identifier", "79 D 2, fol. 23 recto") in terms
    assert ("title", "Päpstlicher Statuenhof und Brunnenmonument") in terms
    assert ("language", "de") in terms


def test_a_term_with_no_source_is_absent_not_empty(dc_config: Config) -> None:
    """NotInThisData matches nothing, and dc:date must therefore not appear."""
    root = _call(
        dc_config,
        ("verb", "GetRecord"),
        ("identifier", f"x:obj-{_sample_ids()[1]}"),
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
        ("identifier", f"x:obj-{_sample_ids()[0]}"),
        ("metadataPrefix", "ria"),
    )
    md = ria.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    # stored stripped, served namespaced: the wire payload must be admissible
    # under <metadata>'s namespace="##other" wildcard. The payload is the whole
    # record document (`application` root), which is the only GLOBAL element.
    assert list(md)[0].tag == "{http://www.zetcom.com/ria/ws/module}application"


def test_the_object_title_is_the_virtual_field(config: Config) -> None:
    """`ObjObjectTitleVrt` is the museum's own display title: non-empty on
    1000/1000 objects, and equal to the ObjObjectTitleGrp item with SortLnu = 1
    in all of them (measured over the real export, not the fixture - the fixture
    carries no title field at all, which is why dc:title was missing)."""
    fmt = next(f for f in config.formats if f.prefix == "oai_dc")
    titles = [
        t for t in fmt.terms if t.term == "dc:title" and t.module == "Object"
    ]
    assert titles, "the Object module maps no dc:title"
    assert titles[0].xpath == "//virtualField[@name='ObjObjectTitleVrt']/value"


def test_the_object_dimensions_are_composed(config: Config) -> None:
    """`dc:format` carries dimensions as well as material/technique — DCMI defines
    it as "the file format, physical medium, or dimensions of the resource". The
    value can't come from a path: height, width and the unit live in three
    places, so it is an `expression`, and the unit is read from `UnitDdiVoc`
    rather than assumed."""
    fmt = next(f for f in config.formats if f.prefix == "oai_dc")
    formats = [t for t in fmt.terms if t.term == "dc:format" and t.module == "Object"]
    assert len(formats) == 2, formats
    expressions = [t for t in formats if t.expression]
    assert len(expressions) == 1, "dimensions should be the expression"
    text = expressions[0].expression
    assert "ObjDimAllGrp" in text
    assert "HeightNum" in text and "WidthNum" in text
    assert "UnitDdiVoc" in text, "the unit is data, not a constant"


def test_the_person_date_is_the_primary_dated_entry(config: Config) -> None:
    """`PerDateGrp` is dated *events*, not a date list — residences, acquisitions,
    burials — so `dc:date` takes the `SortingLnu = 1` entry only, where the naive
    path would serve up to 12 values per person.

    It is deliberately **not** filtered on `NotesClb`: that field carries date
    qualifiers and sources ("1910 anderslt. Todesjahr", "Quelle: Metzger 2002")
    about as often as it carries a place, and filtering on it dropped real life
    dates."""
    fmt = next(f for f in config.formats if f.prefix == "oai_dc")
    dates = [t for t in fmt.terms if t.term == "dc:date" and t.module == "Person"]
    assert len(dates) == 1, dates
    assert "PerDateGrp" in dates[0].xpath
    assert "SortingLnu" in dates[0].xpath
    assert "NotesClb" not in dates[0].xpath, "NotesClb is not a discriminator"


def _flat(value: str) -> str:
    """Collapse runs of whitespace, so a value's embedded newlines are not the
    thing under test."""
    return " ".join(value.split())


def _source_titles(live: Config, obj_ids: list[str]) -> dict[str, str]:
    """The non-empty `ObjObjectTitleVrt` value for each id, straight from BaseX.

    One query, against the same field the mapping reads, so the test checks the
    served output against its *source* rather than against an assumption about
    the data. Returned as elements, not as delimited text: a title value can
    itself contain a newline, which a line-based reply would split.
    """
    obj = next(m for m in live.modules if m.name == "Object")
    wanted = ",".join(f"'{i}'" for i in obj_ids if i)
    if not wanted:
        return {}

    async def go() -> dict[str, str]:
        async with _client(live) as bx:
            nodes = await bx.query_nodes(
                f"for $d in collection('{obj.database}')//moduleItem[@id = ({wanted})]\n"
                "  let $v := $d//virtualField[@name='ObjObjectTitleVrt']"
                "/value[normalize-space(.) ne ''][1]\n"
                "  where $v\n"
                '  return <t id="{string($d/@id)}">{string($v)}</t>'
            )
        return {(n.get("id") or ""): (n.text or "") for n in nodes}

    return asyncio.run(go())


@pytest.mark.integration
def test_a_served_title_mirrors_its_source(live: Config) -> None:
    """The served dc:title reflects the source field, in both directions.

    "Every object has a title" was never true: the real export titles *most*
    objects but at least one carries an empty `<value/>` for
    `ObjObjectTitleVrt` (measured: object 1922446). The mapping is right to omit
    a term whose source is empty, so the invariant that holds is the pair - a
    non-empty source is always served, and an empty one never becomes a blank
    `<dc:title/>`. The fixture cannot prove this: it has no title field.
    """
    root = _call(
        live,
        ("verb", "ListRecords"),
        ("metadataPrefix", "oai_dc"),
        ("set", "KK"),
    )
    records = root.findall(f"{q('ListRecords')}/{q('record')}")
    assert records, "no records served"
    obj_ids = [
        (r.findtext(f"{q('header')}/{q('identifier')}") or "").rsplit("-", 1)[-1]
        for r in records
    ]
    source = _source_titles(live, obj_ids)

    for rec in records:
        ident = rec.findtext(f"{q('header')}/{q('identifier')}") or ""
        md = rec.find(f"{q('metadata')}")
        assert md is not None, f"{ident} served without metadata"
        dc = list(md)[0]
        titles = [
            (c.text or "").strip() for c in dc if c.tag.split("}")[-1] == "title"
        ]
        assert all(titles), f"{ident}: a blank dc:title was served"
        obj_id = ident.rsplit("-", 1)[-1]
        if obj_id in source:
            assert titles and _flat(titles[0]) == _flat(source[obj_id]), (
                f"{ident}: source title {source[obj_id]!r} not served ({titles})"
            )
        else:
            assert not titles, f"{ident}: no source title, yet one was served"



# -- dc:creator: the role allow-list ---------------------------------------


def _shipped_creator() -> TermRule:
    """dc:creator as `oai.toml` ships it, not a copy that could drift from it.

    Terms live on the metadata *format*, carrying the module they apply to.
    """
    config = Config.load(ROOT / "oai.toml")
    return next(
        t
        for f in config.formats
        for t in f.terms
        if t.term == "dc:creator" and t.module == "Object"
    )


def _only_creator_module(cfg: Config, term: TermRule) -> Config:
    """The live config, with one module over the sample and one dc term."""
    formats = tuple(
        dataclasses.replace(f, terms=(term,)) if f.prefix == "oai_dc" else f
        for f in cfg.formats
    )
    return dataclasses.replace(
        cfg,
        formats=formats,
        modules=(
            ModuleConfig(
                name="Object",
                database=TEST_DB,
                identifier_prefix="x:obj-",
            ),
        ),
    )


def _seed_object_module(cfg: Config, dump: Path) -> int:
    """Ingest one dump into the test database; returns how many records landed."""
    builder = QueryBuilder(cfg.modules, cfg.timezone_offset)

    async def go():
        async with _client(cfg) as bx:
            if await bx.database_exists(TEST_DB):
                await bx.drop_database(TEST_DB)
            await bx.create_database(TEST_DB)
            await bx.query(
                builder.module_ingest_query(),
                path=str(dump),
                db=TEST_DB,
                moduleName="Object",
            )
            return int(
                await bx.query(
                    f"count(db:get('{TEST_DB}')/application/modules/module/moduleItem)"
                )
            )

    return asyncio.run(go())


def _creators(cfg: Config, identifiers: list[str]) -> dict[str, list[str]]:
    served = {}
    for ident in identifiers:
        root = _call(
            cfg,
            ("verb", "GetRecord"),
            ("identifier", ident),
            ("metadataPrefix", "oai_dc"),
        )
        served[ident] = [v for term, v in _dc_of(root) if term == "creator"]
    return served

@pytest.mark.integration
def test_the_shipped_dc_creator_reads_the_role_not_the_display_order(
    live: Config,
) -> None:
    """dc:creator is chosen by RoleVoc, and the name is parsed out of the
    association's display string."""
    shipped = _shipped_creator()
    assert shipped.expression and not shipped.xpath

    cfg = _only_creator_module(live, _shipped_creator())
    ids = _sample_ids()
    assert _seed_object_module(cfg, SAMPLE) == len(ids)
    served = _creators(cfg, [f"x:obj-{i}" for i in ids])

    # Each fixture record names exactly one maker, a Zeichner*in; the value is
    # the display string with the attribution prefix and the life dates gone -
    # and, for the third, with the name's own qualifier intact.
    assert [served[f"x:obj-{i}"] for i in ids] == [
        ["Maarten van Havelaar"],
        ["Maarten van Havelaar"],
        ["David Tielens (der Jüngere)"],
    ]
    for values in served.values():
        for value in values:
            assert not re.search(r"\(\d", value), f"life dates left in {value!r}"
            assert "Zeichner" not in value, f"role left in {value!r}"


# a sitter, a maker, and an engraver - the engraver's role is real but not yet
# in the shipped list, so it is the case that says the list is closed
SITTER_MAKER_ENGRAVER = """<application xmlns="http://www.zetcom.com/ria/ws/module">
  <modules>
    <module name="Object">
      <moduleItem hasAttachments="false" id="900001">
        <systemField dataType="Timestamp" name="__lastModified">
          <value>2026-01-01 00:00:00.000</value>
        </systemField>
        <moduleReference name="ObjPerAssociationRef" multiplicity="N">
          <moduleReferenceItem moduleItemId="501">
            <formattedValue language="de">Ausführung: Der Porträtierte, Dargestellt</formattedValue>
            <vocabularyReference name="RoleVoc" id="30423" instanceName="ObjPerAssociationRoleVgr">
              <vocabularyReferenceItem id="1" name="Dargestellt">
                <formattedValue language="de">Dargestellt</formattedValue>
              </vocabularyReferenceItem>
            </vocabularyReference>
          </moduleReferenceItem>
          <moduleReferenceItem moduleItemId="502">
            <formattedValue language="de">Ausführung: Die Zeichnerin (1700 - 1760), Zeichner*in</formattedValue>
            <vocabularyReference name="RoleVoc" id="30423" instanceName="ObjPerAssociationRoleVgr">
              <vocabularyReferenceItem id="2" name="Zeichner*in">
                <formattedValue language="de">Zeichner*in</formattedValue>
              </vocabularyReferenceItem>
            </vocabularyReference>
          </moduleReferenceItem>
          <moduleReferenceItem moduleItemId="503">
            <formattedValue language="de">Ausführung: Der Stecher (1650 - 1700), Stecher*in</formattedValue>
            <vocabularyReference name="RoleVoc" id="30423" instanceName="ObjPerAssociationRoleVgr">
              <vocabularyReferenceItem id="3" name="Stecher*in">
                <formattedValue language="de">Stecher*in</formattedValue>
              </vocabularyReferenceItem>
            </vocabularyReference>
          </moduleReferenceItem>
        </moduleReference>
      </moduleItem>
    </module>
  </modules>
</application>
"""


@pytest.mark.integration
def test_a_depicted_person_is_not_a_creator(live: Config, tmp_path) -> None:
    """The record that made this mapping necessary: its person associations are
    a depicted sitter, an engraver, and a maker - only the maker is a creator."""
    dump = tmp_path / "occupations.xml"
    dump.write_text(SITTER_MAKER_ENGRAVER, encoding="utf-8")

    cfg = _only_creator_module(live, _shipped_creator())
    assert _seed_object_module(cfg, dump) == 1
    served = _creators(cfg, ["x:obj-900001"])

    assert served["x:obj-900001"] == ["Die Zeichnerin"]

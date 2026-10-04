"""Module-per-database ingest: the colleague's `sync_<Type>` layout.

Unit tests need no BaseX; the integration tests are skipped unless one answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from oai.basex import BaseXClient
from oai.config import Config, ConfigError, ModuleConfig, SetRule
from oai.mapping import QueryBuilder
from oai.protocol import Provider, q

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
        "spk-berlin.de:object-",
        "spk-berlin.de:person-",
        "spk-berlin.de:asset-",
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


def test_module_mode_refuses_a_deleted_record_policy(tmp_path) -> None:
    """Module ingest never deletes (additive by default; --reset rebuilds from
    one dump), so no tombstone can be served. Advertising "persistent" would be
    the one lie a harvester cannot detect, so the combination is rejected at
    load time rather than documented."""
    text = (ROOT / "oai.toml").read_text().replace(
        'deletedRecord = "no"', 'deletedRecord = "persistent"'
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="deletedRecord must be"):
        Config.load(bad)


# -- the generated queries ------------------------------------------------


def test_module_ingest_strips_namespaces(config: Config) -> None:
    """The one deliberate exception to "payload verbatim": the tree is rebuilt
    with local-name(), so element and attribute names survive and namespace
    URIs do not."""
    q = QueryBuilder(config.modules, config.timezone_offset).module_ingest_query()
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
    q = QueryBuilder(config.modules, config.timezone_offset).module_ingest_query()
    assert "element application" in q
    assert "element modules" in q
    assert "element module" in q
    # @name is carried (it is what records_xpath() matches and the colleague's
    # layout carries), but the dump's @totalSize is NOT copied - the serve path
    # writes the document's own record count, so there is a single site.
    assert "local-name($a)" in q
    assert "'totalSize'" in q and "ne 'totalSize'" in q


def test_module_count_is_read_only(config: Config) -> None:
    q = QueryBuilder(config.modules, config.timezone_offset).module_count_query()
    assert "db:put" not in q
    assert "totalSize" in q
    # it also reports how many records will get no datestamp, so a record the
    # serve query silently drops is visible. castable, not a try/instance-of:
    # the latter is answered from the static type and never evaluates.
    assert "undated" in q
    assert "castable as xs:dateTime" in q


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
    builder = QueryBuilder(live.modules, live.timezone_offset)

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
            "moduleItem/dataField[@name='ObjObjectNumberTxt']/value)[1])"
        )
        return count.strip(), uris.strip(), one.strip()

    count, uris, value = _run(live, ingest_and_count)
    assert count == "3"
    assert uris == ""  # namespaces are gone
    assert value, "the ingested records should carry an object number"


def test_the_count_reports_records_without_a_usable_datestamp(live: Config, tmp_path) -> None:
    """A record the serve query cannot datestamp is dropped from the repository
    silently; the count pass must surface it, or an operator never learns."""
    dump = tmp_path / "chunk.xml"
    dump.write_text(
        '<application><modules><module name="Object" totalSize="3">'
        '<moduleItem id="1"><systemField name="__lastModified">'
        "<value>2026-01-02 10:00:00.0</value></systemField></moduleItem>"
        '<moduleItem id="2"><systemField name="__lastModified">'
        "<value>not-a-date</value></systemField></moduleItem>"
        '<moduleItem id="3"/>'
        "</module></modules></application>",
        encoding="utf-8",
    )
    builder = QueryBuilder(live.modules, live.timezone_offset)

    async def report(bx: BaseXClient):
        return await bx.query_xml(
            builder.module_count_query(), path=str(dump), moduleName="Object"
        )

    got = _run(live, report)
    assert got is not None
    assert got.get("items") == "3"
    assert got.get("undated") == "2"  # the malformed one and the missing one


# -- the six verbs over the module databases ------------------------------

DB_OBJ = "oai_module_check_obj"
DB_PER = "oai_module_check_per"

# A two-module dump, namespaced on purpose: the ingest must strip it, and the
# verbs must read it back. Object carries a group reference so a module set rule
# has something to match.
TWO_MODULES = """<?xml version="1.0" encoding="UTF-8"?>
<application xmlns="http://www.zetcom.com/ria/ws/module">
  <modules>
    <module name="Object" totalSize="2">
      <moduleItem hasAttachments="true" id="1001">
        <systemField dataType="Timestamp" name="__lastModified">
          <value>2026-01-02 10:00:00.0</value>
        </systemField>
        <dataField dataType="Varchar" name="ObjObjectNumberTxt">
          <value>EM-1001</value>
        </dataField>
        <virtualField name="ObjGeograficVrt">
          <value>Nürnberg</value>
        </virtualField>
        <repeatableGroup name="ObjDimAllGrp" size="1">
          <repeatableGroupItem id="7004">
            <dataField dataType="Long" name="SortLnu">
              <value>1</value>
            </dataField>
            <dataField dataType="Numeric" name="HeightNum">
              <value>25.2</value>
            </dataField>
            <dataField dataType="Numeric" name="WidthNum">
              <value>16.7</value>
            </dataField>
          </repeatableGroupItem>
        </repeatableGroup>
        <vocabularyReference name="UnitDdiVoc" id="60030" instanceName="UnitDdiVgr">
          <vocabularyReferenceItem id="1610300" name="cm">
            <formattedValue language="de">cm</formattedValue>
          </vocabularyReferenceItem>
        </vocabularyReference>
        <repeatableGroup name="ObjSystematicGrp" size="1">
          <repeatableGroupItem id="7005">
            <vocabularyReference name="SystematicVoc" id="60040" instanceName="SystematicVgr">
              <vocabularyReferenceItem id="1610400" name="Zeichnung">
                <formattedValue language="de">Zeichnung</formattedValue>
              </vocabularyReferenceItem>
            </vocabularyReference>
          </repeatableGroupItem>
        </repeatableGroup>
        <moduleReference name="ObjObjectGroupsRef">
          <moduleReferenceItem moduleItemId="6054"/>
        </moduleReference>
      </moduleItem>
      <moduleItem hasAttachments="false" id="1002">
        <systemField dataType="Timestamp" name="__lastModified">
          <value>2026-02-02 10:00:00.0</value>
        </systemField>
        <dataField dataType="Varchar" name="ObjObjectNumberTxt">
          <value>EM-1002</value>
        </dataField>
      </moduleItem>
    </module>
    <module name="Person" totalSize="1">
      <moduleItem hasAttachments="false" id="77">
        <systemField dataType="Timestamp" name="__lastModified">
          <value>2026-03-02 10:00:00.0</value>
        </systemField>
        <dataField dataType="Varchar" name="PerNameTxt">
          <value>Doe, Jane</value>
        </dataField>
        <repeatableGroup name="PerGeograficGrp" size="1">
          <repeatableGroupItem id="9003">
            <vocabularyReference name="PlaceNameVoc" id="60050" instanceName="PlaceNameVgr">
              <vocabularyReferenceItem id="1610500" name="Berlin">
                <formattedValue language="de">Berlin</formattedValue>
              </vocabularyReferenceItem>
            </vocabularyReference>
            <vocabularyReference name="GeograficVoc" id="60051" instanceName="GeograficVgr">
              <vocabularyReferenceItem id="1610501" name="Stadt">
                <formattedValue language="de">Stadt</formattedValue>
              </vocabularyReferenceItem>
            </vocabularyReference>
          </repeatableGroupItem>
        </repeatableGroup>
        <repeatableGroup name="PerDateGrp" size="2">
          <repeatableGroupItem id="9001">
            <dataField dataType="Long" name="SortingLnu">
              <value>1</value>
            </dataField>
            <dataField dataType="Varchar" name="DatingNewTxt">
              <value>1900 - 1970</value>
            </dataField>
            <dataField dataType="Clob" name="NotesClb">
              <value>anderslt. Todesjahr 1971</value>
            </dataField>
          </repeatableGroupItem>
          <repeatableGroupItem id="9002">
            <dataField dataType="Long" name="SortingLnu">
              <value>5</value>
            </dataField>
            <dataField dataType="Varchar" name="DatingNewTxt">
              <value>1930 - 1935</value>
            </dataField>
            <dataField dataType="Clob" name="NotesClb">
              <value>Berlin</value>
            </dataField>
          </repeatableGroupItem>
        </repeatableGroup>
      </moduleItem>
    </module>
  </modules>
</application>
"""


@pytest.fixture()
def module_config(live: Config, tmp_path) -> Config:
    """The live config with the two test modules swapped in for the real ones."""
    # the same id 1001 exists in both modules, which is exactly the collision
    # the module tag in the prefix has to resolve
    mods = (
        ModuleConfig(
            name="Object",
            database=DB_OBJ,
            identifier_prefix="spk-berlin.de:object-",
            sets=(
                SetRule(
                    spec="mimo",
                    label="Musikinstrumente",
                    xpath="moduleReference[@name='ObjObjectGroupsRef']"
                    "/moduleReferenceItem[@moduleItemId='6054']",
                ),
            ),
        ),
        ModuleConfig(
            name="Person",
            database=DB_PER,
            identifier_prefix="spk-berlin.de:person-",
        ),
    )
    dump = tmp_path / "two.xml"
    dump.write_text(TWO_MODULES, encoding="utf-8")
    config = dataclasses.replace(live, modules=mods)
    _seed_modules(config, dump)
    return config


def _seed_modules(config: Config, dump: Path) -> None:
    builder = QueryBuilder(config.modules, config.timezone_offset)

    async def go(bx: BaseXClient):
        for module in config.modules:
            if await bx.database_exists(module.database):
                await bx.drop_database(module.database)
            await bx.create_database(module.database)
            await bx.query(
                builder.module_ingest_query(),
                path=str(dump),
                db=module.database,
                moduleName=module.name,
            )

    _run(config, go)


def _call(config: Config, *pairs: tuple[str, str]):
    async def go(bx: BaseXClient):
        return await Provider(config, bx).handle(list(pairs))

    return _run(config, go)


def test_verbs_read_all_three_sources_by_identifier(module_config: Config) -> None:
    """Identifiers from both modules, and the shared integer id 1001 does not
    collide because each module carries its own prefix."""
    root = _call(
        module_config, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria")
    )
    headers = root.findall(f"{q('ListIdentifiers')}/{q('header')}")
    ids = {h.find(q("identifier")).text for h in headers}
    assert ids == {
        "spk-berlin.de:object-1001",
        "spk-berlin.de:object-1002",
        "spk-berlin.de:person-77",
    }


def test_get_record_reads_the_module_payload_and_shifts_the_datestamp(
    module_config: Config,
) -> None:
    root = _call(
        module_config,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:person-77"),
        ("metadataPrefix", "ria"),
    )
    header = root.find(f"{q('GetRecord')}/{q('record')}/{q('header')}")
    assert header.find(q("identifier")).text == "spk-berlin.de:person-77"
    # 10:00 local at +02:00 is 08:00Z
    assert header.find(q("datestamp")).text == "2026-03-02T08:00:00Z"
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    item = list(md)[0]
    z = "{http://www.zetcom.com/ria/ws/module}"
    # the whole record document, not the bare moduleItem: `moduleItem` is a
    # LOCAL element in Zetcom's schema, only `application` is global
    assert item.tag == f"{z}application"
    mi = item.find(f"{z}modules/{z}module[@name='Person']/{z}moduleItem")
    assert mi is not None
    name = mi.find(f"{z}dataField[@name='PerNameTxt']/{z}value")
    assert name is not None and name.text == "Doe, Jane"


def test_place_is_served_as_coverage_but_not_the_kind_of_place(
    module_config: Config,
) -> None:
    """dc:coverage carries the place *name*, never the kind of place. The object
    takes the museum's display form (ObjGeograficVrt); a person takes
    PlaceNameVoc out of PerGeograficGrp. Both fixtures carry GeograficVoc /
    GeopolVoc ("Stadt") alongside, so mapping the kind as coverage would fail
    here rather than quietly tell a harvester the place is "Stadt"."""
    for identifier, expected in (
        ("spk-berlin.de:object-1001", "Nürnberg"),
        ("spk-berlin.de:person-77", "Berlin"),
    ):
        root = _call(
            module_config,
            ("verb", "GetRecord"),
            ("identifier", identifier),
            ("metadataPrefix", "oai_dc"),
        )
        md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
        assert md is not None, f"no metadata for {identifier}"
        coverage = [c.text for c in list(md)[0] if c.tag.split("}")[-1] == "coverage"]
        assert coverage == [expected], (identifier, coverage)


def test_the_systematic_classification_is_a_subject(module_config: Config) -> None:
    """dc:subject carries the museum's systematic classification out of
    ObjSystematicGrp. `SystematicVoc` exists nowhere else in the real data — 0
    top-level elements — so the path has to go through the group item."""
    root = _call(
        module_config,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:object-1001"),
        ("metadataPrefix", "oai_dc"),
    )
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    assert md is not None, "no metadata served"
    subjects = [c.text for c in list(md)[0] if c.tag.split("}")[-1] == "subject"]
    assert "Zeichnung" in subjects, subjects


def test_dimensions_are_served_with_their_unit(module_config: Config) -> None:
    """Height, width and the unit are composed into one dc:format value. The unit
    comes from UnitDdiVoc — it is data (cm or mm), not a declared constant — so
    it has to appear in what is served."""
    root = _call(
        module_config,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:object-1001"),
        ("metadataPrefix", "oai_dc"),
    )
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    assert md is not None, "no metadata served"
    formats = [c.text for c in list(md)[0] if c.tag.split("}")[-1] == "format"]
    assert "25.2 x 16.7 cm" in formats, formats


def test_a_person_gets_life_dates_but_not_a_residence(module_config: Config) -> None:
    """dc:date for a Person is the PerDateGrp entry with `SortingLnu = 1`, and
    **not** filtered on NotesClb. The fixture pins both halves: the primary entry
    carries a note ("anderslt. Todesjahr 1971") and must still be served, while a
    residence in Berlin at SortingLnu = 5 must not be — excluded because it is
    not the primary, not because it has a note."""
    root = _call(
        module_config,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:person-77"),
        ("metadataPrefix", "oai_dc"),
    )
    md = root.find(f"{q('GetRecord')}/{q('record')}/{q('metadata')}")
    assert md is not None, "no metadata served"
    dates = [c.text for c in list(md)[0] if c.tag.split("}")[-1] == "date"]
    assert dates == ["1900 - 1970"], dates


def test_module_set_filters_on_the_envelope_free_rows(module_config: Config) -> None:
    root = _call(module_config, ("verb", "ListSets"))
    specs = [
        s.find(q("setSpec")).text for s in root.findall(f"{q('ListSets')}/{q('set')}")
    ]
    assert specs == ["mimo"]
    # and the filter actually selects: only the object with the group reference
    root = _call(
        module_config,
        ("verb", "ListIdentifiers"),
        ("metadataPrefix", "ria"),
        ("set", "mimo"),
    )
    headers = root.findall(f"{q('ListIdentifiers')}/{q('header')}")
    assert [h.find(q("identifier")).text for h in headers] == [
        "spk-berlin.de:object-1001"
    ]


def test_unknown_identifier_is_id_does_not_exist(module_config: Config) -> None:
    root = _call(
        module_config,
        ("verb", "GetRecord"),
        ("identifier", "spk-berlin.de:person-999"),
        ("metadataPrefix", "ria"),
    )
    err = root.find(q("error"))
    assert err is not None and err.get("code") == "idDoesNotExist"


def test_list_records_carries_the_module_item_as_metadata(module_config: Config) -> None:
    root = _call(module_config, ("verb", "ListRecords"), ("metadataPrefix", "ria"))
    mds = root.findall(f"{q('ListRecords')}/{q('record')}/{q('metadata')}")
    assert len(mds) == 3
    z = "{http://www.zetcom.com/ria/ws/module}"
    for md in mds:
        app = list(md)[0]
        assert app.tag == f"{z}application"
        mod = app.find(f"{z}modules/{z}module")
        assert mod is not None
        item = mod.find(f"{z}moduleItem")
        assert item is not None
        assert item.get("id")
        # totalSize counts the records in THIS document (the store keeps one
        # record per document), not the source file's count: the Object module
        # fixture declares totalSize="2" while each stored document holds one.
        assert mod.get("totalSize") == "1"


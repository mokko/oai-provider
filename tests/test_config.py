"""Config + query generation tests. No BaseX required."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from oai.config import ENVELOPE_NS, Config, ConfigError, Mapping, SetRule
from oai.mapping import QueryBuilder

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def config() -> Config:
    return Config.load(ROOT / "oai.toml")


def test_loads_the_real_config(config: Config) -> None:
    assert config.basex.database == "riadb"
    assert config.mapping.identifier == "@id"
    assert config.mapping.identifier_prefix == "spk-berlin.de:EM-objId-"
    assert config.identity.deleted_record == "persistent"
    assert {r.spec for r in config.mapping.sets} == {"mimo", "78"}
    assert {r.label for r in config.mapping.sets} == {
        "Musikinstrumente",
        "Schellackplatten",
    }


def test_password_comes_from_env_when_set(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OAI_BASEX_PASSWORD", "from-env")
    cfg = Config.load(ROOT / "oai.toml")
    assert cfg.basex.password == "from-env"
    assert cfg.basex.from_env is True


def test_reserved_envelope_prefix_is_rejected(tmp_path) -> None:
    text = (ROOT / "oai.toml").read_text()
    text = text.replace(
        'namespaces = { m = "http://www.zetcom.com/ria/ws/module" }',
        'namespaces = { env = "http://example.org/x" }',
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="reserved"):
        Config.load(bad)


def test_bad_deleted_record_is_rejected(tmp_path) -> None:
    text = (ROOT / "oai.toml").read_text().replace(
        'deletedRecord = "persistent"', 'deletedRecord = "maybe"'
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="deletedRecord"):
        Config.load(bad)


def test_relative_and_absolute_xpath_resolution() -> None:
    mapping = Mapping(
        records="/a/b",
        identifier="@id",
        identifier_prefix="x:",
        datestamp="d/value",
    )
    # relative hangs off the record node, absolute off the document
    assert mapping.resolve("@id") == "$r/@id"
    assert mapping.resolve("d/value") == "$r/d/value"
    assert mapping.resolve("/a/b") == "$doc/a/b"
    assert mapping.resolve("//anything") == "$doc//anything"


def test_query_declares_the_prefixes_it_uses(config: Config) -> None:
    """The trap that cost two round trips: a prefix used in a query resolves
    against the query prolog, never against the source document."""
    q = QueryBuilder(config.mapping).ingest_query()
    assert 'declare namespace m = "http://www.zetcom.com/ria/ws/module";' in q
    assert f'declare namespace env = "{ENVELOPE_NS}";' in q
    assert "$r/m:systemField[@name='__lastModified']/m:value" in q


def test_stripped_namespaces_need_no_declarations() -> None:
    mapping = Mapping(
        records="/application/modules/module[@name='Object']/moduleItem",
        identifier="@id",
        identifier_prefix="x:",
        datestamp="systemField[@name='__lastModified']/value",
        namespaces={},
    )
    q = QueryBuilder(mapping).ingest_query()
    assert "declare namespace m" not in q
    assert "$r/systemField[@name='__lastModified']/value" in q
    # the envelope itself is always declared
    assert f'declare namespace env = "{ENVELOPE_NS}";' in q


def test_user_supplied_values_are_variables_not_query_text(config: Config) -> None:
    """Paths and prefixes must not be interpolated into the query."""
    q = QueryBuilder(config.mapping).ingest_query()
    for name in ("$path", "$db", "$idPrefix", "$tzOffset", "$dumpId"):
        assert f"declare variable {name} external;" in q
    assert "spk-berlin.de" not in q


def test_sets_are_authored_labels_not_record_values(config: Config) -> None:
    """The security property: the setSpec is the string we wrote, so no
    value from a record - internal group id, org unit - can appear in OAI
    output, and an unlisted group is not harvestable."""
    exprs = QueryBuilder(config.mapping).set_expressions()
    assert "then 'mimo' else ()" in exprs
    assert "then '78' else ()" in exprs
    assert "exists(" in exprs
    # no value ever flows from the record into a setSpec
    assert "string($v)" not in exprs
    assert "concat(" not in exprs


def test_invalid_setspec_is_rejected(config: Config) -> None:
    with pytest.raises(ConfigError, match="setSpec"):
        SetRule(spec="not valid!", label="nope", xpath="x")


def test_duplicate_setspec_is_rejected(tmp_path) -> None:
    text = (ROOT / "oai.toml").read_text().replace('spec = "78"', 'spec = "mimo"')
    bad = tmp_path / "bad.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="duplicate"):
        Config.load(bad)


def test_payload_is_stored_unaltered(config: Config) -> None:
    """Explicit decision: the internal XML is stored and served verbatim.

    Nothing is filtered out, so no transform/filter step may appear in the
    ingest query without that decision being revisited.
    """
    q = QueryBuilder(config.mapping).ingest_query()
    assert "<env:source>{$r}</env:source>" in q
    assert "copy $c" not in q
    assert "delete node" not in q
    assert "PAYLOAD_EXPR" not in q


def test_deleted_record_policy_reaches_the_reconcile_query(config: Config) -> None:
    q = QueryBuilder(config.mapping).reconcile_query()
    assert "db:delete($db, db:path($d))" in q
    assert 'env:status="deleted"' in q

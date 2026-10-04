"""Config + query generation tests. No BaseX required."""

from __future__ import annotations

from pathlib import Path

import pytest

from oai.config import Config, ConfigError, SetRule
from oai.mapping import QueryBuilder

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def config() -> Config:
    return Config.load(ROOT / "oai.toml")


def test_loads_the_real_config(config: Config) -> None:
    assert [m.name for m in config.modules] == ["Object", "Person", "Multimedia"]
    assert {m.database for m in config.modules} == {
        "sync_Object",
        "sync_Person",
        "sync_Multimedia",
    }
    assert config.timezone_offset == "+02:00"
    assert config.identity.deleted_record == "no"
    obj = next(m for m in config.modules if m.name == "Object")
    assert obj.identifier_prefix == "spk-berlin.de:object-"
    assert {r.spec for r in obj.sets} == {"KK"}


def test_password_comes_from_env_when_set(monkeypatch) -> None:
    monkeypatch.setenv("OAI_BASEX_PASSWORD", "from-env")
    cfg = Config.load(ROOT / "oai.toml")
    assert cfg.basex.password == "from-env"
    assert cfg.basex.from_env is True


def test_bad_deleted_record_is_rejected(tmp_path) -> None:
    text = (ROOT / "oai.toml").read_text().replace(
        'deletedRecord = "no"', 'deletedRecord = "maybe"'
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="deletedRecord"):
        Config.load(bad)


def test_module_mode_refuses_a_deleted_record_policy(tmp_path) -> None:
    """Serving no tombstones while advertising deletions is the one lie a
    harvester cannot detect, so [[modules]] plus anything but "no" is a
    load-time error rather than a note in the docs."""
    text = (ROOT / "oai.toml").read_text().replace(
        'deletedRecord = "no"', 'deletedRecord = "persistent"'
    )
    bad = tmp_path / "bad.toml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="deletedRecord"):
        Config.load(bad)


def test_module_queries_declare_no_prefixes(config: Config) -> None:
    """Module mode stores namespace-stripped records, so a generated query needs
    no prefix declarations of its own. (The trap that cost two round trips is
    that a prefix used in a query resolves against the query prolog, never
    against the source document's own xmlns.)"""
    q = QueryBuilder(config.modules, config.timezone_offset).page_query()
    assert "declare namespace m" not in q
    assert "declare namespace env" not in q
    assert "systemField[@name='__lastModified']/value" in q


def test_user_supplied_values_are_variables_not_query_text(config: Config) -> None:
    """Paths and prefixes must not be interpolated into the query."""
    q = QueryBuilder(config.modules, config.timezone_offset).module_ingest_query()
    for name in ("$path", "$db", "$moduleName"):
        assert f"declare variable {name} external;" in q
    assert "spk-berlin.de" not in q


def test_sets_are_authored_labels_not_record_values(config: Config) -> None:
    """The security property: the setSpec is the string written in config, so no
    value from a record - internal group id, organisational unit - can reach OAI
    output, and an unlisted group is not harvestable at all."""
    obj = next(m for m in config.modules if m.name == "Object")
    exprs = QueryBuilder(
        config.modules, config.timezone_offset
    )._set_expressions(obj.sets)
    assert "then 'KK' else ()" in exprs
    assert "exists(" in exprs
    # no value ever flows from the record into a setSpec
    assert "string($v)" not in exprs
    assert "concat(" not in exprs


def test_invalid_setspec_is_rejected() -> None:
    with pytest.raises(ConfigError, match="setSpec"):
        SetRule(spec="not valid!", label="nope", xpath="x")


# -- the off-host placeholder guard ----------------------------------------


def test_placeholder_secrets_are_refused_offhost(monkeypatch) -> None:
    """The repo runs locally with the shipped placeholders, but advertising a
    non-loopback baseURL (which harvesters are told to call) with them is a
    load-time error, not a note in the docs."""
    monkeypatch.setenv("OAI_BASE_URL", "http://example.org/oai")
    monkeypatch.setenv("OAI_BASEX_PASSWORD", "devpass")
    monkeypatch.setenv("OAI_TOKEN_SECRET", "dev-token-secret-change-me")
    with pytest.raises(ConfigError, match="placeholder"):
        Config.load(ROOT / "oai.toml")


def test_placeholder_secrets_are_fine_on_loopback(monkeypatch) -> None:
    monkeypatch.setenv("OAI_BASE_URL", "http://localhost:8000/oai")
    monkeypatch.setenv("OAI_BASEX_PASSWORD", "devpass")
    monkeypatch.setenv("OAI_TOKEN_SECRET", "dev-token-secret-change-me")
    cfg = Config.load(ROOT / "oai.toml")
    assert cfg.identity.base_url == "http://localhost:8000/oai"


def test_real_secrets_allow_an_offhost_baseurl(monkeypatch) -> None:
    monkeypatch.setenv("OAI_BASE_URL", "https://museum.example.org/oai")
    monkeypatch.setenv("OAI_BASEX_PASSWORD", "a-real-password")
    monkeypatch.setenv("OAI_TOKEN_SECRET", "a-real-token-secret")
    cfg = Config.load(ROOT / "oai.toml")
    assert cfg.identity.base_url == "https://museum.example.org/oai"

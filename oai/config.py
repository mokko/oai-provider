"""Configuration: OAI identity, BaseX connection, and the XPath mapping.

Nothing here knows about MuseumPlus. The mapping is data: which XPaths pull
identifier / datestamp / sets out of arbitrary XML. A different source is a
different oai.toml.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENVELOPE_NS = "urn:oai:envelope"

# Prefix used inside generated queries for our own envelope. Must not collide
# with anything in [mapping.namespaces].
ENVELOPE_PREFIX = "env"


class ConfigError(Exception):
    """Bad or missing configuration."""


@dataclass(frozen=True)
class Identity:
    repository_name: str
    base_url: str
    admin_email: str
    earliest_datestamp: str = "1970-01-01T00:00:00Z"
    granularity: str = "YYYY-MM-DDThh:mm:ssZ"
    # OAI's deletedRecord: "no" | "transient" | "persistent"
    deleted_record: str = "persistent"
    description: str = ""

    def __post_init__(self) -> None:
        if self.deleted_record not in {"no", "transient", "persistent"}:
            raise ConfigError(
                f"deletedRecord must be no|transient|persistent, "
                f"got {self.deleted_record!r}"
            )
        if self.granularity not in {
            "YYYY-MM-DD",
            "YYYY-MM-DDThh:mm:ssZ",
        }:
            raise ConfigError(f"unsupported granularity {self.granularity!r}")


@dataclass(frozen=True)
class BaseXSettings:
    url: str
    database: str
    user: str
    password: str
    timeout: float = 120.0
    from_env: bool = False


@dataclass(frozen=True)
class SetRule:
    """One way of deriving a setSpec from a record.

    name    - human label, used in reports
    xpath   - relative to the record (or absolute if it starts with /)
    spec    - template for the setSpec; "{value}" is the extracted value.
              Defaults to the raw value.
    """

    name: str
    xpath: str
    spec: str = "{value}"


@dataclass(frozen=True)
class Mapping:
    records: str
    identifier: str
    identifier_prefix: str
    datestamp: str
    timezone_offset: str = "+00:00"
    namespaces: dict[str, str] = field(default_factory=dict)
    sets: tuple[SetRule, ...] = ()

    def resolve(self, xpath: str) -> str:
        """Turn a config XPath into one rooted at the right query variable.

        Relative XPaths hang off the record node ($r); ones starting with /
        are rooted at the document ($doc).
        """
        x = xpath.strip()
        if not x:
            raise ConfigError("empty XPath in mapping")
        if x.startswith("/"):
            return f"$doc{x}"
        return f"$r/{x}"

    def namespace_declarations(self) -> str:
        """Prolog declarations for the generated query.

        These are required, not cosmetic: a prefix used in a query resolves
        against the query's own prolog, never against the source document's
        declarations. Configure [] for namespace-stripped data.
        """
        lines = [
            f'declare namespace {p} = "{uri}";'
            for p, uri in self.namespaces.items()
        ]
        lines.append(f'declare namespace {ENVELOPE_PREFIX} = "{ENVELOPE_NS}";')
        return "\n".join(lines)


@dataclass(frozen=True)
class Config:
    identity: Identity
    basex: BaseXSettings
    mapping: Mapping

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config not found: {path}")
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)

        try:
            ident_raw = raw["identity"]
            bx_raw = raw["basex"]
            map_raw = raw["mapping"]
        except KeyError as exc:
            raise ConfigError(f"missing section {exc} in {path}") from exc

        for key in ("repositoryName", "baseURL", "adminEmail"):
            if key not in ident_raw:
                raise ConfigError(f"identity.{key} is required")

        identity = Identity(
            repository_name=ident_raw["repositoryName"],
            base_url=ident_raw["baseURL"],
            admin_email=ident_raw["adminEmail"],
            earliest_datestamp=ident_raw.get(
                "earliestDatestamp", "1970-01-01T00:00:00Z"
            ),
            granularity=ident_raw.get("granularity", "YYYY-MM-DDThh:mm:ssZ"),
            deleted_record=ident_raw.get("deletedRecord", "persistent"),
            description=ident_raw.get("description", ""),
        )

        namespaces = dict(map_raw.get("namespaces", {}))
        if ENVELOPE_PREFIX in namespaces:
            raise ConfigError(
                f"mapping.namespaces must not define {ENVELOPE_PREFIX!r}; "
                "it is reserved for the OAI envelope"
            )
        for prefix in namespaces:
            if not prefix or ":" in prefix or " " in prefix:
                raise ConfigError(f"bad namespace prefix {prefix!r}")

        # Credentials: env wins. The config file is a dev convenience only.
        pw_env = os.environ.get("OAI_BASEX_PASSWORD")
        pw = pw_env or bx_raw.get("password", "")
        if not pw:
            raise ConfigError(
                "no BaseX password: set OAI_BASEX_PASSWORD (preferred) "
                "or basex.password in the config"
            )

        basex = BaseXSettings(
            url=bx_raw.get("url", "http://localhost:8080/rest").rstrip("/"),
            database=bx_raw.get("database") or _must(bx_raw, "database"),
            user=bx_raw.get("user") or _must(bx_raw, "user"),
            password=pw,
            timeout=float(bx_raw.get("timeout", 120)),
            from_env=bool(pw_env),
        )

        set_rules = tuple(
            SetRule(
                name=s.get("name") or f"set{i}",
                xpath=s["xpath"],
                spec=s.get("spec", "{value}"),
            )
            for i, s in enumerate(map_raw.get("sets", []))
        )

        mapping = Mapping(
            records=map_raw["records"],
            identifier=map_raw["identifier"],
            identifier_prefix=map_raw.get("identifierPrefix", ""),
            datestamp=map_raw["datestamp"],
            timezone_offset=map_raw.get("timezoneOffset", "+00:00"),
            namespaces=namespaces,
            sets=set_rules,
        )
        for rule in mapping.sets:
            if "{value}" not in rule.spec:
                raise ConfigError(
                    f"set rule {rule.name!r} spec must contain {{value}}"
                )

        return cls(identity=identity, basex=basex, mapping=mapping)


def _must(section: dict, key: str) -> str:
    if key not in section:
        raise ConfigError(f"missing required key {key!r}")
    return section[key]

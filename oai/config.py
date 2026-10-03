"""Configuration: OAI identity, BaseX connection, and the XPath mapping.

Nothing here knows about MuseumPlus. The mapping is data: which XPaths pull
identifier / datestamp / sets out of arbitrary XML. A different source is a
different oai.toml.
"""

from __future__ import annotations

import os
import re
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
    """One public OAI set, as an allow-list entry.

    spec  - the setSpec published in OAI responses. Authored here, never
            copied out of a record.
    label - the setName shown by ListSets.
    xpath - relative to the record; a record is in this set if the XPath
            selects anything. Put value tests inside the XPath, e.g.
            m:dataField[@name='ObjekttypTxt'][m:value='Musikinstrument'].

    Because spec is written by hand and never taken from the data, an
    internal group id or organisational unit cannot reach OAI output by
    accident - and anything not named here is not harvestable at all.
    """

    spec: str
    label: str
    xpath: str

    def __post_init__(self) -> None:
        if not SETSPEC_RE.match(self.spec):
            raise ConfigError(
                f"setSpec {self.spec!r} is not valid OAI setSpec syntax "
                "(colon-separated tokens of A-Za-z0-9 _ - . ! ~ * ' ( ))"
            )
        if not self.label.strip():
            raise ConfigError(f"set {self.spec!r} has an empty label")


# OAI-PMH setSpec: colon-separated tokens, each of unreserved characters.
SETSPEC_RE = re.compile(r"^[A-Za-z0-9_\-\.!~*'()]+(:[A-Za-z0-9_\-\.!~*'()]+)*$")


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
class ProtocolSettings:
    page_size: int = 100
    token_ttl: int = 86400
    token_secret: str = ""
    from_env: bool = False


@dataclass(frozen=True)
class MetadataFormat:
    """A format we can disseminate.

    kind="passthrough" serves the stored payload verbatim inside <metadata>.
    A derived format (oai_dc assembled from XPaths) is not implemented - that
    is still an open decision.
    """

    prefix: str
    namespace: str
    schema: str = ""
    kind: str = "passthrough"


@dataclass(frozen=True)
class ModuleConfig:
    """One MuseumPlus module, ingested into a database of its own.

    This emulates the colleague's `sync_<Type>` layout: a database per module,
    namespaces stripped on the way in, and records stored so that

        collection('sync_Object')/application/modules/module[@name='Object']/moduleItem

    reads exactly like his own queries. The module name is the one in the
    dump (`module/@name`); it is also what tells two otherwise identical
    records apart, so it is carried in the OAI identifier prefix.

    The XPaths below are **relative to the record** and written for
    namespace-stripped data, which is what module mode stores. They are the
    module's own mapping so that a module can differ from its siblings; the
    defaults are the MuseumPlus shapes.
    """

    name: str
    database: str
    identifier_prefix: str = ""
    records: str = ""
    identifier: str = "@id"
    datestamp: str = "systemField[@name='__lastModified']/value"
    sets: tuple[SetRule, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigError("module.name is required")
        if not self.database.strip():
            raise ConfigError(f"module {self.name!r} has no database")
        # The database name is interpolated into generated queries (it names a
        # collection), so it is held to an identifier-shaped charset rather
        # than trusted raw.
        if not MODULE_DB_RE.fullmatch(self.database):
            raise ConfigError(
                f"module {self.name!r}: database {self.database!r} is not a "
                "plain name (A-Za-z0-9 _ - . only)"
            )

    def records_xpath(self) -> str:
        return self.records or (
            f"/application/modules/module[@name='{self.name}']/moduleItem"
        )


# A BaseX database name we are willing to write into generated query text.
MODULE_DB_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


@dataclass(frozen=True)
class Config:
    identity: Identity
    basex: BaseXSettings
    mapping: Mapping
    protocol: ProtocolSettings = field(default_factory=ProtocolSettings)
    formats: tuple[MetadataFormat, ...] = ()
    modules: tuple[ModuleConfig, ...] = ()


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
                spec=s["spec"],
                label=s.get("label", s["spec"]),
                xpath=s["xpath"],
            )
            for s in map_raw.get("sets", [])
        )
        specs = [r.spec for r in set_rules]
        if len(specs) != len(set(specs)):
            raise ConfigError("duplicate setSpec in [mapping.sets]")

        modules = tuple(
            ModuleConfig(
                name=m.get("name", ""),
                database=m.get("database", ""),
                identifier_prefix=m.get("identifierPrefix", ""),
                records=m.get("records", ""),
                identifier=m.get("identifier", "@id"),
                datestamp=m.get(
                    "datestamp", "systemField[@name='__lastModified']/value"
                ),
                sets=tuple(
                    SetRule(
                        spec=s["spec"],
                        label=s.get("label", s["spec"]),
                        xpath=s["xpath"],
                    )
                    for s in m.get("sets", [])
                ),
            )
            for m in raw.get("modules", [])
        )
        module_dbs = [m.database for m in modules]
        if len(module_dbs) != len(set(module_dbs)):
            raise ConfigError("two [[modules]] entries share a database")
        module_names = [m.name for m in modules]
        if len(module_names) != len(set(module_names)):
            raise ConfigError("duplicate module name in [[modules]]")

        mapping = Mapping(
            records=map_raw["records"],
            identifier=map_raw["identifier"],
            identifier_prefix=map_raw.get("identifierPrefix", ""),
            datestamp=map_raw["datestamp"],
            timezone_offset=map_raw.get("timezoneOffset", "+00:00"),
            namespaces=namespaces,
            sets=set_rules,
        )

        proto_raw = raw.get("protocol", {})
        secret_env = os.environ.get("OAI_TOKEN_SECRET")
        secret = secret_env or proto_raw.get("tokenSecret", "")
        protocol = ProtocolSettings(
            page_size=int(proto_raw.get("pageSize", 100)),
            token_ttl=int(proto_raw.get("tokenTTL", 86400)),
            token_secret=secret,
            from_env=bool(secret_env),
        )
        if protocol.page_size < 1:
            raise ConfigError("protocol.pageSize must be >= 1")

        formats = tuple(
            MetadataFormat(
                prefix=f["prefix"],
                namespace=f["namespace"],
                schema=f.get("schema", ""),
                kind=f.get("kind", "passthrough"),
            )
            for f in raw.get("metadata", {}).get("formats", [])
        )
        for fmt in formats:
            if fmt.kind != "passthrough":
                raise ConfigError(
                    f"metadata format {fmt.prefix!r}: kind {fmt.kind!r} is not "
                    "implemented yet (only 'passthrough')"
                )
        if not formats:
            raise ConfigError(
                "no [[metadata.formats]] configured: a repository must "
                "advertise at least one"
            )

        return cls(
            identity=identity,
            basex=basex,
            mapping=mapping,
            protocol=protocol,
            formats=formats,
            modules=modules,
        )


def _must(section: dict, key: str) -> str:
    if key not in section:
        raise ConfigError(f"missing required key {key!r}")
    return section[key]

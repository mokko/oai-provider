"""Configuration: OAI identity, BaseX connection, and the XPath mapping.

Nothing here knows about MuseumPlus. The mapping is data: which XPaths pull
identifier / datestamp / sets out of arbitrary XML. A different source is a
different oai.toml.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ENVELOPE_NS = "urn:oai:envelope"

# Prefix used inside generated queries for our own envelope. Must not collide
# with anything in [mapping.namespaces].
ENVELOPE_PREFIX = "env"


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE file, without overwriting anything
    already set.

    stdlib on purpose: python-dotenv is a dependency this project does not need
    for four lines of parsing. A real environment variable always wins, so a
    process manager's EnvironmentFile behaves the same as this file.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def dotenv_path(config_path: Path | None = None) -> Path:
    """Where the deployment's secrets and address live.

    OAI_ENV_FILE wins; otherwise `.env` beside the config file, so a checkout
    carries its own settings and one file can be swapped per deployment.
    """
    override = os.environ.get("OAI_ENV_FILE")
    if override:
        return Path(override)
    base = config_path.parent if config_path else Path(".")
    return base / ".env"


class ConfigError(Exception):
    """Bad or missing configuration."""


# Values shipped in the repo so a local checkout runs out of the box. They are
# public, so they must never protect a repository reachable off-host.
PLACEHOLDER_SECRETS = frozenset({"devpass", "dev-token-secret-change-me"})


def _host_is_loopback(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host in {"", "localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def check_secrets(identity: Identity, basex: BaseXSettings,
                  protocol: ProtocolSettings) -> None:
    """Refuse to serve off-host with a shipped placeholder credential.

    The repo stays runnable locally - loopback plus the dev values is fine - but
    advertising a non-loopback `baseURL` (which harvesters are told to call) with
    the public placeholders is exactly how a secret leaks: a forged resumption
    token, or a full read/write REST endpoint. So this is a load-time error, not
    a note in the docs, in the same spirit as the module-mode/deletedRecord check.
    """
    if _host_is_loopback(identity.base_url):
        return
    offenders = []
    if not basex.password or basex.password in PLACEHOLDER_SECRETS:
        offenders.append("OAI_BASEX_PASSWORD")
    if not protocol.token_secret or protocol.token_secret in PLACEHOLDER_SECRETS:
        offenders.append("OAI_TOKEN_SECRET")
    if offenders:
        raise ConfigError(
            f"baseURL {identity.base_url!r} is not loopback, but "
            + " and ".join(offenders)
            + " still hold the shipped placeholder values. Set real values (a "
            "real environment variable, or the .env beside the config) before "
            "serving off-host - or point baseURL at localhost while developing."
        )


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
class TermRule:
    """One Dublin Core element and where its value comes from.

    `term` is a **prefixed** name that the format declares ("dc:type"). Exactly
    one of `xpath` / `literal` is set: an XPath relative to the record, or a
    constant written here (dc:language "de").

    **An empty value is never emitted** - the element is left out instead. That
    is what "correct Dublin Core" means here: a term with no source is absent,
    not present and blank.
    """

    term: str
    xpath: str = ""
    literal: str = ""

    def __post_init__(self) -> None:
        if not TERM_RE.fullmatch(self.term):
            raise ConfigError(
                f"term {self.term!r} must be a prefixed name like 'dc:title'"
            )
        if bool(self.xpath) == bool(self.literal):
            raise ConfigError(
                f"term {self.term!r} needs exactly one of xpath / literal"
            )


TERM_RE = re.compile(r"^[A-Za-z_][\w.\-]*:[A-Za-z_][\w.\-]*$")


@dataclass(frozen=True)
class MetadataFormat:
    """A format we can disseminate.

    kind="passthrough" serves the stored payload verbatim inside <metadata>.
    kind="derived" builds `wrapper` from the term rules (oai_dc), so the
    payload is stored once and any number of views are assembled from it.
    """

    prefix: str
    namespace: str
    schema: str = ""
    kind: str = "passthrough"
    # kind="derived": the element to build ("oai_dc:dc") and the prefixes its
    # wrapper and terms use. Every prefix a term names must be declared here,
    # because a query's prefixes resolve against its own prolog.
    wrapper: str = ""
    namespaces: dict[str, str] = field(default_factory=dict)
    terms: tuple[TermRule, ...] = ()
    # kind="xslt": an XSLT stylesheet that turns a record into metadata. The
    # transform runs **inside BaseX** (xslt:transform), so the payload never
    # leaves the store; it needs Saxon on BaseX's classpath, which the app
    # probes for at startup rather than discovering on a request.
    #
    # `record` is the element to lift out of the transform's output - the
    # stylesheet emits a wrapper (lidoWrap) and one <metadata> holds one
    # record (lido) - and its prefix must appear in `namespaces`.
    #
    # `modules` restricts which modules serve this format: LIDO is
    # object-centric, so persons and assets are not LIDO records.
    stylesheet: str = ""
    record: str = ""
    modules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"passthrough", "derived", "xslt"}:
            raise ConfigError(
                f"metadata format {self.prefix!r}: kind {self.kind!r} is not "
                "implemented (only 'passthrough', 'derived' and 'xslt')"
            )
        if self.kind == "xslt":
            if not self.stylesheet:
                raise ConfigError(
                    f"metadata format {self.prefix!r}: kind \"xslt\" needs "
                    "a stylesheet path"
                )
            if self.record:
                prefix = self.record.split(":", 1)[0]
                if prefix not in self.namespaces:
                    raise ConfigError(
                        f"metadata format {self.prefix!r}: record {self.record!r} "
                        f"uses undeclared prefix {prefix!r}"
                    )
        if self.kind == "derived":
            if not self.wrapper:
                raise ConfigError(
                    f"metadata format {self.prefix!r}: a derived format needs "
                    "a wrapper element (e.g. wrapper = \"oai_dc:dc\")"
                )
            if not self.terms and not self.namespaces:
                raise ConfigError(
                    f"metadata format {self.prefix!r}: a derived format needs a "
                    "namespaces table for its wrapper and terms"
                )
            wrapper_prefix = self.wrapper.split(":", 1)[0]
            declared = set(self.namespaces)
            if wrapper_prefix not in declared:
                raise ConfigError(
                    f"metadata format {self.prefix!r}: wrapper prefix "
                    f"{wrapper_prefix!r} is not in its namespaces table"
                )
            for rule in self.terms:
                prefix = rule.term.split(":", 1)[0]
                if prefix not in declared:
                    raise ConfigError(
                        f"metadata format {self.prefix!r}: term {rule.term!r} "
                        f"uses undeclared prefix {prefix!r}"
                    )


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
    # A derived metadata format's terms are the shape of one *kind of record*,
    # and the modules are different record shapes, so a module may override
    # them. Falling back to the format's own terms keeps one shared mapping for
    # the common case.
    terms: tuple[TermRule, ...] = ()

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
        # Deployment-specific values come from the environment (a real variable
        # or a .env beside the config), so the tracked file stays neutral.
        load_dotenv(dotenv_path(path))
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
            # OAI_BASE_URL wins: the address a harvester must call back is a
            # property of where this is deployed, not of the code, and it must
            # be right - Identify echoes it and harvesters then call it.
            base_url=os.environ.get("OAI_BASE_URL") or ident_raw["baseURL"],
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
                terms=tuple(
                    TermRule(
                        term=t["term"],
                        xpath=t.get("xpath", ""),
                        literal=t.get("literal", ""),
                    )
                    for t in m.get("terms", [])
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
        # Module mode cannot report deletions: each database is dropped and
        # rebuilt from a full dump, so nothing knows what vanished. Advertising
        # "persistent" or "transient" while serving no tombstones is the one
        # lie a harvester can never detect, so it is a configuration error
        # rather than a note in the docs.
        if modules and identity.deleted_record != "no":
            raise ConfigError(
                "module mode cannot report deletions (a module database is "
                "dropped and rebuilt), so deletedRecord must be \"no\", not "
                f"{identity.deleted_record!r}"
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
                wrapper=f.get("wrapper", ""),
                namespaces=dict(f.get("namespaces", {})),
                stylesheet=f.get("stylesheet", ""),
                record=f.get("record", ""),
                modules=tuple(f.get("modules", ())),
                terms=tuple(
                    TermRule(
                        term=t["term"],
                        xpath=t.get("xpath", ""),
                        literal=t.get("literal", ""),
                    )
                    for t in f.get("terms", [])
                ),
            )
            for f in raw.get("metadata", {}).get("formats", [])
        )
        format_prefixes = [f.prefix for f in formats]
        if len(format_prefixes) != len(set(format_prefixes)):
            raise ConfigError("duplicate metadataPrefix in [[metadata.formats]]")
        # A stylesheet-backed format: resolve the path against the config file
        # so a deployment can be run from anywhere, and **fail at load time**
        # if it is missing - a format that cannot possibly work should stop the
        # server starting, not error once a harvester asks for it.
        module_names_all = {m.name for m in modules}
        resolved_formats = []
        for fmt in formats:
            if fmt.kind != "xslt":
                resolved_formats.append(fmt)
                continue
            sheet = Path(fmt.stylesheet)
            if not sheet.is_absolute():
                sheet = (path.parent / sheet).resolve()
            if not sheet.is_file():
                raise ConfigError(
                    f"metadata format {fmt.prefix!r}: stylesheet not found: {sheet}"
                )
            if not modules:
                raise ConfigError(
                    f"metadata format {fmt.prefix!r} is kind \"xslt\" but there "
                    "are no [[modules]]: the stylesheet reads a whole "
                    "application/modules tree, which only module mode provides"
                )
            unknown = [m for m in fmt.modules if m not in module_names_all]
            if unknown:
                raise ConfigError(
                    f"metadata format {fmt.prefix!r}: modules "
                    f"{', '.join(unknown)} are not in [[modules]]"
                )
            resolved_formats.append(
                dataclasses.replace(fmt, stylesheet=str(sheet))
            )
        formats = tuple(resolved_formats)
        # A derived format needs somewhere to get its terms. Either it carries
        # its own (which every source then shares), or every module brings its
        # own - and a module that has none would silently disseminate an empty
        # oai_dc, so that is an error rather than an omission.
        for fmt in formats:
            if fmt.kind != "derived" or fmt.terms:
                continue
            if not modules:
                raise ConfigError(
                    f"metadata format {fmt.prefix!r} is derived but has no "
                    "[[metadata.formats.terms]]"
                )
            missing = [m.name for m in modules if not m.terms]
            if missing:
                raise ConfigError(
                    f"metadata format {fmt.prefix!r} has no terms of its own, "
                    "so every module must define them; missing for: "
                    + ", ".join(missing)
                )
        if not formats:
            raise ConfigError(
                "no [[metadata.formats]] configured: a repository must "
                "advertise at least one"
            )

        check_secrets(identity, basex, protocol)

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

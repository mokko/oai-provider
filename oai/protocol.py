"""OAI-PMH 2.0: the six verbs, argument validation, error codes, and
resumption tokens.

Pure by design - request arguments in, XML element out, no sockets. Every
verb and every error path is therefore testable without an HTTP server, and
the HTTP layer (`oai/app.py`) is a thin adapter.

Protocol reference:
https://www.openarchives.org/OAI/2.0/openarchivesprotocol.htm
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .basex import BaseXClient
from .config import Config
from .mapping import LIDO_NS, ZETCOM_NS, QueryBuilder

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NS = "http://purl.org/dc/elements/1.1/"
TOKEN_VERSION = 1

# A namespace prefix is arbitrary - the *URI* is what the spec fixes - so the
# `ns0:` ElementTree invents is conformant. It is still worth avoiding: our own
# elements come out as ns0/ns1 because OAI's namespace is not in ElementTree's
# "well-known" map (while `dc` and `xsi` are, which is why those already looked
# right), whereas the spec's examples and harvesters' expectations use `oai:`.
# Registering changes only how our elements are written out, never the
# namespace they are in.
ET.register_namespace("oai", OAI_NS)
ET.register_namespace("oai_dc", OAI_DC_NS)

# The payloads carry the source vocabularies - RIA for a `ria` record, LIDO for a
# `lido` one - and ElementTree invents `ns2:` for any namespace outside its
# well-known map. Register the prefixes the data and the config already use, so a
# served record reads like the export it came from instead of like a parser's
# leftovers. Registered here, beside the OAI ones, so the wire naming is decided
# in one place.
ET.register_namespace("z", ZETCOM_NS)
ET.register_namespace("lido", LIDO_NS)

VERBS = (
    "Identify",
    "ListMetadataFormats",
    "ListSets",
    "GetRecord",
    "ListIdentifiers",
    "ListRecords",
)

# The internal page/row elements the templates return live in NO namespace:
# they are ours, not OAI's, and keeping them unqualified means they can never
# be confused with protocol elements. Comparing them against an OAI-qualified
# tag silently matched nothing.
PAGE_TAG = "page"
ROW_TAG = "row"

# both the "illegal argument" and "missing argument" checks - keeping two
# lists in sync is how a verb ends up rejecting its own required argument.
VERB_ARGS: dict[str, tuple[set[str], set[str]]] = {
    "Identify": (set(), set()),
    "ListMetadataFormats": ({"identifier"}, set()),
    "ListSets": (set(), set()),
    "GetRecord": (set(), {"identifier", "metadataPrefix"}),
    "ListIdentifiers": (
        {"from", "until", "set"},
        {"metadataPrefix"},
    ),
    "ListRecords": (
        {"from", "until", "set"},
        {"metadataPrefix"},
    ),
}

TS_DAY = r"\d{4}-\d{2}-\d{2}"
TS_SECONDS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"

# An XML attribute name is an NCName - no colon, so it can never introduce a
# namespace prefix. Used to keep a bad request argument out of <request> and
# the response well-formed.
_XML_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def q(name: str) -> str:
    return f"{{{OAI_NS}}}{name}"


# A stylesheet that is deliberately XSLT **2.0** (string-join does not exist in
# 1.0). BaseX's built-in processor is 1.0 and refuses it; Saxon on the classpath
# accepts it. So this one query answers "can this server serve a stylesheet
# format at all", without guessing at a jar name or a version.
XSLT_PROBE = (
    'xslt:transform(<probe/>,'
    '<xsl:stylesheet version="2.0"'
    ' xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
    '<xsl:template match="/"><ok/></xsl:template></xsl:stylesheet>)'
)


async def xslt_problem(client: BaseXClient) -> str:
    """Why an `xslt` format cannot be served here, or "" if it can.

    Checked once at startup rather than on the first request that asks for
    LIDO: a repository that advertises a format it cannot produce is worse than
    one that refuses to start.
    """
    try:
        out = await client.query(XSLT_PROBE)
    except Exception as exc:  # noqa: BLE001 - any failure is the same answer
        return str(exc).strip()[:300]
    if "ok" not in out:
        return f"xslt:transform returned {out.strip()[:120]!r}"
    return ""


class ProtocolError(Exception):
    """An OAI error, to be rendered as <error code="...">."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime, granularity: str) -> str:
    dt = dt.astimezone(timezone.utc)
    if granularity == "YYYY-MM-DD":
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# datestamps
# --------------------------------------------------------------------------


def _is_valid_ts(value: str, granularity: str) -> bool:
    if granularity == "YYYY-MM-DD":
        if not re.fullmatch(TS_DAY, value):
            return False
    else:
        # the finer granularity must also accept the coarser form for
        # from/until, per the spec
        if not re.fullmatch(f"{TS_DAY}|{TS_SECONDS}", value):
            return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def normalise_from(value: str, granularity: str) -> str:
    if granularity == "YYYY-MM-DD" or len(value) > 10:
        return value
    return f"{value}T00:00:00Z"


def normalise_until(value: str, granularity: str) -> str:
    if granularity == "YYYY-MM-DD" or len(value) > 10:
        return value
    # a day-granularity upper bound means the whole of that day
    return f"{value}T23:59:59Z"


# --------------------------------------------------------------------------
# arguments
# --------------------------------------------------------------------------


@dataclass
class Request:
    verb: str = ""
    args: dict[str, str] = field(default_factory=dict)

    @property
    def metadata_prefix(self) -> str:
        return self.args.get("metadataPrefix", "")

    @property
    def token(self) -> str:
        return self.args.get("resumptionToken", "")


def parse_args(pairs: list[tuple[str, str]]) -> Request:
    """Validate the argument set and return a Request.

    Strict on purpose: repeated arguments, unknown arguments, and a
    resumptionToken combined with anything else are all badArgument. Harvesters
    are supposed to send exactly one of either.
    """
    seen: dict[str, str] = {}
    for key, value in pairs:
        if key in seen:
            raise ProtocolError(
                "badArgument", f"repeated argument: {key}"
            )
        seen[key] = value

    verb = seen.pop("verb", None)
    if verb is None:
        raise ProtocolError("badVerb", "missing verb")
    if verb not in VERBS:
        raise ProtocolError("badVerb", f"unknown verb: {verb}")

    if "resumptionToken" in seen:
        token = seen.pop("resumptionToken")
        if seen:
            raise ProtocolError(
                "badArgument",
                "resumptionToken is exclusive; no other arguments allowed "
                f"(got {', '.join(sorted(seen))})",
            )
        if not token:
            raise ProtocolError("badArgument", "empty resumptionToken")
        return Request(verb=verb, args={"resumptionToken": token})

    allowed, required = VERB_ARGS[verb]
    unknown = set(seen) - allowed - required
    if unknown:
        raise ProtocolError(
            "badArgument", f"illegal argument(s): {', '.join(sorted(unknown))}"
        )
    missing = [name for name in sorted(required) if not seen.get(name)]
    if missing:
        raise ProtocolError(
            "badArgument", f"missing required argument(s): {', '.join(missing)}"
        )

    return Request(verb=verb, args=seen)


# --------------------------------------------------------------------------
# resumption tokens
# --------------------------------------------------------------------------


class TokenError(ProtocolError):
    def __init__(self, message: str) -> None:
        super().__init__("badResumptionToken", message)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


@dataclass
class TokenState:
    """Everything needed to resume without server-side state.

    The cursor is the pair (datestamp, identifier) because the datestamp alone
    is not unique, and the pinned `until` keeps a re-ingest during the harvest
    from moving a record across a page boundary.
    """

    fingerprint: str
    pinned_until: str
    last_datestamp: str = ""
    last_identifier: str = ""
    prefix: str = ""
    issued_at: int = 0
    delivered: int = 0
    complete_list_size: int = 0
    set_spec: str = ""
    from_: str = ""


def encode_token(state: TokenState, secret: str) -> str:
    payload = {
        "v": TOKEN_VERSION,
        "fp": state.fingerprint,
        "u": state.pinned_until,
        "ds": state.last_datestamp,
        "id": state.last_identifier,
        "p": state.prefix,
        "iss": state.issued_at,
        "n": state.complete_list_size,
        "c": state.delivered,
        "s": state.set_spec,
        "f": state.from_,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if secret:
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()[:16]
        return f"{_b64e(body)}.{_b64e(sig)}"
    return _b64e(body)


def decode_token(token: str, secret: str, fingerprint: str, ttl: int) -> TokenState:
    """Decode and validate, or raise badResumptionToken.

    Every failure mode here is a loud error rather than a quietly wrong page:
    a truncated token, a tampered payload, a token from a different mapping,
    or one that has expired.
    """
    try:
        if secret:
            body_b64, _, sig_b64 = token.partition(".")
            if not sig_b64:
                raise TokenError("token is not signed")
            body = _b64d(body_b64)
            expect = hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).digest()[:16]
            if not hmac.compare_digest(expect, _b64d(sig_b64)):
                raise TokenError("token signature does not match")
        else:
            body = _b64d(token)
        payload = json.loads(body)
    except TokenError:
        raise
    except Exception as exc:  # noqa: BLE001 - any malformed token is the same
        raise TokenError(f"token is malformed ({exc})") from exc

    if payload.get("v") != TOKEN_VERSION:
        raise TokenError(f"token version {payload.get('v')!r} is not supported")
    if payload.get("fp") != fingerprint:
        raise TokenError(
            "token was issued under a different mapping; the same request "
            "arguments no longer mean the same result set, so restart the "
            "harvest"
        )
    issued = payload.get("iss") or 0
    if ttl and time.time() - issued > ttl:
        raise TokenError("token has expired; restart the harvest")

    return TokenState(
        fingerprint=payload["fp"],
        pinned_until=payload.get("u", ""),
        last_datestamp=payload.get("ds", ""),
        last_identifier=payload.get("id", ""),
        prefix=payload.get("p", ""),
        issued_at=issued,
        delivered=int(payload.get("c", 0)),
        complete_list_size=int(payload.get("n", 0)),
        set_spec=payload.get("s", ""),
        from_=payload.get("f", ""),
    )


# --------------------------------------------------------------------------
# XML building
# --------------------------------------------------------------------------


def _el(parent: ET.Element, name: str, **attrs: str) -> ET.Element:
    child = ET.SubElement(parent, q(name))
    for key, value in attrs.items():
        child.set(key, value)
    return child


def error_response(request: ET.Element, exc: ProtocolError, granularity: str) -> ET.Element:
    root = ET.Element(q("OAI-PMH"))
    root.set(f"{{{XSI_NS}}}schemaLocation",
             f"{OAI_NS} http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd")
    _el(root, "responseDate").text = _fmt(utcnow(), granularity)
    root.append(request)
    node = _el(root, "error", **({"code": exc.code} if exc.code else {}))
    node.text = exc.message or exc.code
    return root


def make_request_el(base_url: str, pairs: list[tuple[str, str]]) -> ET.Element:
    """The <request> element, echoing the arguments actually received.

    Argument names become **XML attribute names**, which are NCNames — no colon.
    A stray query parameter such as `spk-berlin.de:object-851035` (a missing
    `identifier=`) turns the colon into an undeclared namespace prefix and the
    whole response stops being well-formed XML — a harvester fails outright,
    where it should simply be told badArgument. So a name that is not an NCName
    is left out of the echo; parse_args still names it in the error message.
    """
    el = ET.Element(q("request"))
    for key, value in pairs:
        if _XML_NAME.match(key):
            el.set(key, value)
    el.text = base_url
    return el


def header_el(row: ET.Element) -> ET.Element:
    header = ET.Element(q("header"))
    if row.get("status") == "deleted":
        header.set("status", "deleted")
    _el(header, "identifier").text = row.get("identifier", "")
    _el(header, "datestamp").text = row.get("datestamp", "")
    for spec in (row.get("sets") or "").split():
        _el(header, "setSpec").text = spec
    return header


# --------------------------------------------------------------------------
# the provider
# --------------------------------------------------------------------------


@dataclass
class Page:
    rows: list[ET.Element]
    has_more: bool
    complete_list_size: int
    pinned_until: str
    last_datestamp: str
    last_identifier: str


class Provider:
    """Serves the six verbs against BaseX."""

    def __init__(self, config: Config, client: BaseXClient) -> None:
        self.config = config
        self.client = client
        self.builder = QueryBuilder(config.modules, config.timezone_offset)
        self.granularity = config.identity.granularity
        # Sets come from the modules' own allow-lists. Deduplicated by spec,
        # because the same set can be declared by more than one module.
        rules: list = []
        seen: set[str] = set()
        for module in config.modules:
            for rule in module.sets:
                if rule.spec not in seen:
                    seen.add(rule.spec)
                    rules.append(rule)
        self.set_rules = tuple(rules)
        # Bound into every query that renders a source. The templates still
        # declare $db external (module mode's collection() calls name their
        # databases literally), and an unbound external variable is an error,
        # so it is passed empty. $tzOffset shifts a local wall clock to UTC.
        self.source_vars = {
            "db": "",
            "tzOffset": config.timezone_offset,
            # the deployment's vocmap, for the formats that resolve related-work
            # ISILs locally (see todo/related-works-online.md); "" when no format
            # asks for it
            "vocmap": next((f.vocmap for f in config.formats if f.vocmap), ""),
        }

    # -- helpers ---------------------------------------------------------

    def format_for(self, prefix: str):
        for fmt in self.config.formats:
            if fmt.prefix == prefix:
                return fmt
        raise ProtocolError(
            "cannotDisseminateFormat", f"unsupported metadataPrefix: {prefix}"
        )

    def _new_token_state(self, request: Request, pinned_until: str) -> TokenState:
        """Used only for a fresh harvest; resumes rebuild state from the token."""
        return TokenState(
            fingerprint=self.builder.fingerprint(),
            pinned_until=pinned_until,
            prefix=request.metadata_prefix,
            issued_at=int(time.time()),
            set_spec=request.args.get("set", ""),
            from_=request.args.get("from", ""),
        )

    async def fetch_page(
        self,
        *,
        set_spec: str,
        from_: str,
        until: str,
        after_ds: str,
        after_id: str,
        with_payload: bool,
        fmt=None,
        want_total: bool = False,
    ) -> Page:
        limit = self.config.protocol.page_size
        nodes = await self.client.query_nodes(
            self.builder.page_query(fmt),
            **self.source_vars,
            set=set_spec,
            **{"from": from_},
            until=until,
            afterDs=after_ds,
            afterId=after_id,
            limit=limit,
            withPayload="true" if with_payload else "false",
            wantTotal="true" if want_total else "false",
        )
        rows: list[ET.Element] = []
        total = 0
        if nodes:
            if nodes[0].tag == PAGE_TAG:
                rows = list(nodes[0])
                # present only when want_total. On the first page the cursor is
                # empty, so this count IS the whole pinned set - which is why
                # the caller needs no separate COUNT query.
                total = int(nodes[0].get("total") or 0)
            else:
                rows = nodes
        has_more = len(rows) > limit
        rows = rows[:limit]
        return Page(
            rows=rows,
            has_more=has_more,
            complete_list_size=total,
            pinned_until=until,
            last_datestamp=rows[-1].get("datestamp", "") if rows else after_ds,
            last_identifier=rows[-1].get("identifier", "") if rows else after_id,
        )

    # -- verbs -----------------------------------------------------------

    async def identify(self, request: Request) -> ET.Element:
        ident = self.config.identity
        node = ET.Element(q("Identify"))
        _el(node, "repositoryName").text = ident.repository_name
        _el(node, "baseURL").text = ident.base_url
        _el(node, "protocolVersion").text = "2.0"
        _el(node, "adminEmail").text = ident.admin_email
        _el(node, "earliestDatestamp").text = ident.earliest_datestamp
        _el(node, "deletedRecord").text = ident.deleted_record
        _el(node, "granularity").text = ident.granularity
        if ident.description:
            # descriptionType is `<any namespace="##other">`: bare text is not
            # admissible, so the note is wrapped in a small oai_dc record -
            # schema-backed, and a namespace harvesters already understand.
            # _el() qualifies every name with the OAI namespace, so foreign
            # elements are built directly.
            desc = _el(node, "description")
            dc = ET.SubElement(desc, f"{{{OAI_DC_NS}}}dc")
            ET.SubElement(
                dc, f"{{{DC_NS}}}description"
            ).text = ident.description
        return node

    async def list_metadata_formats(self, request: Request) -> ET.Element:
        node = ET.Element(q("ListMetadataFormats"))
        identifier = request.args.get("identifier", "")
        if identifier:
            rows = await self.client.query_nodes(
                self.builder.record_query(),
                **self.source_vars,
                identifier=identifier,
                withPayload="false",
            )
            if not rows:
                raise ProtocolError(
                    "idDoesNotExist", f"unknown identifier: {identifier}"
                )
        for fmt in self.config.formats:
            md = _el(node, "metadataFormat")
            _el(md, "metadataPrefix").text = fmt.prefix
            _el(md, "schema").text = fmt.schema or ""
            _el(md, "metadataNamespace").text = fmt.namespace
        return node

    async def list_sets(self, request: Request) -> ET.Element:
        if not self.set_rules:
            raise ProtocolError("noSetHierarchy", "this repository has no sets")
        node = ET.Element(q("ListSets"))
        for rule in self.set_rules:
            item = _el(node, "set")
            _el(item, "setSpec").text = rule.spec
            _el(item, "setName").text = rule.label
        return node

    async def get_record(self, request: Request) -> ET.Element:
        fmt = self.format_for(request.metadata_prefix)
        identifier = request.args["identifier"]
        rows = await self.client.query_nodes(
            self.builder.record_query(fmt),
            **self.source_vars,
            identifier=identifier,
            withPayload="true",
        )
        if not rows:
            raise ProtocolError("idDoesNotExist", f"unknown identifier: {identifier}")
        node = ET.Element(q("GetRecord"))
        # GetRecordType is <sequence><element name="record"/></sequence>: the
        # header and metadata are wrapped, never bare under the verb.
        record = _el(node, "record")
        record.append(header_el(rows[0]))
        if rows[0].get("status") != "deleted":
            record.append(self._metadata(rows[0], fmt))
        return node

    def _metadata(self, row: ET.Element, fmt) -> ET.Element:
        md = ET.Element(q("metadata"))
        for child in list(row):
            md.append(child)
        return md

    async def _list(self, request: Request, verb: str) -> ET.Element:
        """Shared implementation of ListIdentifiers and ListRecords."""
        with_payload = verb == "ListRecords"
        total = 0

        if request.token:
            # A resumption request carries NO metadataPrefix - the spec makes
            # the token exclusive - so the format must come from the token.
            # Resolving it from the arguments first rejects every resume.
            state = decode_token(
                request.token,
                self.config.protocol.token_secret,
                self.builder.fingerprint(),
                self.config.protocol.token_ttl,
            )
            fmt = self.format_for(state.prefix)
            from_ = state.from_
            set_spec = state.set_spec
            pinned = state.pinned_until
            after_ds, after_id = state.last_datestamp, state.last_identifier
            delivered = state.delivered
            total = state.complete_list_size
        else:
            fmt = self.format_for(request.metadata_prefix)
            raw_from = request.args.get("from", "")
            raw_until = request.args.get("until", "")
            for name, value in (("from", raw_from), ("until", raw_until)):
                if value and not _is_valid_ts(value, self.granularity):
                    raise ProtocolError(
                        "badArgument",
                        f"{name} is not a valid datestamp for granularity "
                        f"{self.granularity}: {value}",
                    )
            from_ = normalise_from(raw_from, self.granularity) if raw_from else ""
            requested_until = (
                normalise_until(raw_until, self.granularity) if raw_until else ""
            )
            set_spec = request.args.get("set", "")
            if set_spec:
                known = {r.spec for r in self.set_rules}
                if set_spec not in known:
                    # unknown set: noRecordsMatch with the right message beats
                    # a silent empty page
                    raise ProtocolError("noRecordsMatch", f"unknown set: {set_spec}")
            # pin the window so a re-ingest cannot move records under the cursor
            now = _fmt(utcnow(), self.granularity)
            pinned = min(requested_until, now) if requested_until else now
            after_ds = after_id = ""
            delivered = 0
            # The total is NOT counted here: the first page is asked for it and
            # counts it from the scan it already runs. See fetch_page.

        fresh = not request.token
        page = await self.fetch_page(
            set_spec=set_spec,
            from_=from_,
            until=pinned,
            after_ds=after_ds,
            after_id=after_id,
            with_payload=with_payload,
            fmt=fmt,
            want_total=fresh,
        )
        if fresh:
            total = page.complete_list_size

        if not page.rows and not request.token:
            raise ProtocolError(
                "noRecordsMatch", "no records match the request"
            )

        node = ET.Element(q(verb))
        for row in page.rows:
            # ListRecordsType is <sequence><element name="record" maxOccurs=
            # "unbounded"/></sequence>, so a record wraps its header and
            # metadata. ListIdentifiersType instead takes <header> directly -
            # hence the branch, not two code paths.
            if with_payload:
                record = _el(node, "record")
                record.append(header_el(row))
                if row.get("status") != "deleted":
                    record.append(self._metadata(row, fmt))
            else:
                node.append(header_el(row))

        at_end = not page.has_more
        if page.rows:
            delivered += len(page.rows)
            if at_end:
                # empty token element signals completion
                _el(
                    node,
                    "resumptionToken",
                    completeListSize=str(total),
                    cursor=str(max(delivered - len(page.rows), 0)),
                ).text = None
            else:
                state = TokenState(
                    fingerprint=self.builder.fingerprint(),
                    pinned_until=pinned,
                    last_datestamp=page.last_datestamp,
                    last_identifier=page.last_identifier,
                    prefix=fmt.prefix,
                    issued_at=int(time.time()),
                    delivered=delivered,
                    complete_list_size=total,
                    set_spec=set_spec,
                    from_=from_,
                )
                _el(
                    node,
                    "resumptionToken",
                    completeListSize=str(total),
                    cursor=str(delivered),
                ).text = encode_token(state, self.config.protocol.token_secret)
        return node

    async def list_identifiers(self, request: Request) -> ET.Element:
        return await self._list(request, "ListIdentifiers")

    async def list_records(self, request: Request) -> ET.Element:
        return await self._list(request, "ListRecords")

    # -- entry point -----------------------------------------------------

    async def handle(self, pairs: list[tuple[str, str]]) -> ET.Element:
        request_el = make_request_el(self.config.identity.base_url, pairs)
        try:
            request = parse_args(pairs)
        except ProtocolError as exc:
            return error_response(request_el, exc, self.granularity)

        handler = {
            "Identify": self.identify,
            "ListMetadataFormats": self.list_metadata_formats,
            "ListSets": self.list_sets,
            "GetRecord": self.get_record,
            "ListIdentifiers": self.list_identifiers,
            "ListRecords": self.list_records,
        }[request.verb]

        try:
            verb_el = await handler(request)
        except ProtocolError as exc:
            return error_response(request_el, exc, self.granularity)

        root = ET.Element(q("OAI-PMH"))
        root.set(
            f"{{{XSI_NS}}}schemaLocation",
            f"{OAI_NS} http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd",
        )
        _el(root, "responseDate").text = _fmt(utcnow(), self.granularity)
        root.append(request_el)
        root.append(verb_el)
        return root


def _pretty_print(elem: ET.Element, level: int = 0, space: str = "  ") -> None:
    """Indent an element tree for human reading, in place.

    It **stops at a `<metadata>` element**: that content is the served payload,
    kept verbatim, so inserting whitespace text nodes there would change it.
    Everything else is ours to format, and an XML parser ignores the whitespace
    either way.
    """
    if elem.tag == q("metadata"):
        return
    children = list(elem)
    if not children:
        return
    pad = "\n" + space * (level + 1)
    for child in children:
        _pretty_print(child, level + 1, space)
    if elem.text is None or not elem.text.strip():
        elem.text = pad
    for i, child in enumerate(children):
        if child.tail is not None and child.tail.strip():
            continue
        child.tail = pad if i < len(children) - 1 else "\n" + space * level


def serialise(root: ET.Element, pretty: bool = False) -> bytes:
    if pretty:
        _pretty_print(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="utf-8"
    )

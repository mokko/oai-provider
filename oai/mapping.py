"""Renders the XQuery templates from the mapping.

The extraction work belongs in BaseX, not in Python: XPath 3.1, in-process
with the data, no lxml, no ElementTree XPath-1.0 subset. Python's job is to
turn config into query text and to report what happened.

Only structural, trusted values (XPaths, prefix declarations) are
substituted into the query. Everything that comes from a user or a file
name - database, path, id prefix, timestamps - is passed as an XQuery
external variable, so it cannot break out of the query.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import ENVELOPE_PREFIX, Mapping

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "xq"

# The MuseumPlus/RIA namespace. Module mode stores records WITHOUT namespaces,
# but an XSLT written for RIA matches namespace-qualified names
# (`z:module[@name='Object']`), so the input handed to a transform is rebuilt
# with this namespace - the reverse of the ingest's local:strip().
ZETCOM_NS = "http://www.zetcom.com/ria/ws/module"

# Re-namespace a stored record for an XSLT that expects RIA. Attribute names are
# kept as they are: module ingest only ever dropped ELEMENT namespaces in
# practice, and an unprefixed attribute belongs to no namespace either way.
ZETCOM_FUNCTION = (
    "declare function local:zetcom($n as node()?) as node()? {\n"
    "  typeswitch ($n)\n"
    "    case element() return\n"
    "      element { QName('" + ZETCOM_NS + "', local-name($n)) } {\n"
    "        $n/@*,\n"
    "        ($n/node() ! local:zetcom(.))\n"
    "      }\n"
    "    case document-node() return document { $n/node() ! local:zetcom(.) }\n"
    "    default return $n\n"
    "};"
)

# MPX/XQuery date normalisation: source timestamps are local wall clock with
# a space separator and no zone ("2021-09-07 10:56:34.615"). OAI requires a
# canonical UTC "YYYY-MM-DDThh:mm:ssZ". Appending Z to a local time is wrong
# by the offset, so declare the offset and shift.
OAI_DATE_FUNCTION = '''
declare function local:oaiDate($raw as xs:string?, $offset as xs:string)
    as xs:string? {
  let $s := normalize-space($raw)
  return if ($s = '') then ()
    else
      let $iso := replace($s,
        '^(\\d{4}-\\d{2}-\\d{2})[ T](\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?)$', '$1T$2')
      return
        try {
          format-dateTime(
            adjust-dateTime-to-timezone(
              xs:dateTime(concat($iso, $offset)),
              xs:dayTimeDuration('PT0S')),
            '[Y0001]-[M01]-[D01]T[H01]:[m01]:[s01]Z')
        } catch * { () }
};
'''.strip()


def _xq_string(value: str) -> str:
    """A single-quoted XQuery string literal.

    XQuery escapes a quote inside a single-quoted literal by doubling it, so
    this is what keeps a config-authored value from ending the literal early.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _xq_text(value: str) -> str:
    """Text for a literal element's content, XML-escaped."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class QueryBuilder:
    def __init__(
        self,
        mapping: Mapping,
        modules: tuple = (),
        template_dir: Path = TEMPLATE_DIR,
    ):
        self.mapping = mapping
        self.modules = tuple(modules)
        self.template_dir = Path(template_dir)

    # -- the source of rows ----------------------------------------------

    def uses_modules(self) -> bool:
        """Whether this mapping reads the per-module databases.

        With [[modules]] configured the provider serves the module databases
        (the colleague's `sync_*` layout); without them it serves the single
        enveloped database. The two are normalized to the same row shape here,
        so nothing downstream - paging, the cursor, the token - has to know
        which one it is talking to.
        """
        return bool(self.modules)

    def modules_for(self, fmt=None) -> tuple:
        """The modules a request actually spans.

        A format may serve a subset - LIDO is object-centric, so persons and
        assets are not LIDO records and must not appear in its result set. That
        subset IS the result set: paging, the cursor and completeListSize all
        work off these, so a `ListRecords&metadataPrefix=lido` pages objects
        only.
        """
        if not self.modules:
            return ()
        if fmt is not None and fmt.modules:
            allowed = set(fmt.modules)
            return tuple(m for m in self.modules if m.name in allowed)
        return self.modules

    def source_expr(self, fmt=None) -> str:
        """Header rows: datestamp, identifier, status, sets - and **never a
        payload**.

        Paging orders these, and the sort must not carry records it is about to
        discard. Measured before the split: `ListRecords` cost the same for a
        page of 1 as for a page of 100 (6.5s), because every one of the 5,884
        matching payloads was attached before the page was chosen and sorted
        away. The payload is attached afterwards, for the page alone - see
        `payload_expr`.

        Rows live in NO namespace, which is what lets the page, count and
        record templates be identical for both storage modes.
        """
        modules = self.modules_for(fmt)
        if modules:
            return self._module_source(modules)
        if self.modules:
            # the format restricts to a module set that produces nothing
            return "()"
        return self._envelope_source()

    # -- the payload, fetched for the page and nothing else ---------------

    def payload_expr(self, fmt=None) -> str:
        """`<p id="…">payload</p>` for exactly the records in `$wanted`.

        `$wanted` is the page's rows: one scan per module, filtered to those
        identifiers, instead of a payload attached to every match. A derived
        format (oai_dc) is built here rather than in the source, so it too is
        assembled only for what is served.
        """
        if self.modules:
            blocks = ",\n".join(self._module_payload(m, fmt) for m in self.modules)
            return f"(\n{blocks}\n)"
        return self._envelope_payload(fmt)

    def _envelope_payload(self, fmt=None) -> str:
        env = ENVELOPE_PREFIX
        # In the enveloped store the record is INSIDE env:source, so terms are
        # relative to that element, not to the envelope itself.
        base = f"$d/{env}:source"
        body = self._metadata_branch(
            fmt, fmt.terms if fmt else (), f"{base}/node()", base
        )
        return (
            f"for $d in collection($db)/{env}:record\n"
            f"let $id := string($d/@{env}:identifier)\n"
            f"where $id = $wanted/@identifier\n"
            f"  and string($d/@{env}:status) ne 'deleted'\n"
            f'return <p id="{{$id}}">{{ {body} }}</p>'
        )

    def _module_payload(self, module, fmt=None) -> str:
        if fmt is not None and fmt.kind == "xslt":
            return self._xslt_payload(module, fmt)
        terms = module.terms or (fmt.terms if fmt else ())
        body = self._metadata_branch(fmt, terms, "$src", "$src")
        prefix = _xq_string(module.identifier_prefix)
        return (
            f"for $src in collection({_xq_string(module.database)})"
            f"{module.records_xpath()}\n"
            f"let $id := concat({prefix}, string($src/{module.identifier}))\n"
            f"where $id = $wanted/@identifier\n"
            f'return <p id="{{$id}}">{{ {body} }}</p>'
        )

    # -- kind="xslt": rebuild the record's world, then transform ----------

    def _module_by_name(self, name: str):
        for module in self.modules:
            if module.name == name:
                return module
        return None

    def _related_forward(self, other, ref_name: str, id_var: str = "$objId") -> str:
        """Records of `other` that THIS record points at.

        `ObjPerAssociationRef` on an object names person ids; the stylesheet
        resolves them as `module[@name='Person']/moduleItem[@id = $kueId]`, so
        the people it needs must be present in the input document.
        """
        return (
            f"for $oth in collection({_xq_string(other.database)})"
            f"{other.records_xpath()}\n"
            f"  where string($oth/{other.identifier}) = "
            f"$src//moduleReference[@name='{ref_name}']"
            "//moduleReferenceItem/@moduleItemId\n"
            f"  return local:zetcom($oth)"
        )

    def _related_reverse(self, other, ref_name: str) -> str:
        """Records of `other` that point BACK at this record.

        An asset carries `MulObjectRef` naming the object it documents, and
        resourceWrap selects the assets that way - a reverse lookup, which is
        why the input has to be assembled from the other database rather than
        read off the record.
        """
        return (
            f"for $oth in collection({_xq_string(other.database)})"
            f"{other.records_xpath()}\n"
            f"  where some $r in $oth"
            f"//moduleReference[@name='{ref_name}']"
            "/moduleReferenceItem/@moduleItemId satisfies string($r) = $objId\n"
            f"  return local:zetcom($oth)"
        )

    def _xslt_payload(self, module, fmt) -> str:
        """Assemble one record's input document and transform it.

        The stylesheet is not a per-record mapping: it reads the whole
        `application/modules` tree. So the record's world is rebuilt here -
        itself, the persons it names, the assets that name it, and the objects
        it relates to - and handed to `xslt:transform` whole. The transform runs
        inside BaseX, so none of this leaves the store.
        """
        prefix = _xq_string(module.identifier_prefix)
        related: list[str] = []
        people = self._module_by_name("Person")
        if people is not None:
            related.append(
                '<module name="Person">{ '
                + self._related_forward(people, "ObjPerAssociationRef")
                + " }</module>"
            )
        media = self._module_by_name("Multimedia")
        if media is not None:
            related.append(
                '<module name="Multimedia">{ '
                + self._related_reverse(media, "MulObjectRef")
                + " }</module>"
            )
        # related works point at other objects of the SAME module
        same = module.identifier
        related.append(
            f'<module name="{module.name}">{{\n'
            f"    for $oth in collection({_xq_string(module.database)})"
            f"{module.records_xpath()}\n"
            f"      where string($oth/{same}) = $src//composite[@name='ObjObjectCre']"
            "//moduleReferenceItem/@moduleItemId\n"
            f"         or string($oth/{same}) = $src//moduleReference"
            "[@name='ObjLiteratureRef']//moduleReferenceItem/@moduleItemId\n"
            "      return local:zetcom($oth)\n"
            "  }</module>"
        )
        # **`xslt:transform` returns a DOCUMENT node**, not the root element, so
        # the record sits below it ($out/lidoWrap/lido). Descending from the
        # result is what makes this independent of whether a given BaseX/Saxon
        # pair hands back a document or an element.
        record_select = f"$out//{fmt.record}" if fmt.record else "$out"
        return (
            f"for $src in collection({_xq_string(module.database)})"
            f"{module.records_xpath()}\n"
            f"let $id := concat({prefix}, string($src/{module.identifier}))\n"
            "where $id = $wanted/@identifier\n"
            f"let $objId := string($src/{module.identifier})\n"
            "let $input :=\n"
            '  <application xmlns="' + ZETCOM_NS + '">\n'
            "    <modules>{\n"
            f'      <module name="{module.name}">{{ local:zetcom($src) }}</module>,\n'
            + ",\n".join("      " + r for r in related)
            + "\n    }</modules>\n"
            "  </application>\n"
            f"let $out := xslt:transform($input, {_xq_string(fmt.stylesheet)})\n"
            f'return <p id="{{$id}}">{{ {record_select} }}</p>'
        )

    # -- what goes inside <metadata> -------------------------------------

    def _metadata_branch(self, fmt, terms, fallback: str, base: str = "$r") -> str:
        """The expression that becomes a row's child.

        `base` names the variable holding the record: `$r` in the source,
        `$src`/`$d/env:source` in the payload phase - the term XPaths are
        relative to *that*, and a stale variable name is an XPST0008 at query
        time rather than anything a unit test would catch.
        """
        if fmt is not None and fmt.kind == "derived":
            inner = self._term_exprs(terms or fmt.terms, base)
            # **The terms go in an enclosed expression, not bare inside the
            # element.** A direct element constructor treats everything between
            # the tags as text unless it is inside { }, so an unbraced `for $v
            # ... return` would be literal text and its $v undeclared.
            return f"<{fmt.wrapper}>{{ {inner} }}</{fmt.wrapper}>"
        return fallback

    def _term_exprs(self, terms, base: str = "$r") -> str:
        """One expression per DC term, in the order configured.

        A term whose XPath selects nothing - or selects an empty `<value/>` -
        emits **nothing at all**, which is the difference between "correct
        Dublin Core" and an element full of blanks.
        """
        parts: list[str] = []
        for rule in terms:
            if rule.literal:
                parts.append(f"<{rule.term}>{_xq_text(rule.literal)}</{rule.term}>")
                continue
            # relative to the record; a leading / means "search down from it"
            path = (
                f"{base}{rule.xpath}"
                if rule.xpath.startswith("/")
                else f"{base}/{rule.xpath}"
            )
            parts.append(
                f"for $v in ({path})[normalize-space(string(.)) ne '']\n"
                f"        return <{rule.term}>{{string($v)}}</{rule.term}>"
            )
        return ",\n        ".join(parts)

    def format_namespace_declarations(self, fmt) -> str:
        """Prolog declarations for a derived format's wrapper and terms.

        Required, not cosmetic: a prefix in a query resolves against the
        query's own prolog, and the stored payload has no namespaces of its own
        to lend.
        """
        if fmt is None or not fmt.namespaces:
            return ""
        return "\n".join(
            f'declare namespace {p} = "{uri}";'
            for p, uri in sorted(fmt.namespaces.items())
        )

    def _envelope_source(self) -> str:
        env = ENVELOPE_PREFIX
        return (
            f"for $r in collection($db)/{env}:record\n"
            "return\n"
            f'  <row datestamp="{{$r/@{env}:datestamp}}"\n'
            f'       identifier="{{$r/@{env}:identifier}}"\n'
            f'       status="{{string($r/@{env}:status)}}"\n'
            f'       sets="{{string-join($r/{env}:set, \' \')}}"/>'
        )

    def _module_source(self, modules) -> str:
        blocks = ",\n".join(self._module_block(m) for m in modules)
        return f"(\n{blocks}\n)"

    def _module_block(self, module) -> str:
        records = module.records_xpath()
        ident = module.identifier
        ds = module.datestamp
        prefix = _xq_string(module.identifier_prefix)
        sets = self._set_expressions(module.sets)
        return (
            f"for $r in collection({_xq_string(module.database)}){records}\n"
            f"let $raw := string($r/{ident})\n"
            f"let $ds := local:oaiDate(string($r/{ds}), $tzOffset)\n"
            f"let $sets := distinct-values(({sets}))\n"
            "where $raw ne '' and $ds\n"
            "return\n"
            '  <row datestamp="{$ds}"\n'
            f'       identifier="{{concat({prefix}, $raw)}}"\n'
            '       status=""\n'
            '       sets="{string-join($sets, \' \')}"/>'
        )


    # -- pieces -----------------------------------------------------------

    @staticmethod
    def _spec_literal(spec: str) -> str:
        return "'" + spec.replace("'", "&apos;") + "'"

    def _set_expressions(self, rules) -> str:
        """One allow-list test per set rule, against the record `$r`.

        The XPath is a membership predicate and its value is ignored: the
        setSpec is the string written in config, so nothing from the record
        can appear in OAI output.
        """
        if not rules:
            return ""
        parts = [
            f"if (exists($r/{rule.xpath})) "
            f"then {self._spec_literal(rule.spec)} else ()"
            for rule in rules
        ]
        return ",\n    ".join(parts)

    def set_expressions(self) -> str:
        """Set membership for the enveloped mapping (rooted at $r)."""
        return self._set_expressions(self.mapping.sets)

    def identifier_expr(self) -> str:
        return f"string({self.mapping.resolve(self.mapping.identifier)})"

    def datestamp_expr(self) -> str:
        return self.mapping.resolve(self.mapping.datestamp)

    def record_expr(self) -> str:
        return self.mapping.resolve(self.mapping.records)

    # -- rendering --------------------------------------------------------

    def render(self, template: str, fmt=None, **extra: str) -> str:
        text = (self.template_dir / template).read_text(encoding="utf-8")
        subs = {
            "NAMESPACES": self.mapping.namespace_declarations(),
            "FORMAT_NAMESPACES": self.format_namespace_declarations(fmt),
            "ENVELOPE_PREFIX": ENVELOPE_PREFIX,
            "OAI_DATE_FUNCTION": OAI_DATE_FUNCTION,
            "RECORD_XPATH": self.record_expr(),
            "IDENTIFIER_EXPR": self.identifier_expr(),
            "DATESTAMP_EXPR": self.datestamp_expr(),
            "SET_EXPRESSIONS": self.set_expressions(),
            "SOURCE": self.source_expr(fmt),
            "PAYLOADS": self.payload_expr(fmt),
            "ZETCOM_FUNCTION": ZETCOM_FUNCTION,
            "TZOFFSET": self.mapping.timezone_offset,
            **extra,
        }
        for key, value in subs.items():
            text = text.replace("{{" + key + "}}", str(value))
        if "{{" in text:
            leftover = text[text.index("{{") : text.index("{{") + 40]
            raise ValueError(f"unsubstituted placeholder near: {leftover!r}")
        return text

    def ingest_query(self) -> str:
        return self.render("ingest.xq.tmpl")

    def validate_query(self) -> str:
        return self.render("validate.xq.tmpl")

    def reconcile_query(self) -> str:
        return self.render("reconcile.xq.tmpl")

    def stale_query(self) -> str:
        return self.render("stale.xq.tmpl")

    def page_query(self, fmt=None) -> str:
        return self.render("page.xq.tmpl", fmt=fmt)

    def count_query(self, fmt=None) -> str:
        return self.render("count.xq.tmpl", fmt=fmt)

    def record_query(self, fmt=None) -> str:
        return self.render("record.xq.tmpl", fmt=fmt)

    def module_ingest_query(self) -> str:
        """Ingest one module into its own database (see the template)."""
        return self.render("module_ingest.xq.tmpl")

    def module_count_query(self) -> str:
        """Read-only record count for one module of a dump."""
        return self.render("module_count.xq.tmpl")

    def fingerprint(self) -> str:
        """A digest of the mapping, so a resumption token issued under one
        mapping is not honoured under another.

        Changing this is exactly the case the token fingerprint exists to
        catch: the same request arguments stop meaning the same result set.
        """
        parts = [
            self.mapping.records,
            self.mapping.identifier,
            self.mapping.identifier_prefix,
            self.mapping.datestamp,
            self.mapping.timezone_offset,
            ",".join(f"{p}={u}" for p, u in sorted(self.mapping.namespaces.items())),
            ";".join(f"{r.spec}|{r.xpath}" for r in self.mapping.sets),
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

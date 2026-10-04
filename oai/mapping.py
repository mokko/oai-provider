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

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "xq"

# The MuseumPlus/RIA namespace. Module mode stores records WITHOUT namespaces,
# but an XSLT written for RIA matches namespace-qualified names
# (`z:module[@name='Object']`), so the input handed to a transform is rebuilt
# with this namespace - the reverse of the ingest's local:strip().
ZETCOM_NS = "http://www.zetcom.com/ria/ws/module"

# LIDO's namespace, the one the served LIDO records and the related-works pass
# speak.
LIDO_NS = "http://www.lido-schema.org"

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


RELATED_WORKS_NAMESPACES = 'declare namespace z = "http://www.zetcom.com/ria/ws/module";'

# The *functions* may sit with the other function declarations; the prefix
# above may not - XQuery's prolog requires namespace declarations before any
# function declaration, so it goes in the template's NAMESPACES slot, which
# leads the prolog. The LIDO prefix is not declared here: the format declares it
# already (validation insists it is bound to `lido`), and declaring it twice is
# an error (XQST0033).
RELATED_WORKS_FUNCTION = '''
(: Where the object is held, as the institution's own display string. It is the
   key into vocmap's `verwaltendeInstitution` concept. :)
declare function local:verwaltendeInstitution($item as node()?) as xs:string {
  normalize-space(string($item//z:moduleReference[@name='ObjOwnerRef']//z:formattedValue))
};

(: The same publication predicate zml2lido's relWorksCache used, read off the
   target record instead of asking RIA: ObjPublicationGrp with PublicationVoc
   "Ja" *and* the TypeVoc item for SMB-digital. :)
declare function local:isOnline($item as node()?) as xs:boolean {
  exists($item//z:repeatableGroup[@name='ObjPublicationGrp']/z:repeatableGroupItem[
    z:vocabularyReference[@name='PublicationVoc']/z:vocabularyReferenceItem/@name = 'Ja'
    and z:vocabularyReference[@name='TypeVoc']/z:vocabularyReferenceItem/@name =
        'Daten freigegeben für SMB-digital'])
};

(: The ISIL for a holding institution, out of vocmap.xml. `$vocmapPath` is an
   absolute path bound in from config: a relative doc() would resolve against the
   query's static base URI, which for a query sent over REST is not the JVM
   working directory. :)
declare function local:isil($vocmapPath as xs:string?, $verw as xs:string?) as xs:string? {
  if (empty($vocmapPath) or empty($verw)) then () else
  doc($vocmapPath)/vocmap/voc[@name='verwaltendeInstitution']
    /concept[source = $verw]/target[@name='ISIL']/string()
};

(: Keep only related works that are present in this store, and give the ones we
   can an ISIL source. Objects only: LIT.ID is left alone (there is no
   Literature module here) and KUE.ID/MM.ID likewise - see
   todo/related-works-online.md for why those are an open question rather than
   an oversight. An object can be online yet have no ISIL: such a set is kept,
   and left with @source=OBJ.ID rather than half-rewritten. :)
declare function local:fixRelatedWorks($out as node()?, $online as map(*), $isil as map(*))
    as node()? {
  (: the objectID is element TEXT, so it carries the serialiser's whitespace -
     normalise before comparing it with the attribute-valued map keys, or every
     target reads as offline. :)
  let $dropped := copy $o := $out
    modify (for $s in $o//lido:relatedWorkSet[
              string(lido:relatedWork/lido:object/lido:objectID/@lido:source) = 'OBJ.ID']
              [not(map:contains($online,
                   normalize-space(string(lido:relatedWork/lido:object/lido:objectID))))]
            return delete node $s)
    return $o
  let $rewritten := copy $o := $dropped
    modify (
      for $oid in $o//lido:relatedWorkSet[
            string(lido:relatedWork/lido:object/lido:objectID/@lido:source) = 'OBJ.ID']
            [map:contains($isil,
             normalize-space(string(lido:relatedWork/lido:object/lido:objectID)))]
            /lido:relatedWork/lido:object/lido:objectID
      let $id := normalize-space(string($oid))
      return replace node $oid with
        element { node-name($oid) } {
          $oid/@* except $oid/@lido:source,
          attribute { QName('http://www.lido-schema.org', 'source') } { 'ISIL/ID' },
          text { $isil($id) || '/' || $id }
        }
    )
    return $o
  return copy $o := $rewritten
    modify (for $w in $o//lido:relatedWorksWrap[not(lido:relatedWorkSet)] return delete node $w)
    return $o
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
        modules: tuple = (),
        timezone_offset: str = "+00:00",
        template_dir: Path = TEMPLATE_DIR,
    ):
        self.modules = tuple(modules)
        self.timezone_offset = timezone_offset
        self.template_dir = Path(template_dir)

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
        if not modules:
            # the format restricts to a module set that produces nothing
            return "()"
        return self._module_source(modules)

    # -- the payload, fetched for the page and nothing else ---------------

    def payload_expr(self, fmt=None) -> str:
        """`<p id="…">payload</p>` for exactly the records in `$wanted`.

        `$wanted` is the page's rows: one scan per module, filtered to those
        identifiers, instead of a payload attached to every match. A derived
        format (oai_dc) is built here rather than in the source, so it too is
        assembled only for what is served.
        """
        blocks = ",\n".join(
            self._module_payload(m, fmt) for m in self.modules_for(fmt)
        )
        return f"(\n{blocks}\n)"

    def terms_for(self, module, fmt) -> tuple:
        """The DC terms that apply to one module's records.

        A module may carry its own [[modules.terms]]; otherwise it takes the
        format's terms scoped to it (`module = "<Name>"`) or unscoped (shared by
        every module). Resolved here so that the query only ever carries the
        rules for the record in hand.
        """
        if module.terms:
            return module.terms
        if fmt is None or fmt.kind != "derived":
            return ()
        return tuple(t for t in fmt.terms if t.module in ("", module.name))

    def _module_payload(self, module, fmt=None) -> str:
        if fmt is not None and fmt.kind == "xslt":
            return self._xslt_payload(module, fmt)
        terms = self.terms_for(module, fmt)
        # A passthrough format serves the stored document, and module ingest
        # stripped it of namespaces. OAI-PMH's <metadata> is declared
        # `<any namespace="##other" processContents="strict"/>`, and `##other`
        # excludes the *absent* namespace - so a namespace-free payload is not
        # admissible at all, however well-formed. Re-namespace it for the wire,
        # exactly as the XSLT path does. The **store** stays stripped: this is a
        # serve-time rebuild, not an ingest change.
        #
        # The RIA record is the whole `application` document
        # (application/modules/module/moduleItem); `moduleItem` is a LOCAL
        # element, so a payload rooted at it is not the record and does not
        # resolve in Zetcom's schema. Serve the whole document.
        #
        # This is the module path, which is RIA-shaped by construction (see
        # MetadataFormat's note): the module element below is MuseumPlus's, not
        # something the generic passthrough mechanism introduces.
        #
        # `totalSize` is the number of records THIS document holds - the store
        # keeps one record per document, so 1, not the source file's count. It
        # is the single site: ingest does not copy it. Always written, so a
        # document carries a truthful total whether or not the store had one.
        if fmt is None or fmt.kind == "passthrough":
            body = (
                "local:zetcom("
                "copy $app := $src/ancestor::application "
                "modify (for $m in $app/modules/module return ("
                "delete node $m/@totalSize, "
                "insert node attribute totalSize "
                "{ string(count($app//moduleItem)) } into $m)) "
                "return $app)"
            )
        else:
            body = self._metadata_branch(fmt, terms, "local:zetcom($src)", "$src")
        prefix = _xq_string(module.identifier_prefix)
        return (
            f"for $src in collection({_xq_string(module.database)})"
            f"{module.records_xpath()}\n"
            f"let $id := concat({prefix}, string($src/{module.identifier}))\n"
            f"where $id = $wanted/@identifier\n"
            f'return <p id="{{$id}}">{{ {body} }}</p>'
        )

    # -- kind="xslt": rebuild the record's world, then transform ----------

    def _zetcom_prefix(self, fmt) -> str:
        """A prefix bound to the RIA namespace inside the generated query.

        The transform input needs one: with a *default* namespace on the input
        constructor, every unprefixed name test in its enclosed expressions would
        resolve against RIA and match nothing.
        """
        for p, uri in (fmt.namespaces if fmt is not None else {}).items():
            if uri == ZETCOM_NS:
                return p
        return "z"

    def _module_by_name(self, name: str):
        for module in self.modules:
            if module.name == name:
                return module
        return None

    def _related_forward(self, other, ref_name: str) -> str:
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
        # The wrapper carries the RIA namespace as a **prefix**, never as a
        # default namespace: an `xmlns="…"` on the constructor would apply to the
        # enclosed expressions inside it, so every unprefixed name test in them
        # (`$src//composite[@name='ObjObjectCre']`, and the `records_xpath()` of
        # each related lookup) would resolve against the RIA namespace and match
        # nothing - the stored records are namespace-stripped, so the whole
        # related-world assembly silently came out empty.
        p = self._zetcom_prefix(fmt)
        related: list[str] = []
        people = self._module_by_name("Person")
        if people is not None:
            related.append(
                f'<{p}:module name="Person">{{ '
                + self._related_forward(people, "ObjPerAssociationRef")
                + " }</" + p + ":module>"
            )
        media = self._module_by_name("Multimedia")
        if media is not None:
            related.append(
                f'<{p}:module name="Multimedia">{{ '
                + self._related_reverse(media, "MulObjectRef")
                + " }</" + p + ":module>"
            )
        # related works point at other objects of the SAME module
        same = module.identifier
        related.append(
            f'<{p}:module name="{module.name}">{{\n'
            f"    for $oth in collection({_xq_string(module.database)})"
            f"{module.records_xpath()}\n"
            f"      where string($oth/{same}) = $src//composite[@name='ObjObjectCre']"
            "//moduleReferenceItem/@moduleItemId\n"
            f"         or string($oth/{same}) = $src//moduleReference"
            "[@name='ObjLiteratureRef']//moduleReferenceItem/@moduleItemId\n"
            "      return local:zetcom($oth)\n"
            "  }</" + p + ":module>"
        )
        # **`xslt:transform` returns a DOCUMENT node**, not the root element, so
        # the record sits below it ($out/lidoWrap/lido). Descending from the
        # result is what makes this independent of whether a given BaseX/Saxon
        # pair hands back a document or an element.
        # The input holds the record's *world*, not just the record, so the
        # stylesheet emits a lido per object and the record's own must be picked
        # out. Its lidoRecID ends in "/<id>" (the stylesheet writes ISIL/ID);
        # the bare-id case is allowed too, so a record without an ISIL still
        # serves rather than silently disappearing.
        if fmt.record:
            rec_prefix = fmt.record.split(":", 1)[0]
            record_select = (
                f"$out//{fmt.record}["
                f"ends-with(normalize-space({rec_prefix}:lidoRecID), "
                f"concat('/', $objId))"
                f" or normalize-space({rec_prefix}:lidoRecID) = $objId]"
            )
        else:
            record_select = "$out"
        prune = ""
        if fmt.related_works_online_only:
            # Related works: keep only the targets held in this store, and give
            # the ones with an ISIL an ISIL-based source. `$out` cannot be
            # rebound inside the same FLWOR, so the fixed tree is `$fixed` and
            # the record is taken from that. Rule and rationale:
            # todo/related-works-online.md.
            record_select = record_select.replace("$out", "$fixed")
            prune = (
                # $input IS the <application> element (a constructor, not a
                # document), so the module list hangs directly off it - one
                # level shallower than in the recipe, whose $input was a
                # document node.
                # The input was built with the prefix `_zetcom_prefix` chose, so
                # the prune must use the same one - a hardcoded `z:` would match
                # nothing if the format binds RIA to another prefix.
                f"let $targets := $input/{p}:modules"
                f"/{p}:module[@name='Object']/{p}:moduleItem\n"
                "let $online := map:merge(\n"
                "  for $m in $targets where local:isOnline($m)\n"
                "  return map:entry(string($m/@id), true()))\n"
                "let $isil := map:merge(\n"
                "  for $m in $targets\n"
                "  let $i := local:isil($vocmap, local:verwaltendeInstitution($m))\n"
                "  where local:isOnline($m) and exists($i)\n"
                "  return map:entry(string($m/@id), $i))\n"
                "let $fixed := local:fixRelatedWorks($out, $online, $isil)\n"
            )
        return (
            f"for $src in collection({_xq_string(module.database)})"
            f"{module.records_xpath()}\n"
            f"let $id := concat({prefix}, string($src/{module.identifier}))\n"
            "where $id = $wanted/@identifier\n"
            f"let $objId := string($src/{module.identifier})\n"
            "let $input :=\n"
            f'  <{p}:application xmlns:{p}="{ZETCOM_NS}">\n'
            f"    <{p}:modules>{{\n"
            f'      <{p}:module name="{module.name}">{{ local:zetcom($src) }}</{p}:module>,\n'
            + ",\n".join("      " + r for r in related)
            + f"\n    }}</{p}:modules>\n"
            f"  </{p}:application>\n"
            f"let $out := xslt:transform($input, {_xq_string(fmt.stylesheet)})\n"
            + prune
            + f'return <p id="{{$id}}">{{ {record_select} }}</p>'
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
            inner = self._term_exprs(terms, base)
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
            if rule.expression:
                # An expression, not a path: it may have to compose several
                # fields, which no path can do. `$record` marks the record, so
                # the expression stays independent of the variable name the
                # template happens to use ($src in the payload phase).
                source = rule.expression.replace("$record", base)
            else:
                # relative to the record; a leading / means "search down from it"
                source = (
                    f"{base}{rule.xpath}"
                    if rule.xpath.startswith("/")
                    else f"{base}/{rule.xpath}"
                )
            parts.append(
                f"for $v in ({source})[normalize-space(string(.)) ne '']\n"
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

    # -- rendering --------------------------------------------------------

    def render(self, template: str, fmt=None, **extra: str) -> str:
        text = (self.template_dir / template).read_text(encoding="utf-8")
        subs = {
            # Module mode stores namespace-stripped records, so a generated
            # query declares no prefixes of its own - the placeholder is empty.
            # The one exception is a format that resolves related works, whose
            # functions need the RIA and LIDO prefixes; those declarations must
            # lead the prolog, before any function declaration.
            "NAMESPACES": (
                RELATED_WORKS_NAMESPACES
                if fmt is not None and fmt.related_works_online_only
                else ""
            ),
            "FORMAT_NAMESPACES": self.format_namespace_declarations(fmt),
            "OAI_DATE_FUNCTION": OAI_DATE_FUNCTION,
            "SOURCE": self.source_expr(fmt),
            "PAYLOADS": self.payload_expr(fmt),
            "ZETCOM_FUNCTION": ZETCOM_FUNCTION
            + (
                "\n" + RELATED_WORKS_FUNCTION
                if fmt is not None and fmt.related_works_online_only
                else ""
            ),
            **extra,
        }
        for key, value in subs.items():
            text = text.replace("{{" + key + "}}", str(value))
        if "{{" in text:
            leftover = text[text.index("{{") : text.index("{{") + 40]
            raise ValueError(f"unsubstituted placeholder near: {leftover!r}")
        return text

    def page_query(self, fmt=None) -> str:
        return self.render("page.xq.tmpl", fmt=fmt)

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
        parts = [self.timezone_offset]
        for module in self.modules:
            parts += [
                module.name,
                module.database,
                module.identifier_prefix,
                module.records_xpath(),
                module.identifier,
                module.datestamp,
            ]
            parts += [f"{r.spec}|{r.xpath}" for r in module.sets]
            parts += [f"{t.term}|{t.xpath}|{t.literal}" for t in module.terms]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

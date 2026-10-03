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


class QueryBuilder:
    def __init__(self, mapping: Mapping, template_dir: Path = TEMPLATE_DIR):
        self.mapping = mapping
        self.template_dir = Path(template_dir)

    # -- pieces -----------------------------------------------------------

    @staticmethod
    def _spec_literal(spec: str) -> str:
        return "'" + spec.replace("'", "&apos;") + "'"

    def set_expressions(self) -> str:
        """One allow-list test per declared set.

        The XPath is a membership predicate and its value is ignored: the
        setSpec is the string we wrote in config, so nothing from the record
        can appear in OAI output.
        """
        if not self.mapping.sets:
            return ""
        parts = [
            f"if (exists({self.mapping.resolve(rule.xpath)})) "
            f"then {self._spec_literal(rule.spec)} else ()"
            for rule in self.mapping.sets
        ]
        return ",\n    ".join(parts)

    def identifier_expr(self) -> str:
        return f"string({self.mapping.resolve(self.mapping.identifier)})"

    def datestamp_expr(self) -> str:
        return self.mapping.resolve(self.mapping.datestamp)

    def record_expr(self) -> str:
        return self.mapping.resolve(self.mapping.records)

    # -- rendering --------------------------------------------------------

    def render(self, template: str, **extra: str) -> str:
        text = (self.template_dir / template).read_text(encoding="utf-8")
        subs = {
            "NAMESPACES": self.mapping.namespace_declarations(),
            "ENVELOPE_PREFIX": ENVELOPE_PREFIX,
            "OAI_DATE_FUNCTION": OAI_DATE_FUNCTION,
            "RECORD_XPATH": self.record_expr(),
            "IDENTIFIER_EXPR": self.identifier_expr(),
            "DATESTAMP_EXPR": self.datestamp_expr(),
            "SET_EXPRESSIONS": self.set_expressions(),
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

    def page_query(self) -> str:
        return self.render("page.xq.tmpl")

    def count_query(self) -> str:
        return self.render("count.xq.tmpl")

    def record_query(self) -> str:
        return self.render("record.xq.tmpl")

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

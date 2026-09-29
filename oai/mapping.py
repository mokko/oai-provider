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
    def _spec_expr(spec: str) -> str:
        """Turn a setSpec template into an XQuery expression.

        "{value}" alone is the common case; anything else is a prefix/suffix
        template and has to become a concat, not a string pasted next to the
        substring - "group-{value}" is not the function group-string().
        """
        if spec == "{value}":
            return "string($v)"
        head, _, tail = spec.partition("{value}")
        pieces = []
        if head:
            pieces.append(f"'{head.replace(chr(39), '&apos;')}'")
        pieces.append("string($v)")
        if tail:
            pieces.append(f"'{tail.replace(chr(39), '&apos;')}'")
        return "concat(" + ", ".join(pieces) + ")"

    def set_expressions(self) -> str:
        """One XQuery expression per set rule, producing setSpec strings."""
        if not self.mapping.sets:
            return ""
        parts = []
        for rule in self.mapping.sets:
            path = self.mapping.resolve(rule.xpath)
            parts.append(
                f"for $v in {path}"
                f" return normalize-space({self._spec_expr(rule.spec)})"
            )
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

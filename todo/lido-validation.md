# LIDO validation — achievable, deliberately not enabled

**Status: a LIDO *record's content* is not validated; only its envelope is.**
`tests/test_schema_conformance.py` checks the OAI-PMH envelope for every format,
and the payload for `oai_dc` (strictly) and `ria` (against Zetcom's schema). For
LIDO nothing under `<metadata>` is checked.

This is a decision to take, not a blocker to investigate — it works today, and
the recipe is verified below. Left unbuilt on purpose.

## Why it is not on

The vendored `data/lido/lido-v1.0.xsd` imports two things over plain http:

```
http://www.w3.org/2001/03/xml.xsd                        (a 2001-era URL)
http://schemas.opengis.net/gml/3.1.1/base/feature.xsd    (GML 3.1.1)
```

So a validator reaches out for the **GML 3.1.1 tree and its transitive imports**.
`xmlschema` fetches it and then fails *inside GML* with *"the derived group is an
illegal restriction"* on `AbstractReferenceSystemBaseType` — a type that lives in
**GML, not in LIDO**. (Check it: `grep AbstractReferenceSystemBaseType
data/lido/lido-v1.0.xsd` finds nothing.) The failure is in the dependency, not in
LIDO and not in our output — which is worth knowing before anyone concludes the
payload is fine because nothing complained.

A test suite should not depend on fetching a GML tree either way.

## It works — verified, with xmlschema, no lxml

Repoint those two imports at local copies:

1. the current W3C `xml.xsd` (`https://www.w3.org/2001/xml.xsd`) for the XML
   namespace — the LIDO schema has 12 `xml:lang` references needing it;
2. a **stub** for GML declaring the only three names LIDO references —
   `gml:Point`, `gml:LineString`, `gml:Polygon`. That really is all of them, and
   all three are element references: **no GML type is derived from**, so an
   element-only stub satisfies the import.

Measured, both ways:

```
xmlschema + local imports -> LOADED: http://www.lido-schema.org
                             our live LIDO payload: VALID
lxml      + local imports -> compiles, and the payload is VALID too
```

**LIDO 1.1 does not help.** `https://lido-schema.org/schema/v1.1/lido-v1.1.xsd`
exists (185 KB) and carries the *same* two imports, failing identically. It is
the dependency, not the schema version.

## Options

1. **Enable it with `xmlschema`** — recommended if LIDO content matters. Vendor
   `xml.xsd` and a small `gml-stub.xsd` under `data/lido/`, keep a patched copy
   of the LIDO schema whose two `schemaLocation`s point at them, then validate
   the served payload exactly as the Zetcom payload is validated today. One
   validator for everything, no new dependency. Cost: two extra vendored files
   and a patched schema copy to keep in step with upstream. Caveat, stated
   plainly: the stub means the three GML-referencing *types* are not really
   validated — irrelevant in practice, since our LIDO output contains **0**
   occurrences of `gml:Point`/`LineString`/`Polygon`.
2. **Use `lxml` instead.** Also verified to compile and validate, and needs the
   same two provisions. Cost: a second validator, which the project deliberately
   avoided in favour of pure-Python `xmlschema`.
3. **Leave it as it is.** Envelope-only. The LIDO payload is the colleague's
   stylesheet output, so a schema violation would be theirs to fix — but we would
   never learn that it happened. Note the asymmetry: the Zetcom payload now has
   exactly the protection LIDO lacks.

## What would settle it

Does anything consume our LIDO, and would a schema-invalid LIDO payload be worth
catching here rather than in the stylesheet's own repository? If LIDO is a
published view, option 1 is cheap insurance. If it is a side channel, option 3 is
honest and this file is the record of the trade.

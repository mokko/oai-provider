# The `ria` payload exposes internal-only fields — filter them out

**Status: decided in principle, not built.** Some values in the internal RIA data must not go public —
**insurance values** and **object storage locations**. The `ria` format serves the stored record
verbatim, so those fields are currently published to any harvester. This file records the decision,
what is known about the fields, and the one thing to settle before building (the exact field list).
Do not invent a field name: identify them on the real internal export.

## Why it is currently a leak

`kind = "passthrough"` serves the whole stored document (`oai/mapping.py`, `_module_payload`). The
store is the raw RIA record with namespaces stripped, so whatever MuseumPlus holds is served. There is
no allow-list, by design — the standing policy was "the payload is stored verbatim, filtering it was
explicitly declined" (AGENTS.md, "Payload"). **This reverses that for the serve path**: the store
stays verbatim (the colleague's layout and the `deep-equal` ingest test are unaffected); the filter is
a serve-time rebuild, the same place the record is already rebuilt for the wire.

## Confirmed present in the store (Object module, KK sample)

Location fields, found by surveying the stored `@name`s — names only, no values:

- `ObjCurrentLocationGrp`, `ObjCurrentLocationVoc`, `ObjCurrentLocationVrt`,
  `ObjCurrentLocationGrpVrt`, `ObjCurrentLocationHierarchicalVrt`
- `ObjNormalLocationVoc`, `ObjNormalLocationVrt`, `ObjNormalLocationHierarchicalVrt`
- `LocationVoc`, `currentLocation`, `normalLocation`
- `_UserObjGeneralStandortGrp`, and a `dataField` named `Vermerk (Standort)`

## Insurance — not in this sample, must be identified

No `Insur`/`Versicher`/`Insurance` field appears in the 1000-record KK sample or in `ZETCOM-FIELDS.md`.
The values the user means are internal, so they may live in a module or a field the sample does not
carry (another collection, another export). **Find the exact field name(s) on the real internal export
before building** — the whole task is the list, and a guessed name would silently filter nothing.

## Where to filter

- The passthrough body already does `local:zetcom(copy $app := $src/ancestor::application modify (…)
  return $app)` for `totalSize`. Add deletes of the sensitive elements there.
- **Match by `local-name()`**, not by a prefixed path: the store is namespace-stripped and the rebuild
  (`local:zetcom`) re-qualifies names, so a `z:`-prefixed test would miss. Delete on the stripped tree
  before re-namespacing, or test `local-name()`.
- **Omission, not redaction.** Drop the element; do not replace it with a note. An empty element
  asserts the field exists and is blank, which is a different claim (the same rule as `dc`'s empty
  terms).
- Make the list **configuration, not code** — a field-name list on the format (e.g.
  `omitFields = [...]`), so a second deployment changes it without editing `mapping.py`. This is the
  repo's own "mapping is data" stance.

## The other advertised formats — check, do not assume

- **LIDO does not currently emit these.** `repositoryWrap.xsl` hardcodes `<lido:repositoryLocation>`
  as Berlin (the institution's city, not the shelf), and `zml2lido.xsl` has a
  `<xsl:template match="z:dataField|z:moduleReference|z:systemField|z:VocabularyReference"/>` that
  suppresses every unmapped field. So no storage location and no insurance reaches LIDO — verify this
  holds after any stylesheet change.
- **`oai_dc` does not map them**: `dc:coverage` is the place of production/depiction and `dc:rights`
  is the photographer/rights holder, not storage or insurance. Confirm against the term list.

The user asked for `ria`; the same leak through another format is the same leak, so the check belongs
with the change.

## Tests to add

- A served `ria` payload contains none of the listed field names; the stored record still does
  (verbatim, unchanged) — the two must be asserted together so the fix cannot quietly start filtering
  the store.
- The `ria` payload still validates against the Zetcom schema after the deletions
  (`tests/test_schema_conformance.py`).
- The `lido` and `oai_dc` payloads carry none of the fields either.

## What would settle it

The exact field list, from the real internal export (location is confirmed; insurance is not yet).
And one decision: are there other internal-only values in the same class (provenance, valuation,
`ObjProvBewertungVoc`, acquisition price) that should be on the list the first time? Better to settle
the whole list than to filter twice.

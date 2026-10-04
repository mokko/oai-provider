# Related works: online status + ISIL, resolved inside BaseX — implemented

**Status: implemented** (`08c358b`, behind the `relatedWorksOnlineOnly` config switch). This is the
**lvl2 conversion** step of the user's `mokko/zml2lido` tool — the Python/lxml pass
(`LidoTool.to_lvl2_single` → `LinkChecker.fixRelatedWorks`) that runs *after* the XSLT. It now runs
in the provider against the local store instead of a second RIA query. The file is kept as the
record of what it does, why, the verified XQuery it was built from, and the one question still open
(KUE.ID / MM.ID targets).

## Where lvl2 comes from

zml2lido makes LIDO in two passes:

1. **lvl1** — the XSLT (`data/lido/zml2lido.xsl`, ~5,000 lines, vendored here).
2. **lvl2** — `LinkChecker`: load the lvl1 LIDO with lxml, rewrite some tags, save
   (`zml2lido/linkChecker.py`). Three rewrites: drop records without `objectPublishedID`, fix
   `relatedWorksWrap` (this file), unescape `descriptiveNoteValue` (`todo/unescape-html.md`).

We drop the first: the export is the published set, so every record in `sync_Object` is online by
construction, and `objectPublishedID` adds nothing. The unescape is a separate todo. **Fix related
works is the part that matters**, because it is the only lvl2 step that needs to know something
about a *second* record.

## What zml2lido does (linkChecker.py:59‑114, 252‑287)

For each `…/objectRelationWrap/relatedWorksWrap/relatedWorkSet/relatedWork/object/objectID`:

- reads `@lido:source`: `OBJ.ID` → Object, `LIT.ID` → Literature; **anything else is a
  `ValueError`**;
- only `Object` ids are acted on at all (a `LIT.ID` is left as-is);
- asks the `RelWorksCache` whether the target is online —
  `ObjPublicationGrp` with `PublicationVoc=Ja` **and** `TypeVoc@id=2600647`
  (`relWorksCache.py:112‑132`);
- **online** → `@lido:source` := `ISIL/ID`; text := `ISIL` + `/` + id, the ISIL looked up in
  `vocmap.xml` (`verwaltendeInstitution` → `target[@name='ISIL']`);
- **offline** → delete the `relatedWorkSet`; if the wrap is then empty, delete it too.

The cache is filled by querying RIA through `mpapi` (`MpApi`), once per distinct target id, and
persisted as `relWorks_cache.xml`. Two bugs **not** to reproduce:

- if the target has no `ObjOwnerRef`, the code sets `@source=ISIL/ID` and *then* fails to set the
  text — leaving an `objectID` that claims `ISIL/ID` but holds a bare number;
- a target RIA returns nothing for raises `KeyError` out of `item_is_online`, uncaught by
  `_rewrite_relWork`, so the run **dies** instead of deleting the set.

## Why the lookup is local — no `mpapi`, no RIA

- The provider already reassembles the related **objects** into the transform input:
  `_xslt_payload` (`oai/mapping.py:246‑258`) selects, per record, the same-module objects named by
  `ObjObjectCre` and `ObjLiteratureRef` — exactly the references `objectRelationWrap.xsl` turns into
  `relatedWorkSet`s. So the target record, with its `ObjPublicationGrp` and `ObjOwnerRef`, is
  already inside `$input` when the transform runs.
- Everything RIA supplied (the target's `ObjPublicationGrp` + `ObjOwnerRef`) is in the stored
  payload. The cache was only ever an optimisation over a remote DB; with the objects local it is
  redundant.
- Consequence: the whole step is a local XQuery over `$input` and the transform output. No
  credentials, no network — **it works on the Rock 5B**, which has no RIA access.

## The rule to implement

For every `lido:relatedWorksWrap/lido:relatedWorkSet` in the transform output:

| `@lido:source` | action |
|---|---|
| `OBJ.ID` | target := the Object in `$input` with `@id = text`. Online := target exists **and** the publication predicate holds on it. Online → rewrite (`@source=ISIL/ID`, text = ISIL`/`id`). Not online → delete the set; delete an emptied wrap. |
| `LIT.ID` | leave untouched — there is no Literature module in the store, and zml2lido never resolves these either. |
| `KUE.ID` / `MM.ID` | the stylesheet *can* emit these (Person / Multimedia targets of `ObjObjectCre`) and `$input` does not carry those modules. **Left untouched** pending the open question below. |
| anything else | leave it and log; do not raise (zml2lido raises — see the bug above). |

Publication predicate — the same vocabulary item the XSLT itself uses (`TypeVoc@id=2600647` in
`relWorksCache` is the same item; prefer the readable name):

```xquery
some $it in $item//repeatableGroup[@name='ObjPublicationGrp']/repeatableGroupItem satisfies
  $it/vocabularyReference[@name='PublicationVoc']/vocabularyReferenceItem/@name = 'Ja'
  and $it/vocabularyReference[@name='TypeVoc']/vocabularyReferenceItem/@name =
      'Daten freigegeben für SMB-digital'
```

ISIL lookup (verified against `vocmap.xml`; returns e.g. `DE-MUS-815114` for
`Alte Nationalgalerie, Staatliche Museen zu Berlin`):

```xquery
$vocmap/vocmap/voc[@name='verwaltendeInstitution']/concept[source = $verw]/target[@name='ISIL']
```

**Bind the vocmap path as an external variable and pass it in (`$vocmap`), don't hard-code it and
don't rely on a relative `doc()`.** In XQuery a relative `doc('vocmap.xml')` resolves against the
**static base URI** — for a query sent over REST that is not the JVM working directory, and from a
file it is the query file's directory. That is a different rule from the stylesheet's
`document('file:vocmap.xml')` (Saxon resolves that against the JVM cwd, which is why the service
`WorkingDirectory` matters). The provider already passes every path as an external variable; keep
that.

## Verified XQuery recipe

Run on BaseX 12.4. `$input` is the assembled RIA document, `$out` the transform result; the map is
built once per record, then three `copy … modify` passes do drop → rewrite → prune.

```xquery
declare namespace z    = 'http://www.zetcom.com/ria/ws/module';
declare namespace lido = 'http://www.lido-schema.org';
declare variable $vocmap external;   -- document-node(), absolute path

declare function local:isOnline($item as node()?) as xs:boolean {
  exists($item//z:repeatableGroup[@name='ObjPublicationGrp']/z:repeatableGroupItem[
    z:vocabularyReference[@name='PublicationVoc']/z:vocabularyReferenceItem/@name = 'Ja'
    and z:vocabularyReference[@name='TypeVoc']/z:vocabularyReferenceItem/@name =
        'Daten freigegeben für SMB-digital'])
};

declare function local:isil($vocmap as document-node(), $verw as xs:string?) as xs:string? {
  $vocmap/vocmap/voc[@name='verwaltendeInstitution']
    /concept[source = $verw]/target[@name='ISIL']/string()
};

(: per record, before the transform output is fixed up :)
let $targets := $input/z:application/z:modules/z:module[@name='Object']/z:moduleItem
let $online := map:merge(                     (: ids that are present AND published :)
  for $m in $targets where local:isOnline($m)
  return map:entry(string($m/@id), true())
)
let $isil := map:merge(                       (: the subset that also has an ISIL :)
  for $m in $targets
  let $v := normalize-space($m//z:moduleReference[@name='ObjOwnerRef']//z:formattedValue)
  let $i := local:isil($vocmap, $v)
  where local:isOnline($m) and exists($i)
  return map:entry(string($m/@id), $i)
)

(: 1. drop offline OBJ.ID sets :)
let $dropped := copy $o := $out
  modify (for $s in $o//lido:relatedWorkSet[
            string(lido:relatedWork/lido:object/lido:objectID/@lido:source) = 'OBJ.ID']
            [not(map:contains($online, string(lido:relatedWork/lido:object/lido:objectID)))]
          return delete node $s)
  return $o

(: 2. rewrite online OBJ.ID sets that have an ISIL :)
let $rewritten := copy $o := $dropped
  modify (
    for $oid in $o//lido:relatedWorkSet[
          string(lido:relatedWork/lido:object/lido:objectID/@lido:source) = 'OBJ.ID']
          [map:contains($isil, string(lido:relatedWork/lido:object/lido:objectID))]
          /lido:relatedWork/lido:object/lido:objectID
    let $id := string($oid)
    return replace node $oid with
      element { node-name($oid) } {
        $oid/@* except $oid/@lido:source,
        attribute { QName('http://www.lido-schema.org', 'source') } { 'ISIL/ID' },
        text { $isil($id) || '/' || $id }
      }
  )
  return $o

(: 3. drop an emptied wrap :)
let $pruned := copy $o := $rewritten
  modify (for $w in $o//lido:relatedWorksWrap[not(lido:relatedWorkSet)] return delete node $w)
  return $o
```

Why `replace node` and not `replace value of node …/@lido:source`: on a rebuilt element the
attribute is in the LIDO namespace (`lido:source`), so a plain string replace of the value is
fiddly, and two `replace value` expressions on the same node in one `modify` interact badly. Rebuild
the node once. Preserve `lido:type` by carrying `$oid/@* except $oid/@lido:source`.

Note the two-map split: an object can be **online but have no ISIL**. Such a set must be *kept*
(online) but *not* rewritten (no ISIL), and left with `@source=OBJ.ID` — never half-rewritten into
the `@source=ISIL/ID`-with-a-bare-number state zml2lido produces.

## How it is wired (implemented)

- `local:isOnline`, `local:isil`, `local:verwaltendeInstitution` and `local:fixRelatedWorks` are
  inline prolog functions in `oai/mapping.py` (`RELATED_WORKS_FUNCTION`), emitted only for a format
  whose `relatedWorksOnlineOnly` is set. The prune uses the prefix the format chose rather than a
  hardcoded `z:` (`8aa6236`).
- `_xslt_payload` runs the three passes after `xslt:transform` and lifts the record from the pruned
  tree (`$fixed`).
- The switch is per format: `relatedWorksOnlineOnly = true` and `vocmap = "data/lido/vocmap.xml"` in
  `oai.toml`. `Config.load` resolves `vocmap` against the config file and refuses the combination
  without it, or without the LIDO prefix bound to `lido`.
- `$vocmap` is bound as an external variable from config (`Provider.source_vars`), never a relative
  `doc()`.

## Tests — the rule itself is still unverified

Pinned today: the query text carries `local:fixRelatedWorks(` and the record is lifted from `$fixed`
(`tests/test_lido.py`), and the round trip proves the ISIL map is found because the served
`lidoRecID` carries an ISIL from `vocmap.xml`. **The rule below is not covered** — these are still
wanted:

- An online target → `@source=ISIL/ID`, text `ISIL/id`, `lido:type` kept.
- An offline target → the `relatedWorkSet` is gone and an emptied wrap is gone.
- `LIT.ID` → untouched.
- An online target with no `ObjOwnerRef`/ISIL → set kept, `@source` still `OBJ.ID`, no bare-number
  source.
- A record with no related works → no `relatedWorksWrap` at all (unchanged).
- Shape: the pruned output still validates as LIDO (`todo/lido-validation.md`).

## Open questions

- **`KUE.ID` / `MM.ID` targets** (Person / Multimedia creators on `ObjObjectCre`). The stylesheet
  emits them; lvl2 ignores them (it acts on Objects only). Do they belong in `relatedWorks` at all,
  or should the mapping drop them? That is a decision about what the harvested record should say,
  not an implementation detail.
- **Offline vs. absent.** The rule treats "not in `$input`" as "not published". That is exact iff
  the store is the published corpus. If the store ever holds an unpublished range, a target that is
  present-but-unpublished is still correctly dropped (the predicate reads its `ObjPublicationGrp`),
  but a target that was simply never exported is indistinguishable from an unpublished one. If a
  second corpus is ever ingested, this needs the RIA fallback — the thing `mpapi` did.
- ~~**`ObjPublicationGrp` in the store.**~~ **Verified against the live `sync_Object` (1000
  records):** every record carries `ObjPublicationGrp` (as a `repeatableGroup`) and `ObjOwnerRef`,
  and all 1000 satisfy the publication predicate — the store *is* the published set, so "present in
  the store" == "online". The records are stored whole, root `application`, record =
  `application/modules/module[@name='Object']/moduleItem`, namespace-stripped; the file is split per
  record but nothing inside the record is cut. `$input` in the recipe is the same shape: the
  document `_xslt_payload` assembles as `<application><modules><module name="Object">…`.

# AGENTS.md — oai-provider internals

Reference material for anyone (human or agent) changing this provider. The
README is the front page; this is the depth behind it: how the store is shaped,
how each OAI decision was reached, and every trap that cost a round trip.

**Read this before touching the ingest path, the query templates, or the
mapping.** The traps below are not style notes — each one produced wrong output
that looked correct.

## Code map

- `oai/config.py` — loads `oai.toml` + `.env`, validates the combination
  (module mode forces `deletedRecord = "no"`), holds the module list.
- `oai/mapping.py` — renders the query templates from the module config: the row
  expression, the payload expression, and the Dublin Core term rules.
- `oai/basex.py` — async BaseX REST client (`httpx`), including
  `query_nodes` for queries that return a sequence of nodes.
- `oai/protocol.py` — the six verbs, error codes, datestamps, resumption
  tokens, `serialise`, and the `xslt_problem` startup probe.
- `oai/app.py` — the Starlette app; `GET`/`POST /oai`, `GET /healthz`.
- `xq/*.xq.tmpl` — the query templates: `module_ingest`, `module_count`,
  `page`, `record`.
- `tools/ingest.py` — the CLI.
- `tools/oai_browser.py` — an interactive OAI client (the six verbs from a
  menu) for testing a running provider; a stdlib reimplementation of HTTP::OAI's
  `oai_browser.pl` by Tim Brody.

## Ingest

```
python tools/ingest.py samples/ria-dump.xml            # additive
python tools/ingest.py samples/ria-dump.xml --dry-run  # per-module counts only
OAI_BASEX_PASSWORD=... python tools/ingest.py DUMP
```

One database per `[[modules]]` entry (see "Module mode"). BaseX will not let one
query both store and report — a query is either updating or returning — so a
read-only count pass runs before each write pass. A module the dump does not
carry is skipped (its database untouched); a module present but *empty* is
refused.

### Deletions

**`deletedRecord = "no"`, and that is the truth here.** The source records no
deletes, and ingest is additive — it never removes a record — so nothing knows
what vanished from a dump and no tombstone can be served. Advertising
`persistent` or `transient` while serving no deletions is the one lie a
harvester cannot detect, so the combination is **rejected at load time**.

Consequences worth knowing:

- **A record withdrawn in MuseumPlus stays live here.** A harvester learns of a
  withdrawal by re-harvesting, not from a `status="deleted"` header.
- **Additive is what makes a chunked dump safe.** Records are written in place,
  keyed by `<Module>-<id>`, so chunk 2 adds to chunk 1 instead of replacing it.
  `--reset` is the destructive one: it rebuilds a module database from a single
  file, so running it per chunk leaves only the last chunk's records.
- Tombstone support is an **open question**, blocked on what the colleague's
  setup does — see `todo/deleted-records.md`. **`envelope.md`** at the repo root
  describes the storage mode that *could* detect withdrawals (removed), why it
  existed, and what is lost without it.

## Module mode — one database per module

A MuseumPlus RIA dump carries several modules in one file (Object, Person,
Multimedia). With a `[[modules]]` block in `oai.toml` the ingest switches to
**module mode**: each module is split out into its own database, namespaces
stripped, records stored so that a query written for the colleague's `sync_*`
setup reads identically here:

```
collection('sync_Object')/application/modules/module[@name='Object']/moduleItem
```

```
python tools/ingest.py sdata/Dump.xml --dry-run     # per-module counts only
python tools/ingest.py sdata/Dump.xml               # additive: add new, update seen
python tools/ingest.py sdata/Dump.xml --reset       # DROP and rebuild each db
```

- **Databases are `sync_<Name>`** — `sync_Object`, `sync_Person`,
  `sync_Multimedia` — matching the layout this is meant to be installed into.
- **Ingest is additive by default; `--reset` is the destructive one.** Each
  record is `db:put` at `<Module>-<id>.xml`, so the id is the key: a new id is
  added, an existing id replaced, and a record the dump does not mention left
  alone — so importing several chunks of one export just works. `--reset` drops
  and rebuilds each database from that one file (and prints what it drops
  first); running it per chunk would make the last chunk the only data. A module
  **absent** from a dump is skipped (its database untouched), so a per-module
  file merges cleanly; a module present but *empty* is refused.
- **Namespaces are stripped on the way in** (a recursive XQuery `local:strip()`
  rebuilds each element with `local-name()`). The original file is never
  touched; the stored copy is the one deliberate exception to "payload stored
  verbatim". Stripping conflates the four RIA dialects, which is harmless while
  every record is a module response.
- **The RIA namespace is restored on the way out, whatever the format.** The
  store stays namespace-free, and `oai/mapping.py`'s `local:zetcom()` rebuilds
  each element with `QName(ZETCOM_NS, local-name(.))` — the mirror of
  `local:strip()`. For a `kind = "xslt"` format `_xslt_payload()` assembles
  `<application xmlns="…zetcom…"><modules>…` from the record plus its related
  Person/Multimedia/object records, each re-namespaced, before handing the whole
  thing to `xslt:transform`. For `ria` (`kind = "passthrough"`) the payload is
  re-namespaced the same way, because **OAI-PMH does not admit an unnamespaced
  payload at all**: `metadataType` is `<any namespace="##other">`, and `##other`
  excludes the *absent* namespace. Attributes pass through untouched: module
  ingest only ever dropped *element* namespaces.
- **Cost of that rebuild, measured**: the `ria` payload phase goes from ~0.20 s
  to ~0.47 s per 100 records, and a 100-record page grows ~10% (3.88 → 4.28 MB)
  because BaseX serialises the rebuilt elements with an `ns2:` prefix on every
  element once they sit beside OAI-namespaced ones. It is the price of serving a
  conformant payload from a stripped store — the same trade already made for
  LIDO — not an optimisation to revisit. gzip absorbs the bytes.
- **The module tag distinguishes the records.** `@id` is unique only within a
  module — the ranges overlap (Object reaches 935894, Person 1764036,
  Multimedia 8533256, and one id appears as both an Object and a Multimedia
  record) — so each module carries an identifier prefix: `object-`, `person-`,
  `asset-`. All three dropped the `EM-` they used to carry, because `EM` means
  nothing in this export — every `__orgUnit` is `KK…` — so it was a leftover of
  the old MPX convention rather than an institution tag.
- **Getting files out of the zips.** The samples arrive as zips whose single
  entry is **LZMA (method 14)**. Info-ZIP's `unzip` refuses ("need PK compat.
  v6.3"), and there is no 7z or bsdtar on this box. Python's `zipfile` handles
  it — stream it with `shutil.copyfileobj`, since reading a 199 MB entry whole
  puts all of it in memory.

### Serving from the module databases

With `[[modules]]` present the six verbs read **all the module databases** and
serve one OAI repository. Each module is normalized to a single `<row>`
(identifier, datestamp, status, sets + payload), so paging, the
resumption-token cursor, set filtering and `GetRecord` have **one code path**
across every module.

- **A record is bare**, so `identifier` and `datestamp` are derived per module
  at query time — `identifierPrefix` + `@id`, and
  `systemField[@name='__lastModified']/value` shifted to UTC by the
  `[datestamps] timezoneOffset`. Each `[[modules]]` entry may override `records`,
  `identifier` and `datestamp`, and carries its own `[[modules.sets]]`
  allow-list.
- **The cursor spans the union**: rows from every module are sorted by
  `(datestamp, identifier)`. Identifiers are globally unique because of the
  module prefixes, so the pair is a total order across databases and a harvest
  neither skips nor repeats a record.
- **`deletedRecord` is `"no"`.** Ingest is additive and never deletes a record,
  so nothing records what vanished and no tombstone is ever served. Claiming
  `"persistent"` would be the one lie a harvester cannot detect, so the
  combination is **rejected at load time** — module mode plus a `deletedRecord`
  other than `"no"` is a configuration error, not a note in the docs. The source
  owns liveness; a harvester learns of a withdrawal by re-harvesting.

## Serving

```
cp .env.example .env     # then set OAI_BASE_URL for where this runs
.venv/bin/python -m uvicorn oai.app:app --factory --host 0.0.0.0 --port 8000
curl 'http://localhost:8000/oai?verb=Identify'
```

`GET /oai` and `POST /oai` reach the same handler, as the spec requires, and a
test asserts the two produce the same body. `GET /healthz` reports whether BaseX
answers.

**Deployment values come from the environment, not the tracked file.** `baseURL`
is read from `OAI_BASE_URL` (a real variable, or a `.env` beside the config —
`OAI_ENV_FILE` points elsewhere). It must be the address a harvester can call
back: `Identify` echoes it and harvesters then dial it, so `localhost` is only
right when the harvester runs on the same machine. `.env` is gitignored;
`.env.example` is the template. A real environment variable always wins, so a
service manager's `EnvironmentFile` behaves identically.

All six verbs are implemented (Identify, ListMetadataFormats, ListSets,
GetRecord, ListIdentifiers, ListRecords) with the error codes customarily
expected: badArgument, badVerb, cannotDisseminateFormat, idDoesNotExist,
noRecordsMatch, badResumptionToken, noSetHierarchy.

## Response shape (schema, not style)

- **The verb element wraps its records — except `ListIdentifiers`.** Per
  `OAI-PMH.xsd`: `GetRecordType` is a sequence of exactly one `record`,
  `ListRecordsType` a sequence of `record` elements followed by an optional
  `resumptionToken`, and `ListIdentifiersType` takes `header` elements directly.
  So `GetRecord` and `ListRecords` emit `<record><header/>[<metadata/>]</record>`
  while `ListIdentifiers` emits bare `<header/>`. A bare `header`/`metadata`
  under `GetRecord` or `ListRecords` is **schema-invalid** — a conformant
  harvester cannot parse it — which is what this served until the schema was
  checked by hand and `test_records_are_wrapped_as_the_schema_requires` landed.
  The builder branches once on `with_payload` rather than duplicating the loop.

## Schema conformance, and how the payloads differ

`tests/test_schema_conformance.py` validates served responses against the
**vendored, unmodified** schemas (`data/SOURCES.md` has provenance and hashes),
using `xmlschema` — pure Python, so no compiler is needed anywhere the suite
runs. **Not lxml**, deliberately.

`<metadata>` is `<any namespace="##other" processContents="strict"/>`, so a
strict processor must find a **global** element declaration for the payload, and
that is what makes the formats differ:

- **`oai_dc`** — `oai_dc:dc` is global, so the whole document validates strictly.
- **`ria`** — the payload is the **whole record document**, rooted at
  `application`, Zetcom's schema's sole global element, so the payload validates
  strictly as served. (`moduleItem` alone is a **local** element and would not
  resolve — which is why the payload is the whole
  `application/modules/module/moduleItem` tree, not a bare `moduleItem`.)
  The module's `totalSize` is written at serve time as the number of records the
  document actually holds — one, for the per-record store — and the ingest no
  longer copies the source file's count, so there is a single site.
- **`lido`** — **envelope only; content is not validated.** The vendored schema
  imports `xml.xsd` (2001-era URL) and GML 3.1.1 over plain http, so loading it
  fetches the GML tree — and the type xmlschema chokes on lives in **GML, not in
  LIDO** (`grep AbstractReferenceSystemBaseType data/lido/lido-v1.0.xsd` finds
  nothing), so it says nothing about our payload. Repoint those two imports at
  local copies — the current `xml.xsd`, plus a stub declaring only the three GML
  elements LIDO references (`Point`, `LineString`, `Polygon`; no GML *type* is
  derived from, so an element-only stub suffices) — and the schema loads and our
  payload **validates**, with `xmlschema` and without lxml. Verified, not
  enabled: see `todo/lido-validation.md`. LIDO 1.1 is no help — same two imports,
  same failure.

**Zetcom does have a schema** — `data/zetcom/module_1_6.xsd`, self-contained,
`targetNamespace="http://www.zetcom.com/ria/ws/module"`, taken from
**github.com/mokko/MpApi** (`src/mpapi/data/xsd/module_1_6.xsd`, **GPL-3.0** —
settle that licence question deliberately; see `data/SOURCES.md`). Zetcom
publishes none and the dump names none, which is why a `ria` payload looked
unvalidatable for so long. Our served payload passes it, which also proves the
serve-time re-namespacing is right: the schema is
`elementFormDefault="qualified"`, so a stripped payload could not pass.

## Resumption tokens

Stateless and signed, so a restart cannot invalidate a harvest in progress —
which the earlier Perl provider's in-memory chunk cache could not survive.

- The cursor is the pair **(datestamp, identifier)**, a total order. A datestamp
  alone is not unique, and a non-unique cursor either skips the peers or repeats
  them. Skipping is the dangerous one: the harvest still ends on an empty page,
  so the harvester believes it finished. There is a test over a dump whose
  records share a second.
- The window is **pinned**: `until` is fixed on the first page and carried in the
  token, so a re-ingest mid-harvest cannot move a record across a page boundary.
  A record edited during a harvest is missed by *that* harvest and picked up by
  the next — delayed, never silently skipped.
- The token carries a **fingerprint of the mapping**, so a token issued under a
  different mapping is refused with badResumptionToken rather than quietly
  returning a wrong slice. It also expires (`protocol.tokenTTL`), which is what
  replaces server-side eviction.
- `completeListSize` is the size of the whole pinned result set, not the page.
- **No second scan for the total.** The first page counts the pinned set from
  the scan it already runs (its cursor is empty, so the matching set *is* the
  result set); a resume reads the total from the token. So a page is one BaseX
  round trip, not two. Measured and locked in by
  `test_resumption_costs_one_query_per_page`.

## Metadata formats

Three are configured:

- **`ria`** (`kind = "passthrough"`) — the stored payload, verbatim, inside
  `<metadata>`, under Zetcom's namespace.
- **`oai_dc`** (`kind = "derived"`) — Dublin Core, **assembled at serve time**
  from the same stored payload. The payload is stored once and viewed twice;
  nothing is duplicated on disk.
- **`lido`** (`kind = "xslt"`) — **LIDO**, produced by the existing
  `mokko/zml2lido` stylesheet (XSLT 3.0, vendored under `data/lido/`) running
  **inside BaseX** via `xslt:transform`. That stylesheet is not a per-record
  mapping: it reads a whole `application/modules` tree and reaches across
  modules — a person is a forward lookup from `ObjPerAssociationRef`, an asset
  is a *reverse* one (it points back at the object it documents), and related
  works point at other objects. So each record's world is reassembled from the
  module databases, the RIA namespace the ingest stripped is restored, and the
  whole thing is handed to the transform. The record never leaves the store.

A derived format is declared with the element to build and the prefixes its
wrapper and terms use. **Its whole mapping sits in one block under the format**,
each term naming the module whose records it applies to — the three modules are
three different record shapes, so a term rarely applies to all of them:

```toml
[[metadata.formats]]
prefix = "oai_dc"
kind = "derived"
wrapper = "oai_dc:dc"
namespaces = { oai_dc = "…", dc = "…" }

[[metadata.formats.terms]]
module = "Object"         # scopes the term; omit it to apply to every module
term = "dc:type"
xpath = "//dataField[@name='ObjTechnicalTermClb']/value"
```

A module may still carry its own `[[modules.terms]]`, which **overrides** the
format's rules for those records — that is how a one-off exception is written
without disturbing the shared block. A `module` key inside `[[modules.terms]]`
is rejected at load, because the entry is scoped already and the key would
silently do nothing.

- **A term with no value is omitted, never emitted empty.** That is the whole
  of "correct Dublin Core" here — no blank `dc:date`.
- **A derived format must cover every module**, or it would disseminate an empty
  `oai_dc` silently: each module needs module-level terms, a term scoped to it,
  or an unscoped term. A term naming a module that is not in `[[modules]]` is a
  load error.
- **The mapping is authored, not inferred.** Each term is a field the record
  really has, or a literal declared as one (`dc:language = "de"`). Nothing is
  invented — but the corollary bit us once: `dc:title` was left out for objects
  because the *sample fixture then in use* had no title, while the real export
  titles every object (`ObjObjectTitleVrt`, 1000/1000, equal to the
  `ObjObjectTitleGrp` item with `SortLnu = 1` in all 1000). That fixture carried
  `ObjObjectTitleClb`, which the export never has. **It has since been replaced
  by real records** — three Object records, verbatim, with their storage
  locations redacted — precisely so fixture and reality cannot drift apart
  again. Count coverage against the module databases, never against the fixture.
- Fields were chosen by **coverage counted over the real data**, not by name —
  the notes in `oai.toml` carry the counts beside each choice.

### LIDO, and what it costs

- **Saxon and xmlresolver must be on BaseX's classpath** (`lib/Saxon-HE-12.5.jar`,
  `lib/xmlresolver-5.2.2.jar`). BaseX's built-in `xslt:transform` is XSLT 1.0 and
  rejects the stylesheet; with Saxon it runs 3.0. The app **probes for this at
  startup** and refuses to start if it is missing, rather than advertising a
  format it cannot produce.
- **BaseX must run with a working directory containing `vocmap.xml` and
  `europeanaFashion17.rdf`.** The stylesheet calls `document('file:vocmap.xml')`,
  and a relative `file:` URI resolves against the **process cwd**, not the
  stylesheet's — which is why zml2lido's own tool does `os.chdir()`. Without it
  the first ISIL lookup dies with `FODC0002`.
- **LIDO changes the record model.** It is object-centric: one `lido` per object
  with persons and assets inlined, so a `lido` harvest is ~1000 records where the
  raw store has 5,884. `modules = ["Object"]` on the format makes that explicit,
  and the page, the count and the cursor all work off that subset.
- **A transform per record costs roughly 0.5s**, so a page of 100 is noticeably
  slower than the other formats. `ria` and `oai_dc` are unaffected.
- **The stylesheet decides what is publishable**: it drops objects without
  `ObjOwnerRef` and only emits `objectPublishedID` for records published at
  SMB-digital. Those are publishing decisions rather than structure and could be
  lifted into config; the option is noted in `oai.toml`, not taken.

## Sets

Sets are an **explicit allow-list**. Each entry pairs an XPath (a membership
predicate; its value is ignored) with a hand-written `spec` and `label`. They
live under `[[modules.sets]]`, because a set belongs to the module whose records
it filters. A set whose XPath matches nothing is silently inert — `ListSets`
just returns `noSetHierarchy` — which is exactly how a misconfigured set hides,
so verify with a request rather than by reading the config.

```toml
# module mode - the real set. "KK" is our internal label for the
# Kupferstichkabinett; the holder is the Address moduleItem the records carry.
[[modules.sets]]
spec  = "KK"                    # setSpec published to harvesters
label = "Kupferstichkabinett, Staatliche Museen zu Berlin"   # setName
xpath = "moduleReference[@name='ObjOwnerRef']/moduleReferenceItem[@moduleItemId='112264']"
```

A record is in the set when its XPath selects anything; value tests belong
inside the XPath (`dataField[@name='X'][value='Y']` — module records are
namespace-stripped, so no prefixes). Verified: `set=KK` → 1000 (the Object
records), no set → 5884, and an unknown spec is `noRecordsMatch`, not silence.

Consequences worth being deliberate about:

- **The setSpec is written by hand and never taken from a record.** No
  internal group id or organisational unit can reach OAI output by accident,
  and a group that is not listed is not harvestable at all.
- `__orgUnit` is an organisational unit, not a set, and is not published as
  one.
- **Pick the set field by surveying the real data, not by its name.** Two
  obvious candidates are traps here: `ObjOwnerRef` carries a *single* value
  across all 1000 objects (so the set equals the whole repository — fine only
  while one department is imported), and `ObjObjectGroupsRef`'s ~100 groups are
  internal working labels (`KK_Priorisierung 1`, `KK_Diskriminierende Titel
  prüfen`, "nochmal checken für Flamen") that must never be published wholesale.
  The clean hierarchy, if wanted, is `ObjOrgGroupVoc` (16 curatorial
  collections, 1000/1000 coverage); `ObjPublicationStatusVoc` sounds like a
  publish flag but is physical status (`vorhanden`, `Kriegsverlust`).


ListSets needs no database query — the specs and labels come straight from
the config, which is what keeps the allow-list authoritative.

**A set rule that embeds an id copied from a synthetic fixture matches nothing
on real data, and never errors.** A selector like
`.../moduleReferenceItem[@moduleItemId='6054']` taken from the sample file
selects zero real records: the set silently publishes no set and every record is
a member of none. Read the ingest's **dry-run `sets=` column** — empty on every
real record is the tell — and derive set selectors from real values (prefer a
name-based predicate over a numeric id) before trusting any set rule.

## Payload

The stored record keeps the source `moduleItem` **verbatim** — no filtering,
no omissions. `tests/test_ingest_basex.py` asserts each stored `moduleItem`
`deep-equal`s the one it came from, so this cannot drift silently.
(Module mode's namespace stripping is the one deliberate exception, noted
above.) The `ria` format then serves that record as the **whole document** —
`application/modules/module/moduleItem`, re-namespaced — rather than the bare
`moduleItem`: only `application` is a global element, so a `moduleItem`-rooted
payload is neither the record nor resolvable in Zetcom's schema.

## Scale

Paging orders the whole matching set in BaseX (`order by` in `xq/page.xq.tmpl`)
rather than seeking an index — fine at this size, worth an index and a seek
before it carries a large collection.

**The payload is fetched after the page is chosen, not before.** `{{SOURCE}}`
yields header rows only; the order-by sorts what it can afford, and
`{{PAYLOADS}}` attaches the payload for the page's identifiers alone. Attaching
payloads in the source and then sorting them was the entire cost of
`ListRecords`, and it showed as a flat curve — a page of 1 and a page of 100
cost the same, because all 5,884 matching records were materialised and then
discarded:

| | before | after |
|---|---|---|
| `ListRecords` page_size=1 | 6.49s | 1.14s |
| `ListRecords` page_size=100 | 6.63s | 1.34s |
| `ListRecords` page_size=1000 | 8.80s | 6.72s |
| `ListIdentifiers` page_size=100 | 0.88s | 0.88s |

The tell that the fix is real is that the time now **scales with page size**
instead of being constant. `oai_dc` benefits identically — it is built in the
payload phase, so Dublin Core is assembled only for records actually served.

## Datestamps

Zetcom/MuseumPlus emits local wall clock with a space separator
(`2021-09-07 10:56:34.615`). OAI needs `YYYY-MM-DDThh:mm:ssZ` in **UTC**.
Appending `Z` is wrong by the offset and breaks incremental harvesting
silently. Convert: fix the separator, append the configured offset, then
`adjust-dateTime-to-timezone(..., PT0S)`. Use
`systemField[@name='__lastModified']` (the record's own modification time), not
the export date, which changes on every export.

## Mapping notes that cost real round trips

- **A prefix used in a query resolves against the query's prolog**, never
  against the source document's own `xmlns`. The prefix table in `oai.toml`
  is load-bearing. For namespace-stripped data, leave it empty and write
  unprefixed XPaths — both work through the same code.
- **Empty string is not absent.** A variable bound to `""` is not `()`;
  `empty($x)` is false and a `where` clause silently matches nothing.
- **Comparing a missing attribute yields the empty sequence**, which is
  falsy: `@seen ne $dumpId` skips records that never carried a stamp. Use
  `not(@seen = $dumpId)`.
- **A query returning a sequence of nodes is not a parseable document.** See
  `BaseXClient.query_nodes`.
- **`totalSize` is not a record count.** Zetcom's `<module totalSize="5">`
  is the server's result size, not the number of `moduleItem`s in the file.
  (It matched the real counts in the full dump; the mismatch is between a
  *trimmed test fixture* and its declared size.)
- **RIA XML is not flat.** Most fields sit inside
  `repeatableGroup/repeatableGroupItem` at varying depth, so mapping XPaths
  must descend rather than follow fixed paths.
- **The holding institution is `dc:publisher`, and it is the one mapped value
  that is partly an assumption.** Dublin Core has no institution element, and
  `oai_dc.xsd` permits only the fifteen `dc` elements as children — of those,
  `dc:publisher` ("an entity responsible for making the resource available") is
  what harvesters such as Europeana and the DDB read as the data provider.
  `dc:contributor`, `dc:source` and `dc:rights` are the plausible neighbours and
  each means something else. For **Object** the value is real data
  (`ObjOwnerRef//formattedValue`, present on 1000/1000). For **Person** and
  **Multimedia** the record states no owning collection, and a term's XPath sees
  only its own record (so `MulObjectRef` cannot be joined in), so the
  institution is a **literal** — correct for a one-collection deployment and
  wrong the moment a second collection is ingested.
- **External variables arrive as untyped strings unless the query declares the
  type.** `declare variable $limit as xs:integer external;` — `$limit + 1` on a
  bound `1` otherwise fails with "Arithmetics not defined for xs:string and
  xs:integer", and a declared type also rejects a bad binding early.
- **In a predicate, `env:identifier` selects a child ELEMENT.** Attributes need
  `@env:identifier`. A query can report the right record count and match nothing
  at all because of this.
- **A template's own wrapper elements live in no namespace** (ours, not OAI's,
  so they cannot be confused with protocol elements). Comparing one against an
  OAI-qualified tag matches nothing, and every page comes back empty *without*
  an error.
- **A resumption request carries no `metadataPrefix`** — the spec makes the
  token exclusive. Resolve the metadata format from the token, never from the
  arguments, or every resume is rejected as an unsupported format.
- **`db:put(db, node, path)` replaces, so re-ingest is idempotent.**
- **A write pass cannot also report.** `let $u := db:put(...)` fails with
  `[XUST0001]`; BaseX's own `update {}` keyword fails with "Update target was
  not created by transform expression". So ingest counts with a separate
  read-only query before the write pass.
- **In TOML, a key written after a `[[table]]` header belongs to that table.**
  Putting a scalar below `[[modules.sets]]` silently attaches it to the last set
  rule. Keep scalar keys above the first array-of-tables header.

## Testing conventions

- **The fixture must stay representative.** `samples/ria-dump.xml` is shaped like
  the real export — it carries the fields the mapping actually reads
  (`ObjObjectTitleVrt`, `ObjObjectTitleGrp`, `ObjOwnerRef`, the vocabularies) and
  no fields the export lacks. Fixture and reality disagreeing *in both
  directions* is exactly how `dc:title` came to be missing for objects: the old
  fixture had `ObjObjectTitleClb`, which reality never has, and lacked the title
  field reality always has. **Count coverage against the module databases, never
  against the fixture**, and re-shape the fixture when a real field is found.

- **Assert against BaseX directly**, not against your own summary: run a query
  and print what is stored. When a path matches 0 nodes, check `local-name()`
  and `namespace-uri()` — the answer is usually "the namespace or the level is
  not what you assumed", not "the data is missing".
- **A test helper that resets the database on every call** hides exactly the
  behaviour a deletion test is for: the second dump wipes the records it was
  supposed to find missing. Seed incrementally by default, with an explicit
  reset for the first call.
- **Drive the real ASGI app** (`starlette.testclient.TestClient` runs the
  lifespan) so the HTTP layer is covered, and compare the GET and POST bodies.
- **A test that mutates an encoded value to prove tampering must mutate bits
  that survive the encoding.** `test_tampered_token_is_rejected` flipped the
  *last* character of an unpadded base64url HMAC signature — which carries only
  padding bits, decodes to the same bytes, and verifies. It failed about half
  the time, depending on the timestamp. Flip a character that is not last (or
  decode → flip → re-encode), or the flake reads as a security bug.

## Environment

- BaseX 12.4, rootless at `~/basex/basex/`; launchers `bin/basex` (local client,
  best for probing), `bin/basexhttp` (background), `bin/basexhttpstop`.
- **REST is 8080**, client/server 1984, stop 8081. Older docs saying 8984 are
  wrong for 12.x.
- Dedicated dev user `oai` (the `admin` account is separate and has its own
  password). The data lives in `sync_Object` / `sync_Person` / `sync_Multimedia`;
  everything else (`mpxdb`, `riadb`, `riadb2`, `oaitest`, `oai_provider_*`,
  `oai_*_check`) is scratch left behind by test runs.
- **The HTTP server caches the user list at startup.** After `CREATE USER`,
  restart it or REST keeps answering "Access denied: <user>", which looks exactly
  like a wrong password.
- REST transport: `GET` — unknown query parameters are variable bindings (the
  parameter *name* is the variable); `POST` — the body is a
  `<query xmlns="http://basex.org/rest">` wrapper, and a raw query text POSTed
  without it is rejected with "Content is not allowed in prolog". Do **not**
  combine `-G` with a body.
- `uuid` is unreliable in the target MuseumPlus instance, so the integer `@id`
  is the identifier key.

# AGENTS.md — oai-provider internals

Reference material for anyone (human or agent) changing this provider. The
README is the front page; this is the depth behind it: how the store is shaped,
how each OAI decision was reached, and every trap that cost a round trip.

**Read this before touching the ingest path, the query templates, or the
mapping.** The traps below are not style notes — each one produced wrong output
that looked correct.

## Code map

- `oai/config.py` — loads `oai.toml` + `.env`, validates the combination
  (module mode forces `deletedRecord = "no"`), computes the mapping fingerprint.
- `oai/mapping.py` — the prefix table and the XPath→OAI field mapping.
- `oai/basex.py` — async BaseX REST client (`httpx`), including
  `query_nodes` for queries that return a sequence of nodes.
- `oai/protocol.py` — the six verbs, error codes, datestamps, resumption
  tokens, `serialise`, and the `xslt_problem` startup probe.
- `oai/app.py` — the Starlette app; `GET`/`POST /oai`, `GET /healthz`.
- `xq/*.xq.tmpl` — the query templates: `ingest`, `validate`, `reconcile`,
  `stale`, `count`, `page`, `record`, and the module-mode variants.
- `tools/ingest.py` — the CLI.

## Ingest

```
python tools/ingest.py samples/ria-dump.xml          # three passes
python tools/ingest.py samples/ria-dump.xml --dry-run -v
OAI_BASEX_PASSWORD=... python tools/ingest.py DUMP
```

Three passes, because BaseX will not let one query both store and report (a
query is either updating or returning):

1. **validate** — read-only. Per record: identifier, datestamp, sets, and
   whether it is storable. This is the only place a bad mapping shows up as a
   message rather than as records quietly missing.
2. **ingest** — updating. Envelops and stores every record in the dump, each
   stamped with the dump it was seen in.
3. **reconcile** — updating. Tombstones or removes records not seen in this
   dump.

A run refuses to reconcile if the dump yielded zero storable records, so a
broken mapping cannot tombstone the whole database.

### Deletions

**The shipped configuration is module mode, where `deletedRecord = "no"`** —
see "Module mode" below. This paragraph describes the *enveloped* path, which
reconciles and can therefore serve real tombstones.

The source system does **not** record deletes, and a sync that only adds or
overwrites will serve vanished objects as live forever. No harvester can
recover from that. But the dump is complete, so absence is the delete signal:
reconciling a full dump is what makes `deletedRecord = "persistent"` honest in
the enveloped path. Set `deletedRecord = "transient"` to remove instead, or
`"no"` to leave liveness to the source.

The dump id is a content hash, so re-running an unchanged dump is idempotent
and cannot be mistaken for a dump that lost records.

**A chunked dump destroys the database if reconciled per file.** The file is
called *chunk1*, and the ingest derives its dump id from the file's content
hash, so every chunk gets a different id — and `reconcile` tombstones
everything not seen in *that* dump. Run per chunk, chunk 2 marks all of chunk 1
deleted, and **nothing reports an error**. Before pointing real data at it: the
dump id must be shared across the chunks of one harvest (a dump-set id, e.g. the
export date plus query id), and reconcile must run **once, after the last
chunk**. This is the most dangerous thing about the current design.

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
python tools/ingest.py sdata/Dump.xml               # drop and rebuild each db
python tools/ingest.py sdata/Dump.xml --keep        # overwrite in place
```

- **Databases are `sync_<Name>`** — `sync_Object`, `sync_Person`,
  `sync_Multimedia` — matching the layout this is meant to be installed into.
- **Each module is a full-dump resync**: its database is dropped and rebuilt,
  because the source records no deletes and an overwrite would leave a vanished
  record behind. Per-module databases also mean one module missing from a chunk
  can never tombstone another module's records.
- **Namespaces are stripped on the way in** (a recursive XQuery `local:strip()`
  rebuilds each element with `local-name()`). The original file is never
  touched; the stored copy is the one deliberate exception to "payload stored
  verbatim". Stripping conflates the four RIA dialects, which is harmless while
  every record is a module response.
- **The RIA namespace is restored only for a transform that needs it.** The
  store stays namespace-free; when a `kind = "xslt"` format is served,
  `oai/mapping.py`'s `local:zetcom()` rebuilds each element with
  `QName(ZETCOM_NS, local-name(.))` — the mirror of `local:strip()` — and
  `_xslt_payload()` assembles `<application xmlns="…zetcom…"><modules>…` from the
  record plus its related Person/Multimedia/object records, each re-namespaced,
  before handing the whole thing to `xslt:transform`. `ria` and `oai_dc` never
  see a namespace. Attributes pass through untouched: module ingest only ever
  dropped *element* namespaces.
- **The module tag distinguishes the records.** `@id` is unique only within a
  module — the ranges overlap (Object reaches 935894, Person 1764036,
  Multimedia 8533256, and one id appears as both an Object and a Multimedia
  record) — so each module carries an identifier prefix: `EM-object-`,
  `EM-person-`, `EM-asset-`.
- **Getting files out of the zips.** The samples arrive as zips whose single
  entry is **LZMA (method 14)**. Info-ZIP's `unzip` refuses ("need PK compat.
  v6.3"), and there is no 7z or bsdtar on this box. Python's `zipfile` handles
  it — stream it with `shutil.copyfileobj`, since reading a 199 MB entry whole
  puts all of it in memory.

### Serving from the module databases

With `[[modules]]` present the six verbs read **all the module databases** and
serve one OAI repository. The two storage shapes are normalized to a single
`<row>` (identifier, datestamp, status, sets + payload), so paging, the
resumption-token cursor, set filtering and `GetRecord` have **one code path**
whether the data is enveloped or module-split.

- **A record has no envelope**, so `identifier` and `datestamp` are derived per
  module at query time — `identifierPrefix` + `@id`, and
  `systemField[@name='__lastModified']/value` shifted to UTC by
  `mapping.timezoneOffset`. Each `[[modules]]` entry may override `records`,
  `identifier` and `datestamp`, and carries its own `[[modules.sets]]`
  allow-list.
- **The cursor spans the union**: rows from every module are sorted by
  `(datestamp, identifier)`. Identifiers are globally unique because of the
  module prefixes, so the pair is a total order across databases and a harvest
  neither skips nor repeats a record.
- **`deletedRecord` is `"no"` in module mode.** A module database is dropped and
  rebuilt on every ingest, so nothing records what vanished and no tombstone is
  ever served. Claiming `"persistent"` would be the one lie a harvester cannot
  detect, so the combination is **rejected at load time** — module mode plus a
  `deletedRecord` other than `"no"` is a configuration error, not a note in the
  docs. The source owns liveness; a harvester learns of a withdrawal by
  re-harvesting.

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
wrapper and terms use, and its terms are **per module**, because the three
modules are three different record shapes:

```toml
[[metadata.formats]]
prefix = "oai_dc"
kind = "derived"
wrapper = "oai_dc:dc"
namespaces = { oai_dc = "...", dc = "..." }

[[modules.terms]]         # attached to the last [[modules]] entry
term = "dc:type"
xpath = "//dataField[@name='ObjTechnicalTermClb']/value"
```

- **A term with no value is omitted, never emitted empty.** That is the whole
  of "correct Dublin Core" here — no blank `dc:date`.
- **The mapping is authored, not inferred.** Each term is a field the record
  really has, or a literal declared as one (`dc:language = "de"`). Nothing is
  invented: the Object module has no title field in the real export, so there
  is deliberately no `dc:title` for objects.
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

Sets are an **explicit allow-list** (`[[mapping.sets]]`). Each entry pairs an
XPath with a string label:

```toml
[[mapping.sets]]
spec  = "mimo"                 # setSpec published to harvesters
label = "Musikinstrumente"     # setName shown by ListSets
xpath = "m:moduleReference[@name='ObjObjectGroupsRef']/m:moduleReferenceItem[@moduleItemId='6054']"
```

A record is in the set when its XPath selects anything; value tests belong
inside the XPath (`m:dataField[@name='X'][m:value='Y']`).

Two consequences worth being deliberate about:

- **The setSpec is written by hand and never taken from a record.** No
  internal group id or organisational unit can reach OAI output by accident,
  and a group that is not listed here is not harvestable at all.
- `__orgUnit` is an organisational unit, not a set, and is not published as
  one.

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

The stored payload is the source record element **verbatim** — no filtering,
no omissions. `tests/test_ingest_basex.py` asserts each stored payload
`deep-equal`s the `moduleItem` it came from, so this cannot drift silently.
(Module mode's namespace stripping is the one deliberate exception, noted
above.)

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
  not created by transform expression". Design ingest as validate → write →
  reconcile with the counts coming from the read-only pass.
- **In TOML, a key written after a `[[table]]` header belongs to that table.**
  Putting a scalar below `[[mapping.sets]]` silently attaches it to the last set
  rule. Keep scalar keys above the first array-of-tables header.

## Testing conventions

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
- Dedicated dev user `oai` / `devpass` (admin is protected). Test DBs: `oaitest`,
  `mpxdb`, `riadb`, `riadb2`, `oai_provider_test`.
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

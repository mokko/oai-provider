# oai-provider

An OAI-PMH data provider backend, BaseX as the store. It ingests a multi-record
XML dump into an envelope per record, and serves the six OAI verbs from it.

## Shape

- **Generic XML in.** Nothing here knows about MuseumPlus. Which XPaths pull
  `identifier` / `datestamp` / `sets` out of a record is configuration
  (`oai.toml`).
- **One document per record in BaseX**, wrapped in an envelope that carries
  the OAI fields; the source element is kept whole inside it. `from`/`until`
  and set filtering then become attribute queries.
- **The extraction runs inside BaseX**, not in Python. Real XPath 3.1,
  in-process with the data — no lxml (a wheel risk on 3.14/arm64), and no
  `xml.etree` XPath-1.0 subset either. Python generates the query text and
  reports the result.
- **Async Python** (Starlette + httpx). The concurrency lives in the HTTP layer
  and the connection pool; the query work happens in Java, off the event loop.

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
see "Serving from the module databases" above. The paragraph that follows
describes the *enveloped* path, which reconciles and can therefore serve real
tombstones.

The source system does **not** record deletes, and a sync that only adds or
overwrites will serve vanished objects as live forever. No harvester can
recover from that. But the dump is complete, so absence is the delete signal:
reconciling a full dump is what makes `deletedRecord = "persistent"` honest in
the enveloped path. Set `deletedRecord = "transient"` to remove instead, or
`"no"` to leave liveness to the source.

The dump id is a content hash, so re-running an unchanged dump is idempotent
and cannot be mistaken for a dump that lost records.

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
- **The module tag distinguishes the records.** `@id` is unique only within a
  module — the ranges overlap (Object reaches 935894, Person 1764036,
  Multimedia 8533256, and one id appears as both an Object and a Multimedia
  record) — so each module carries an identifier prefix: `EM-object-`,
  `EM-person-`, `EM-asset-`.

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

## Payload

The stored payload is the source record element **verbatim** — no filtering,
no omissions. `tests/test_ingest_basex.py` asserts each stored payload
`deep-equal`s the `moduleItem` it came from, so this cannot drift silently.

## Serving

```bash
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

### Resumption tokens

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

### Metadata formats

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


### Scale

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
- **Datestamps must be shifted to UTC, not relabelled.** Source values are
  local wall clock with a space separator (`2021-09-07 10:56:34.615`). OAI
  wants `YYYY-MM-DDThh:mm:ssZ` in UTC; appending `Z` is wrong by the offset
  and breaks incremental harvesting quietly. `timezoneOffset` is configured.
- **A query returning a sequence of nodes is not a parseable document.** See
  `BaseXClient.query_nodes`.
- **`totalSize` is not a record count.** Zetcom's `<module totalSize="5">`
  is the server's result size, not the number of `moduleItem`s in the file.
- **RIA XML is not flat.** Most fields sit inside
  `repeatableGroup/repeatableGroupItem` at varying depth, so mapping XPaths
  must descend rather than follow fixed paths.

## Status

- BaseX 12.4, rootless, REST on **8080** (not 8984 — older docs are wrong for
  this build). Client/server 1984, stop port 8081.
- `uuid` is unreliable in the target MuseumPlus instance, so the integer
  `@id` is the identifier key.
- Remote: **https://github.com/mokko/oai-provider** (public, branch `main`).
- Tests: `./.venv/bin/python -m pytest tests/ -q` — 69 of them. The integration
  ones need BaseX running and skip themselves when it is not.
- Not yet done: the `oai_dc` mapping decision; an index/seek for large
  collections; and a real deployment story for the two secrets, which are dev
  placeholders (`OAI_BASEX_PASSWORD`, `OAI_TOKEN_SECRET`).

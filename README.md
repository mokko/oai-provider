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

The source system does **not** record deletes, and a sync that only adds or
overwrites will serve vanished objects as live forever. No harvester can
recover from that. But the dump is complete, so absence is the delete signal:
reconciling a full dump is what makes `deletedRecord = "persistent"` honest.
Set `deletedRecord = "transient"` to remove instead, or `"no"` to leave
liveness to the source.

The dump id is a content hash, so re-running an unchanged dump is idempotent
and cannot be mistaken for a dump that lost records.

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
OAI_BASEX_PASSWORD=... .venv/bin/python -m uvicorn oai.app:app --factory --port 8000
curl 'http://localhost:8000/oai?verb=Identify'
```

`GET /oai` and `POST /oai` reach the same handler, as the spec requires, and a
test asserts the two produce the same body. `GET /healthz` reports whether BaseX
answers.

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

Only `ria` (`kind = "passthrough"`) is configured: the stored payload is served
verbatim inside `<metadata>`, under Zetcom's namespace.

**There is no `oai_dc` yet, and that is an open decision** — deriving Dublin Core
needs a field-by-field mapping (title, creator, date, rights …) that has not been
made. It would be a new `kind` in `oai/config.py`, not a rewrite.

### Scale

Paging orders the whole matching set in BaseX (`order by` in `xq/page.xq.tmpl`)
and `ListRecords` fetches payloads in the same query, rather than seeking an
index. Fine for tens of thousands of records; worth an index and a seek before
this carries a large collection.

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

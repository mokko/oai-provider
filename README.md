# oai-provider

An OAI-PMH data provider backend, BaseX as the store. This repo currently
contains the **ingest path only** — the protocol layer (the six verbs,
resumption tokens) is not written yet.

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
- **Async Python** (Starlette + httpx later). The concurrency lives in the
  HTTP layer and the connection pool; the query work happens in Java, off the
  event loop.

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
- No remote is configured. Nothing here is pushed anywhere.
- Not yet written: `oai/protocol.py` (six verbs, error codes, resumption
  tokens) and `oai/app.py` (`GET`/`POST /oai`).

# Indexing the store — recommendation (not yet needed at sample size)

**Status: proposal, deliberately unbuilt.** At the current sample (5,884 records)
the page query is ~1 s and the sort is free; an index only earns its keep at the
scale of the real data. This file records the decision so it is not re-derived,
and names the measurement that should trigger the work.

## The real data changes the scale

The deployment holds roughly **300k Object records, plus Person and Multimedia
(asset)** records — two orders of magnitude above the sample chunk, whose
`Module` counts are Object 1000 / Person 652 / Multimedia 4232. Every per-page
cost below is currently *per matching record*, so it multiplies by ~50 at
deployment.

What the colleague did about indexes in his own `sync_*` setup is **unknown**.
If his BaseX has indexes ours does not, our queries read the same data through a
different (slower) path than his. Worth asking him before optimising — see
"Open question" at the end.

## What the code does today (measured over the 5,884-record sample)

`xq/page.xq.tmpl` derives each row on the fly, then filters and sorts:

- `identifier = identifierPrefix + @id` — computed per record, per request.
- `datestamp = local:oaiDate(systemField[@name='__lastModified']/value)` —
  parsed, shifted to UTC, reformatted, per record, per request.
- then `order by $r/@datestamp, $r/@identifier`, then `subsequence(1, limit+1)`.

And **there are no indexes on any module database** (`db:info//index/text()` is
empty for `sync_Object`, `sync_Person`, `sync_Multimedia`).

A local-client timing split (BaseX 12.4, sample size):

| query | time |
|---|---|
| derive rows + `datestamp ge …` (filter only) | 2.13 s |
| filter + `order by datestamp, identifier` + slice | 2.04 s |

So at this size **the sort is free and the cost is the scan + derivation**. The
index is not the win here yet; avoiding the per-request derivation is.

## Why "add an index" is not the first step

The two fields the window and the cursor depend on are **computed inside the
query**. A value you compute per request cannot be covered by an index. So:

**Step 0 — persist the cursor fields, or nothing is indexable.**
At ingest, write `oai:datestamp` (normalized UTC), `oai:identifier`, and
`oai:sets` as attributes on the wrapper `module` element the ingest already
synthesizes (the record payload underneath stays untouched, so the colleague's
path still reads identically). A `reindex` query backfills existing databases,
so this is adoptable without a full re-ingest. The alternative at ingest time is
to keep the wrapper minimal (raw `sync_*` layout), and that is what costs at
scale.

**Step 1 — `CREATE INDEX` on `oai:datestamp` (and the set value).**
Then `from`/`until`/set stop scanning and become index lookups — which *is* the
dominant cost today, per the table above.

**Caveat, stated plainly:** BaseX value indexes accelerate the **selection**
(equality and range). They do **not** provide ordered iteration — an `order by`
still materializes and sorts the matching set. So an index fixes the scan, not
the sort.

**Step 2 — attack the sort only if a full-set harvest demands it.**
The cursor is already `(datestamp, identifier)`. Turning that into a true *seek*
rather than a sort needs a pre-ordered structure. BaseX sorts document names and
`db:put` paths are addressable, but `db:open` is exact-match; **do not design on
a name-range seek until BaseX is shown to expose one.** The honest position: the
ordered seek is the uncertain part, the persist-then-index part is not.

## Already done: no redundant count per page

`completeListSize` used to need a **second full scan** (`count_matching`) on the
first page, on top of the page query. It no longer does: the first page counts
the pinned set from the scan it already runs (`xq/page.xq.tmpl` returns a
`total` attribute when `wantTotal=true`; the cursor is empty on page one, so
`$matching` *is* the pinned set). A resume carries the total in its token. Net:
one BaseX round trip per page instead of two on the first page, and one on every
resume. Locked in by `test_resumption_costs_one_query_per_page`.

## Trigger, and how to measure it

Do not build this at sample size. Inflate the database (or ingest the real dump)
and watch the **per-page time bend** as N grows — the same "does it scale with
page size, or is it flat?" test that proved the payload-after-paging fix. When
page time starts scaling with the *matching set* rather than the page, the
persist + index steps above are the answer; measure before and after, and keep
the curve.

## Open question for the colleague

Does `sync_Object` / `sync_Person` / `sync_Multimedia` in his setup carry
attribute or value indexes? If yes, match them — that is both the faster path
and the one our XPaths are meant to read identically against.

## Measured, 2026-10-05: the page dies before it is slow

Ingesting the real export gave the store **115,313 documents** (27 of the 47
chunks; Object 27,000 · Person 3,356 · Multimedia 84,957). With that in the
store, `tests/test_schema_conformance.py::test_the_response_validates_against_oai_pmh[ListRecords/lido]`
fails — not on the schema, but with **`HTTP 500: Interrupted.` after ~31 s**.

That is BaseX's **global `TIMEOUT` option, `30` in `~/basex/basex/.basex`**: it
stops a REST query at 30.0 s. Established by ruling the alternatives out:

- a `?timeout=120` REST parameter changed nothing — `500 Interrupted.` at 30.0 s;
- `SET TIMEOUT …` is refused ("Unknown option 'TIMEOUT'") and `db:option('TIMEOUT')`
  answers "Unknown option" too — it is a **global** option, read from the options
  file, so it takes a `.basex` edit and a restart;
- Jetty's own `idleTimeout` in `webapp/WEB-INF/jetty.xml` is 60 s, so it is not the
  one that fires;
- it is not memory: `usedmemory` was 24 MB, and the BaseX log line is
  `500 Interrupted. 30010.02 ms`.

**So the trigger this file was waiting for has arrived, and it arrived as a hard
wall rather than a bend.** A LIDO page over the real store cannot be answered at
all on this machine until either the limit is raised (which makes each page take
30 s+, at 300k records minutes) or Step 0/1 below is done. Raising `TIMEOUT`
buys a working-but-slow provider; it does not make the per-page cost any smaller,
and it is now measurable on real data rather than estimated.

## Measured, same day: what the ingest spends its time on

One 135 MB chunk, into empty databases, before the one-pass count query landed:

| phase | time |
|---|---|
| count pass, once per module (three parses of the file) | 6.1 s each |
| write pass: Object 1000 records | 14.1 s |
| write pass: Person **44** records | 12.7 s |
| write pass: Multimedia 3975 records | 9.2 s |

The 44-record module cost what the 1000-record one did: the time is `parse-xml`
reading the whole file, not the `db:put` calls. That is why the count pass is now
one query for every module (57 s → 44 s on that chunk) and why the per-record
skip idea is filed in `todo/skip-unchanged-records.md` rather than built.

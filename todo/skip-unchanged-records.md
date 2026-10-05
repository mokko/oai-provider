# Skipping records the store already has — open, not built

**Status: proposed, deliberately not built.** Ingest rewrites every record it is given:

```xquery
for $item in $module/*:moduleItem
return db:put($db, <stripped copy>, concat($moduleName, '-', $id, '.xml'))
```

keyed by id — so a record that is already in the store, byte for byte, is written again. The
idea is to compare the record's `__lastModified` with the stored copy's and skip the write when
the source's is not newer.

## What it would and would not buy

Measured 2026-10-05, one 135 MB chunk into empty databases:

- count pass, one query for all three modules: **6.2 s**
- write pass: Object 1000 records **14.1 s** · Person 44 records **12.7 s** · Multimedia
  3975 records **9.2 s**
- **the 44-record module cost what the 1000-record one cost**

So the time is `parse-xml` reading the whole 135–500 MB file for each query, not the `db:put`
calls: skipping the writes of unchanged records leaves that module's parse exactly where it
was. **A date check is therefore not the first thing to reach for.** What paid, in order:

1. ~~one count pass for all modules~~ — **done**: six parses per chunk became four, 57 s → 44 s
   on the measured chunk.
2. ~~a receipt, so a re-run skips a chunk that is already in the store~~ — **done**: the same
   chunk re-runs in 0.4 s instead of 44, and an interrupted import resumes.
3. one **write** pass for all modules — would take four parses to two (another ~26 s a chunk).
   Needs `db:put` to accept a computed database name, and a template that takes the
   module → database map. **Not checked yet.**
4. **this note** — `--only-newer`, worth it only when re-importing *overlapping* exports.

## The risk, which is why it would not be the default

`__lastModified` is the **source's** claim. A record edited in MuseumPlus without the stamp
moving would keep its old copy here for ever, and nothing would say so — the same class of
silent wrongness as a mapping XPath that matches nothing (`todo/xpath-validation.md`). The app
already refuses to serve a record with no usable datestamp rather than guess
(`module_counts.xq.tmpl` reports those as `undated`), and a skip would have to be at least as
careful:

- **opt-in** (`--only-newer`), never the default;
- compare against the **stored** copy's `__lastModified`, which survives ingest: the store is
  namespace-stripped, so the field is `systemField[@name='__lastModified']/value` either way;
- a missing or malformed stamp on **either** side means *write it*, not skip it;
- the run must **report how many were skipped**, so a chunk that "wrote nothing" is visible
  rather than surprising;
- and the receipt (#2) must not be confused with it: the receipt skips whole *files*, this
  would skip *records inside* one.

## What to find out first

**Whether a re-export overlaps at all.** If a re-delivered export is a fresh full dump, every
record differs in nothing but possibly the stamp, and this saves the write; if chunks are
disjoint, it saves nothing on a first ingest. Ask the colleague how a re-export is produced
before building it — the same question that blocks the tombstones
(`todo/deleted-records.md`).

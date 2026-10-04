# Deleted records / tombstones — open, no solution yet

**Status: a known gap, deliberately unresolved.** A tombstone is a record that says *there once
was a record*: a header with `status="deleted"` that a harvester can act on. We do not serve one,
and we have not decided how we would. This file records the problem and the one thing to find out
first — do not start building before that.

## What OAI asks for

`deletedRecord` is `"no" | "transient" | "persistent"`. A withdrawn record is served as a
`<header status="deleted">` (identifier + datestamp, and **no** `<metadata>`); `GetRecord` on it
returns that header with status deleted. `persistent` means the tombstone stays available forever,
`transient` means it may disappear, `no` means the repository never reports deletions.

## Where we are

- Module mode is the deployment path and serves **`deletedRecord = "no"`**, enforced at load
  (`oai/config.py`). The rule is right: see below.
- Ingest is **additive and never deletes**; `--reset` rebuilds a database from one dump. Neither
  path compares *what was there* with *what is here*, so a withdrawal cannot be detected — let
  alone served.
- Consequence: a harvester that only ever sees additions keeps a withdrawn object forever. And
  advertising `"persistent"` or `"transient"` while serving no tombstones is the one lie a
  harvester **cannot** detect — which is exactly why module mode rejects it rather than documenting
  it. The present position is honest, not complete.

## Why it is not just "implement X"

- **The source may already signal withdrawal.** `ObjPublicationStatusVoc` carries
  `Kriegsverlust`, `deakzessioniert (Abgabe)`, `Abgabe innerhalb der SPK` (plus `vorhanden`). If
  those mean "no longer held", a mapped predicate could serve a tombstone without any
  absence-tracking at all. That is a *question for the registrars / the colleague*, not a design.
- **Absence in a full dump** is the other signal, and only works if the dump is complete and its
  chunks share **one** dump id with reconcile run **once after the last chunk**. Reconcile per chunk
  tombstones everything not in that chunk — the enveloped path's own landmine, already documented in
  `AGENTS.md`.

## Sketches (not plans)

1. **Source-signalled** — a per-record predicate (publication status, or a future field) marks a
   record withdrawn; serve `status="deleted"`. Cheapest *if* the data means it.
2. **Absence-diff** — snapshot the identifier set before replacing it, diff against the new one,
   tombstone the missing. Needs the complete-dump guarantee and the shared dump-set id above.
   (The **enveloped** path already does this: reconcile + a content-hash dump id. It is not the
   deployment path.)
3. **Stay `"no"`** — the source owns liveness; a harvester learns of a withdrawal only by
   re-harvesting. Honest today, and the current default.

## Find this out first

**What did the colleague's setup do here — and does MuseumPlus ever mark a record withdrawn?**

Ask before building. Every option above moves `deletedRecord` off `"no"`, which module mode
currently **rejects** at load time, so the detection mechanism and the policy have to land together
— not as a config flag flipped on its own.

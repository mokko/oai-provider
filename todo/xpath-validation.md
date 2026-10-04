# A wrong mapping XPath is silent — an empty repository with no error

**Status: proposal, deliberately unbuilt.** Config XPaths are substituted into the generated queries
and never checked. A typo does not error: the expression matches nothing, rows are dropped, and the
repository looks *empty* rather than *misconfigured*. This file records the problem and the options;
do not build it without deciding the question at the end.

## The class of failure

Every XPath in `oai.toml` is trusted and interpolated (`oai/mapping.py`):

- a module's `datestamp` → `let $ds := local:oaiDate(string($r/{ds}), …)`; a record with an empty
  `$ds` is dropped (`where $raw ne '' and $ds`, `_module_block`);
- a module's `identifier` → `concat(prefix, string($r/{ident}))`; if it is wrong the identifier comes
  out empty or wrong and the record is unreachable;
- a module's `records` (schema location) → if it matches nothing, `collection(...){records}` yields
  nothing and the module is simply absent from every result set;
- a `[[modules.sets]]` `xpath` → `if (exists($r/{xpath})) then 'SPEC' else ()`; a miss makes the set
  silently inert (already documented, and the same trap generalised here);
- a derived term's `xpath` → filtered by `[normalize-space(string(.)) ne '']`; a miss emits no element
  and no warning.

None of these raises, and none is visible in the response: the worst case — a bad `datestamp` or a
`records` XPath that matches nothing — is a repository that answers `noRecordsMatch` to every harvest
and lists zero records, indistinguishable from a genuinely empty collection.

## Why it is worth catching

This is the failure the repo keeps meeting in other clothing (the inert set rule, the fixture value
that matched no real record, the `undated` records that vanish silently). It costs a debugging
session because the symptom is "the repository is empty", a full layer away from the cause.
It only ever appears at deployment or after a config edit, which is exactly when a fast, named error
is worth most.

## Options

1. **Syntax-check the generated query at load** — render each template and ask BaseX to parse it
   (`try { xquery:parse($q) } catch *`, or the `-q` path used for probes). Catches a *malformed*
   XPath, which is the rarer half; a syntactically valid path that matches nothing still passes.
2. **Dry-probe one stored document per module** — at startup (or `ingest --dry-run`), run the
   `identifier` / `datestamp` / each set expression over a sample record and report the hit counts.
   This catches the *matches-nothing* case, which is the real one. Needs BaseX at startup (the app
   already probes Saxon there) and one record per module.
3. **Ingest-time census** — extend `module_count.xq.tmpl` (which already reports `items`, `withId`,
   `undated`) with per-expression hit counts and print them. Cheapest, and the natural place; but it
   only runs when someone ingests, not when someone edits the config and restarts.
4. **Nothing** — document harder. The current position.

## Recommendation

Option 2 as a **startup warning, not a hard error**, plus option 3 in the ingest report. A hard error
is wrong: a legitimately empty collection (a module the dump does not carry) is not a mistake, and
the tool has no way to tell "empty on purpose" from "typo" — so report counts and let the operator
see a zero where they expected a number.

## What would settle it

May a collection legitimately serve zero records? If yes (the partial-dump case), the check is a
warning with hit counts; if a live deployment must never be empty, it can be a load-time error. Pick
that before building.

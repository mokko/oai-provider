# envelope.md — the enveloped storage path, and what dropping it cost

The repository used to be able to store the collection two ways. One of them —
the **enveloped** path — was removed. This note records what it did, why it
existed, and what is no longer possible without it, so it can be read before
anyone re-derives it. `git log` still has the implementation: the commit that
removed it is the one to read backwards.

## What it was

One BaseX database holding one document per record. The document was not the
record; it was a wrapper carrying the record's *OAI identity* as attributes,
with the untouched source element kept whole inside:

```xml
<env:record xmlns:env="urn:oai:envelope"
            env:identifier="spk-berlin.de:object-851035"
            env:datestamp="2025-08-12T09:08:20Z"
            env:status=""
            env:seen="query1035073-chunk1.xml:b43da566bd82aadc">
  <env:set>KK</env:set>
  <env:source>
    ... the moduleItem exactly as the dump carried it ...
  </env:source>
</env:record>
```

Document names were derived from the identifier
(`spk-berlin.de_object-851035.xml`), so the store was addressable and a `db:put`
on the same path was idempotent.

## Why it existed

Three properties, each of which the current store gives up:

1. **The record's OAI identity was data, not a computation.** `identifier`,
   `datestamp` and `sets` were read straight off the document's attributes. On
   the module path they are derived inside every query instead —
   `identifierPrefix` + `@id`, `__lastModified` shifted to UTC, a per-set XPath
   membership test. A value computed per request cannot be covered by an index;
   see `todo/index.md`.
2. **Deletion was detectable.** The `env:seen` attribute stamped each record
   with the *dump* it was last seen in (a content hash of the file). A full dump
   is complete, so absence in it is the delete signal: a reconcile pass read
   every document not carrying this run's stamp and, per the configured
   `deletedRecord` policy, either rewrote it as a bare `env:status="deleted"`
   marker or deleted it outright. That reconcile was the **only** thing in the
   repository that could serve `deletedRecord = "persistent"` honestly.
3. **The payload kept its namespaces.** The source element was stored verbatim,
   Zetcom namespace and all.

## Why it was removed

The deployment is the colleague's layout — one database per module
(`sync_Object` / `sync_Person` / `sync_Multimedia`), records namespace-stripped,
so that *his* queries read identically against ours. That layout is the one
actually in use, and keeping a second store meant two code paths for the same
six verbs. That is exactly where the expensive mistakes lived: a set declared
under the wrong table was silently inert, and the two modes disagreed about what
`deletedRecord` should say. Only one of the two paths was honest about the
deployment, so the other went.

## What we lose — stated plainly

1. **No tombstones, and no way to produce them.** `deletedRecord` is `"no"`, and
   there is no longer any code that can detect a withdrawal, let alone serve a
   `status="deleted"` header. A record withdrawn in MuseumPlus **stays live here
   indefinitely**. A harvester learns of the withdrawal only by re-harvesting and
   noticing an absence — and many harvesters never re-check what they already
   hold. This is the real loss, and it is a deletion rather than a switch: the
   mechanism is gone.
2. **Identity is computed per request.** Every identifier, datestamp and set
   membership is derived inside the query, so none of it can be indexed until it
   is persisted at ingest time (`todo/index.md`, "Step 0").
3. **The `ria` payload has no namespace.** Module ingest strips namespaces, so
   the verbatim passthrough record is unprefixed. The LIDO transform re-adds the
   Zetcom namespace (`local:zetcom`) because it needs it; plain `ria` consumers
   now get the stripped form.
4. **The content-hash dump id is gone**, and with it the idempotence argument
   for re-running an unchanged dump.

## If it is ever revived

Do not re-derive it blind — the design and its landmine are recorded here and in
`todo/deleted-records.md`:

- **Absence-diff is the shape**: snapshot the identifier set, compare it against
  the new dump, act on the difference.
- **The whole dump must be compared as one set.** Run per chunk, every record
  absent from *that* chunk looks deleted — chunk 2 would tombstone all of chunk
  1, and nothing would report an error.
- **Detection and policy have to land together.** `oai/config.py` rejects module
  mode with a `deletedRecord` other than `"no"` on purpose, so a tombstone
  mechanism cannot be half-added and quietly advertised.

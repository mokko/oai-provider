# oai-provider

An OAI-PMH data provider backend written in Python. With BaseX as the store,
generic MuseumPlus RIA XML goes in and the six OAI verbs come out. We are
providing several on-the-fly metadata formats like Dublin Core and Lido. Work
in progress. Nothing particularly well tested at this time. Written with
hermes-agent and deepseek-v4.1 flash.

## Shape

- **Generic XML in.** The provider never assumes a schema — which XPaths pull
  `identifier` / `datestamp` / `sets` out of a record is configuration
  (`oai.toml`).
- **One document per record in BaseX**, under the wrapper the module layout
  expects; the source element is kept whole inside it.
- **Namespaces are stripped on the way in, restored on the way out.** Module
  mode stores every record with its namespaces removed (`local:strip`), which is
  what matches the colleague's `sync_*` layout and conflates the RIA dialects.
  Every format that serves the record has each element rebuilt under Zetcom's
  namespace at serve time (`local:zetcom`) — the LIDO transform is written
  against RIA-qualified names, and OAI-PMH does not admit an unnamespaced
  payload at all. So the store stays namespace-free and nothing is duplicated.
- **The extraction runs inside BaseX** (real XPath 3.1), not in Python — no
  lxml, and no `xml.etree` XPath-1.0 subset either.
- **Async Python** (Starlette + httpx). The query work happens in Java, off
  the event loop.

## Requirements

- **Python ≥ 3.11** (see `pyproject.toml`). Hard dependency: `httpx`. To serve:
  `starlette`, `uvicorn`, `python-multipart` (POST form parsing). Dev: `pytest`.
  These are the `web` and `dev` extras.
- **BaseX 12.4**, installed rootless, with a JRE 21+. REST on port **8080**.
- **For the XSLT format (`lido`) only:**
  - **Saxon-HE 12.5** and **xmlresolver 5.2.2** on BaseX's classpath (`lib/`).
    BaseX's built-in `xslt:transform` is XSLT 1.0 and refuses the `zml2lido`
    stylesheet; Saxon runs it as 3.0. The app **probes for this at startup** and
    refuses to start without it, rather than advertise a format it cannot
    produce.
  - `vocmap.xml` and `europeanaFashion17.rdf` in **BaseX's working directory** —
    the stylesheet calls `document('file:vocmap.xml')`, and a relative `file:`
    URI resolves against the process cwd, not the stylesheet's.
  - A LIDO transform costs roughly 0.5 s per record, so a page of 100 is
    noticeably slower. `ria` and `oai_dc` are unaffected.

## Quick start

```bash
# BaseX (rootless, ~/basex/basex/) — REST on 8080
~/basex/basex/bin/basexhttp &

# Dependencies
uv pip install --python ./.venv/bin/python httpx starlette uvicorn python-multipart pytest

# Ingest a MuseumPlus dump (module mode: one database per module)
python tools/ingest.py sdata/Dump.xml --dry-run   # validate only
python tools/ingest.py sdata/Dump.xml
python tools/ingest.py sdata/                     # a directory: every chunk, in order
python tools/ingest.py sdata/zips/*.zip           # the archives as they arrived: unpacked,
                                                  # ingested, and the unpacked copy deleted
# A dump already in the store is skipped: a receipt beside the chunks says so, which is what
# makes an interrupted import resumable. --force redoes it; --dry-run only counts.

# Serve
cp .env.example .env      # set OAI_BASE_URL to where a harvester reaches you
.venv/bin/python -m uvicorn oai.app:app --factory --host 0.0.0.0 --port 8000
curl 'http://localhost:8000/oai?verb=Identify'
```

`GET /oai` and `POST /oai` reach the same handler, as the spec requires.
`GET /healthz` reports whether BaseX answers.

## Configuration

`oai.toml` holds the identity, the datestamp offset, the set allow-lists, the
metadata formats, and one `[[modules]]` block per MuseumPlus module. Each field
carries a comment saying why it is what it is. A trimmed example:

```toml
[identity]
repositoryName = "Museum collection (OAI provider recreation)"
baseURL = "http://localhost:8000/oai"   # overridden by OAI_BASE_URL
deletedRecord = "no"                    # module mode cannot serve deletions

[basex]
url = "http://localhost:8080/rest"
user = "oai"                            # password from OAI_BASEX_PASSWORD

[datestamps]
timezoneOffset = "+02:00"               # source is local wall clock -> UTC

[protocol]
pageSize = 100
tokenTTL = 86400

# formats: passthrough (verbatim) | derived (assembled) | xslt (stylesheet)
[[metadata.formats]]
prefix = "ria"
namespace = "http://www.zetcom.com/ria/ws/module"
kind = "passthrough"

# a derived format carries its whole mapping: one term per element, `module`
# naming whose records it applies to
[[metadata.formats]]
prefix = "oai_dc"
namespace = "http://www.openarchives.org/OAI/2.0/oai_dc/"
kind = "derived"
wrapper = "oai_dc:dc"
namespaces = { oai_dc = "…oai_dc/", dc = "http://purl.org/dc/elements/1.1/" }

[[metadata.formats.terms]]
module = "Object"
term = "dc:type"
xpath = "//dataField[@name='ObjTechnicalTermClb']/value"

# module mode: one database per module; a set belongs to the module it filters
[[modules]]
name = "Object"
database = "sync_Object"
identifierPrefix = "spk-berlin.de:object-"

# sets are an explicit allow-list; the setSpec is chosen by hand
[[modules.sets]]
spec = "KK"
label = "Kupferstichkabinett, Staatliche Museen zu Berlin"
xpath = "moduleReference[@name='ObjOwnerRef']/moduleReferenceItem[@moduleItemId='112264']"
```

The real file configures all three formats (`ria`, `oai_dc`, `lido`) and all
three modules; see `oai.toml` and `AGENTS.md`.

Deployment values come from the environment, not the tracked file:
`OAI_BASE_URL`, `OAI_BASEX_PASSWORD`, `OAI_TOKEN_SECRET` — a real variable, or
a gitignored `.env` beside the config (`.env.example` is the template;
`OAI_ENV_FILE` points elsewhere).

`OAI_TOKEN_SECRET` is the HMAC key that signs resumption tokens; a token *is*
the paging state (mapping fingerprint, pinned `until`, cursor, counts), so a
forged one must fail. `OAI_BASEX_PASSWORD` is the BaseX user's password. The
config **refuses to start** if `baseURL` is not loopback while either still holds
the shipped placeholder — a public checkout must never protect a reachable
repository. Loopback (the default) keeps the dev values working.

`[protocol] prettyPrint = true` indents the response for reading in a browser.
It is cosmetic — it never formats inside `<metadata>`, so a served payload is
byte-identical either way — and off by default (compact is the wire format).

## Test

```bash
./.venv/bin/python -m pytest tests/ -q   # the BaseX-backed ones skip if it's down
```

Responses are also checked for **schema conformance**, not just well-formedness:
`tests/test_schema_conformance.py` validates them against the vendored
`OAI-PMH.xsd` (plus `oai_dc`, the DCMI simple-DC schema, and Zetcom's
`module_1_6.xsd`), using `xmlschema` — pure Python, from the `dev` extra. See
`data/SOURCES.md` for where each schema came from.

`tools/oai_browser.py` is a small interactive client for poking a running
provider:

```bash
python3 tools/oai_browser.py http://localhost:8000/oai
```

It prompts for a base URL, `Identify`s, then offers the six verbs from a menu
(`--silent`, `--all` to follow resumption tokens, `--trace`). It is a
stdlib-only reimplementation of `oai_browser.pl` — the command-line OAI browser
by **Tim Brody** that ships with HTTP::OAI
(https://metacpan.org/release/HTTP-OAI/source/script/oai_browser.pl).

## Status

- Remote: **https://github.com/mokko/oai-provider** (public, branch `main`).
- All six verbs are implemented. Formats served: `ria` (verbatim), `oai_dc`
  (derived in XQuery), `lido` (via the vendored `zml2lido` stylesheet under
  Saxon).
- Module mode is the deployment path: `sync_Object` / `sync_Person` /
  `sync_Multimedia`, `deletedRecord = "no"`.
- Not done: an index/seek for large collections (see `todo/index.md`).

## Where to read more

- **`AGENTS.md`** — architecture, ingest, resumption tokens, the RIA mapping,
  and every gotcha that cost a round trip. Read it before changing anything.
- **`oai.toml`** — the mapping itself, with the data-coverage counts behind
  each Dublin Core choice.
- **`ZETCOM-FIELDS.md`** — what the Zetcom field/element names mean, in English
  and German, plus the naming grammar.
- **`todo/`** — decisions taken but deliberately not built, and the open
  questions: the index proposal (gated on a benchmark), the nginx
  reverse-proxy question, deleted-record/tombstone handling (blocked on what the
  colleague's setup does), and LIDO content validation (works; not switched on).

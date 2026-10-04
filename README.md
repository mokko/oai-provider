# oai-provider

An OAI-PMH data provider backend. MuseumPlus RIA XML goes in, the six OAI
verbs come out, with BaseX as the store.

## Shape

- **Generic XML in.** The provider never assumes a schema — which XPaths pull
  `identifier` / `datestamp` / `sets` out of a record is configuration
  (`oai.toml`).
- **One document per record in BaseX**, wrapped in an envelope; the source
  element is kept whole inside it.
- **The extraction runs inside BaseX** (real XPath 3.1), not in Python — no
  lxml, and no `xml.etree` XPath-1.0 subset either.
- **Async Python** (Starlette + httpx). The query work happens in Java, off
  the event loop.

## Quick start

```bash
# BaseX (rootless, ~/basex/basex/) — REST on 8080
~/basex/basex/bin/basexhttp &

# Dependencies
uv pip install --python ./.venv/bin/python httpx starlette uvicorn python-multipart pytest

# Ingest a MuseumPlus dump (module mode: one database per module)
python tools/ingest.py sdata/Dump.xml --dry-run   # validate only
python tools/ingest.py sdata/Dump.xml

# Serve
cp .env.example .env      # set OAI_BASE_URL to where a harvester reaches you
.venv/bin/python -m uvicorn oai.app:app --factory --host 0.0.0.0 --port 8000
curl 'http://localhost:8000/oai?verb=Identify'
```

`GET /oai` and `POST /oai` reach the same handler, as the spec requires.
`GET /healthz` reports whether BaseX answers.

## Configuration

`oai.toml` holds the identity, the XPath mapping, the set allow-list, the
metadata formats, and one `[[modules]]` block per MuseumPlus module. Each field
carries a comment saying why it is what it is.

Deployment values come from the environment, not the tracked file:
`OAI_BASE_URL`, `OAI_BASEX_PASSWORD`, `OAI_TOKEN_SECRET` — a real variable, or
a gitignored `.env` beside the config (`.env.example` is the template;
`OAI_ENV_FILE` points elsewhere).

## Test

```bash
./.venv/bin/python -m pytest tests/ -q   # the BaseX-backed ones skip if it's down
```

## Status

- Remote: **https://github.com/mokko/oai-provider** (public, branch `main`).
- All six verbs are implemented. Formats served: `ria` (verbatim), `oai_dc`
  (derived in XQuery), `lido` (via the vendored `zml2lido` stylesheet under
  Saxon).
- Module mode is the deployment path: `sync_Object` / `sync_Person` /
  `sync_Multimedia`, `deletedRecord = "no"`.
- Not done: an index/seek for large collections; a real secrets story (both
  secrets are dev placeholders).

## Where to read more

- **`AGENTS.md`** — architecture, ingest, resumption tokens, the RIA mapping,
  and every gotcha that cost a round trip. Read it before changing anything.
- **`oai.toml`** — the mapping itself, with the data-coverage counts behind
  each Dublin Core choice.

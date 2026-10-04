# Vendored schemas — where each came from

These are **unmodified copies**, kept so tests can validate responses without
reaching the network. Hashes are at the bottom: re-fetch and compare before
updating a copy, so a change is visible as a change.

## OAI-PMH and Dublin Core — `data/oai/`

| file | source |
|---|---|
| `OAI-PMH.xsd` | https://www.openarchives.org/OAI/2.0/OAI-PMH.xsd |
| `oai_dc.xsd` | https://www.openarchives.org/OAI/2.0/oai_dc.xsd |
| `simpledc20021212.xsd` | https://www.dublincore.org/schemas/xmls/simpledc20021212.xsd |

`oai_dc.xsd` `import`s the DCMI schema from a **remote URL**; it is left
verbatim, and tests map that namespace to the local copy through the validator's
`locations` table rather than by editing the file.

## Zetcom module XML — `data/zetcom/module_1_6.xsd`

Copied from **https://github.com/mokko/MpApi**, path
`src/mpapi/data/xsd/module_1_6.xsd` (MpApi is an unofficial MuseumPlus RIA
client; the schema is version 1.6, self-contained — no imports — and its
`targetNamespace` is exactly `http://www.zetcom.com/ria/ws/module`).

Note what it settles: Zetcom publishes **no schema** for RIA, and the dump
carries no `schemaLocation` or DOCTYPE, which is why `ria` payloads looked
unvalidatable. This file is one, and our served `ria` payload validates against
it.

**Licence:** MpApi is **GPL-3.0**; this repository states no licence. That is a
question to settle deliberately rather than by copying a file in — the same
copyright holder owns both, so it is a licensing decision, not a permissions
problem. Recorded here so it is not discovered by accident.

Also referenced, not vendored: `search_1_6.xsd`, `search_1_8.xsd`,
`session_1_0.xsd`, `vocabulary_1_1.xsd` — same directory in MpApi, unused here.

## LIDO — `data/lido/`

Vendored with the zml2lido drop. **Not used for validation as it stands**: it
imports `xml.xsd` (a 2001-era URL) and GML 3.1.1 over plain http, so loading it
fetches the GML tree — and the type xmlschema then rejects lives in **GML, not in
LIDO**, so that failure says nothing about our output either way. Repointing the
two imports at local copies (the current `xml.xsd`, plus a stub for the three GML
elements LIDO actually references) makes it load and our payload validate, under
xmlschema and without lxml. Not enabled — see `todo/lido-validation.md`.

## Hashes (sha256)

```
7383d21bca49c1186627e820a5da7ec209febfc5b4295fada39720ad0cebee9a  data/oai/oai_dc.xsd
da217eb992fff3261ce109282e8be17f46f84cfb2e417667aa5c9359744fc55d  data/oai/OAI-PMH.xsd
e9542e0cd08b59ad3ad994f50226dd7e6707792327b08f46eaa6250dd728d3aa  data/oai/simpledc20021212.xsd
2925d4121dbec9477a7d3e7202cad88330cd9002abc7c38194073c3a121367bf  data/zetcom/module_1_6.xsd
```

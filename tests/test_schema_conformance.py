"""Do the responses we serve actually validate?

Well-formedness is not conformance. A response can be perfectly well-formed XML
and still be rejected by a harvester that validates against `OAI-PMH.xsd` —
which is exactly what this provider did for a long time: `GetRecord` and
`ListRecords` served `<header>`/`<metadata>` with no `<record>` wrapper, and
nothing in the suite objected because nothing checked *shape*.

Everything here is validated against the **vendored, unmodified** schemas in
`data/` (provenance and hashes in `data/SOURCES.md`), using `xmlschema`. Not
lxml: `xmlschema` is pure Python, so this runs anywhere the rest of the suite
does. (LIDO *content* is the one thing left unvalidated — see the last test and
`todo/lido-validation.md`.)

Why the strictness differs per format: `<metadata>` is
`<any namespace="##other" processContents="strict"/>`, so a strict processor
must find a **global** element declaration for the payload.

* `oai_dc:dc` is global ⇒ the whole document validates strictly.
* `moduleItem` — the `ria` payload — is only a **local** element in Zetcom's
  schema (the sole global element is `application`), so no strict processor can
  resolve it. The envelope is validated with that one wildcard relaxed, and the
  payload is validated separately, wrapped in the `application/modules/module`
  skeleton the schema declares.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

xmlschema = pytest.importorskip("xmlschema")

from oai.basex import BaseXClient  # noqa: E402
from oai.config import Config  # noqa: E402
from oai.protocol import Provider, q, serialise  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ZETCOM_NS = "http://www.zetcom.com/ria/ws/module"

# The vendored schemas import each other by remote URL; point every namespace at
# its local copy instead of editing the files.
LOCATIONS = {
    "http://www.openarchives.org/OAI/2.0/oai_dc/": str(DATA / "oai/oai_dc.xsd"),
    "http://purl.org/dc/elements/1.1/": str(DATA / "oai/simpledc20021212.xsd"),
    ZETCOM_NS: str(DATA / "zetcom/module_1_6.xsd"),
}

_STRICT_WILDCARD = '<any namespace="##other" processContents="strict"/>'


def _oai_schema(*, resolve_payload: bool):
    """`OAI-PMH.xsd`, optionally with the one payload wildcard relaxed.

    Relaxing it is not a free pass: it is used only where the payload element is
    not globally declared, and that payload is then validated against its own
    schema below.
    """
    text = (DATA / "oai" / "OAI-PMH.xsd").read_text(encoding="utf-8")
    if not resolve_payload:
        assert _STRICT_WILDCARD in text, "OAI-PMH.xsd changed shape"
        text = text.replace(
            _STRICT_WILDCARD, '<any namespace="##other" processContents="lax"/>'
        )
    return xmlschema.XMLSchema(text, locations=LOCATIONS)


def _client(config: Config) -> BaseXClient:
    return BaseXClient(
        config.basex.url, config.basex.user, config.basex.password, config.basex.timeout
    )


@pytest.fixture(scope="module")
def live() -> Config:
    """The shipped config, page size 1 — validation is slow, one record is enough."""
    cfg = Config.load(ROOT / "oai.toml")

    async def check() -> bool:
        async with _client(cfg) as bx:
            return await bx.ping()

    try:
        ok = asyncio.run(check())
    except Exception:
        ok = False
    if not ok:
        pytest.skip("BaseX is not answering")
    return dataclasses.replace(
        cfg, protocol=dataclasses.replace(cfg.protocol, page_size=1)
    )


def _serve(config: Config, *pairs: tuple[str, str]) -> bytes:
    """The exact bytes a request produces."""

    async def run() -> bytes:
        async with _client(config) as bx:
            root = await Provider(config, bx).handle(list(pairs))
        return serialise(root)

    return asyncio.run(run())


@pytest.fixture(scope="module")
def an_identifier(live: Config) -> str:
    """A real identifier, read from the repository rather than hard-coded."""
    body = _serve(live, ("verb", "ListIdentifiers"), ("metadataPrefix", "ria"))
    first = ET.fromstring(body).find(f".//{q('ListIdentifiers')}/{q('header')}")
    assert first is not None, "no identifiers served"
    return first.findtext(q("identifier")) or ""


# label, request, payload element is globally declared (so strict is possible)
CASES = [
    ("Identify", (("verb", "Identify"),), True),
    ("ListSets", (("verb", "ListSets"),), True),
    ("ListIdentifiers/ria", (("verb", "ListIdentifiers"), ("metadataPrefix", "ria")), True),
    ("ListIdentifiers/oai_dc", (("verb", "ListIdentifiers"), ("metadataPrefix", "oai_dc")), True),
    ("ListRecords/oai_dc", (("verb", "ListRecords"), ("metadataPrefix", "oai_dc")), True),
    ("ListRecords/ria", (("verb", "ListRecords"), ("metadataPrefix", "ria")), False),
    ("ListRecords/lido", (("verb", "ListRecords"), ("metadataPrefix", "lido")), False),
]


@pytest.mark.parametrize("label,pairs,strict", CASES, ids=[c[0] for c in CASES])
def test_the_response_validates_against_oai_pmh(live: Config, label, pairs, strict) -> None:
    schema = _oai_schema(resolve_payload=strict)
    schema.validate(_serve(live, *pairs))


@pytest.mark.parametrize(
    "fmt,strict",
    [("oai_dc", True), ("ria", False), ("lido", False)],
    ids=["oai_dc", "ria", "lido"],
)
def test_get_record_validates(live: Config, an_identifier, fmt, strict) -> None:
    schema = _oai_schema(resolve_payload=strict)
    body = _serve(
        live,
        ("verb", "GetRecord"),
        ("identifier", an_identifier),
        ("metadataPrefix", fmt),
    )
    schema.validate(body)


def test_the_ria_payload_is_valid_zetcom_module_xml(live: Config, an_identifier) -> None:
    """The payload the wildcard could not resolve, validated where it lives.

    Zetcom's schema declares exactly one global element, `application`, so the
    record is validated inside that skeleton — the same shape the ingest stores.
    """
    body = _serve(
        live,
        ("verb", "GetRecord"),
        ("identifier", an_identifier),
        ("metadataPrefix", "ria"),
    )
    payload = ET.fromstring(body).find(f".//{q('metadata')}")
    assert payload is not None and len(payload), "no payload served"
    record = list(payload)[0]
    assert record.tag == f"{{{ZETCOM_NS}}}moduleItem", "payload is not Zetcom XML"

    app = ET.Element(f"{{{ZETCOM_NS}}}application")
    mods = ET.SubElement(app, f"{{{ZETCOM_NS}}}modules")
    mod = ET.SubElement(mods, f"{{{ZETCOM_NS}}}module")
    mod.set("name", "Object")
    mod.append(record)

    schema = xmlschema.XMLSchema(str(DATA / "zetcom" / "module_1_6.xsd"))
    schema.validate(ET.tostring(app, encoding="unicode"))


def test_lido_content_is_not_validated_yet() -> None:
    """A recorded gap, not an oversight: LIDO records are envelope-checked only.

    The vendored schema imports `xml.xsd` and GML 3.1.1 over **plain http**, so
    loading it reaches for the GML tree and its transitive imports — a moving
    network dependency a test suite should not carry. Note *where* it fails: the
    type involved lives in GML, not in LIDO, so this says nothing about our
    payload either way. Repointing those two imports at local copies makes it
    load and our payload validate (verified, with this same validator and with
    lxml) — see `todo/lido-validation.md`.
    """
    text = (DATA / "lido" / "lido-v1.0.xsd").read_text(encoding="utf-8")
    assert 'schemaLocation="http://www.w3.org/2001/03/xml.xsd"' in text
    assert 'schemaLocation="http://schemas.opengis.net' in text
    # and nothing in LIDO itself derives from a GML type - only these three
    # element references, which is why an element-only stub is enough
    referenced = sorted(
        {m for m in re.findall(r"gml:([A-Za-z]+)", text)}
    )
    assert referenced == ["LineString", "Point", "Polygon"], referenced
    assert "DefinitionType" not in text

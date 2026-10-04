"""The interactive client's response helpers - no BaseX, no HTTP.

`resumption_token` initially used `root.find()`, which only looks at the root's
direct children - but the token is nested inside the verb element, so it
returned empty for every list response and `--all` could never follow a
harvest. These tests pin the fix.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "oai_browser", ROOT / "tools" / "oai_browser.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


browser = _load()

PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListIdentifiers>
    <header><identifier>x:1</identifier><datestamp>2020-01-01T00:00:00Z</datestamp></header>
    <resumptionToken completeListSize="2" cursor="1">tok-abc</resumptionToken>
  </ListIdentifiers>
</OAI-PMH>"""

FINAL_PAGE = """<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListIdentifiers><resumptionToken completeListSize="2" cursor="2"/></ListIdentifiers>
</OAI-PMH>"""

ERR = """<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <error code="noRecordsMatch">no records match</error>
</OAI-PMH>"""


def test_resumption_token_is_found_nested_in_the_verb_element() -> None:
    assert browser.resumption_token(PAGE) == "tok-abc"


def test_empty_and_absent_tokens_are_empty() -> None:
    # the terminal token is an empty element: nothing to follow
    assert browser.resumption_token(FINAL_PAGE) == ""
    assert browser.resumption_token("<OAI-PMH/>") == ""


def test_oai_error_is_reported_then_absent() -> None:
    assert browser.oai_error(ERR) == ("noRecordsMatch", "no records match")
    assert browser.oai_error(PAGE) is None

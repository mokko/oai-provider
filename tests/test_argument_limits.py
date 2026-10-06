"""Request arguments are bounded, and a hostile name cannot break the response.

The OAI path already rejects repeated, unknown and mutually-exclusive
arguments. What these tests add is the *bounds* - the lengths and the count
that stop a client forcing unbounded work - and the one that is easy to get
wrong: an argument name becomes an XML attribute name, so a name that is not an
NCName must be kept out of `<request>` while still being named in the error.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from oai.protocol import (
    MAX_ARG_LEN,
    MAX_ARGS,
    ProtocolError,
    make_request_el,
    parse_args,
    q,
)


def _code(pairs) -> str:
    with pytest.raises(ProtocolError) as exc:
        parse_args(list(pairs))
    return exc.value.code


# -- length bounds ----------------------------------------------------------


def test_an_argument_at_the_limit_is_accepted() -> None:
    req = parse_args([("verb", "ListRecords"), ("metadataPrefix", "ria"),
                      ("set", "x" * MAX_ARG_LEN)])
    assert len(req.args["set"]) == MAX_ARG_LEN


def test_one_character_over_the_limit_is_bad_argument() -> None:
    assert _code([("verb", "ListRecords"), ("metadataPrefix", "ria"),
                  ("set", "x" * (MAX_ARG_LEN + 1))]) == "badArgument"


def test_a_huge_argument_name_is_bounded() -> None:
    # a name is echoed into <request> as an attribute, so an unbounded name is
    # unbounded output even when its value is short
    assert _code([("verb", "Identify"), ("x" * 5000, "v")]) == "badArgument"


def test_a_huge_argument_name_never_reaches_the_response() -> None:
    el = make_request_el("http://example.org/oai", [("x" * 5000, "v")])
    assert all(len(k) <= 100 for k in el.attrib)



# -- count bound ------------------------------------------------------------


def test_the_argument_count_is_capped() -> None:
    pairs = [("verb", "Identify")] + [(f"a{i}", "v") for i in range(MAX_ARGS)]
    assert _code(pairs) == "badArgument"


def test_the_cap_does_not_fire_for_a_normal_request() -> None:
    pairs = [("verb", "ListRecords"), ("metadataPrefix", "ria"),
             ("from", "2020-01-01"), ("until", "2021-01-01"), ("set", "KK")]
    req = parse_args(pairs)
    assert req.verb == "ListRecords"


# -- names and values -------------------------------------------------------


def test_a_colon_in_an_argument_name_never_reaches_the_request_element() -> None:
    """`&spk-berlin.de:object-1` (a missing `identifier=`) would otherwise be an
    undeclared namespace prefix, and the whole response would stop being
    well-formed XML."""
    pairs = [("verb", "GetRecord"), ("spk-berlin.de:object-1", ""),
             ("metadataPrefix", "ria")]
    el = make_request_el("http://example.org/oai", pairs)
    assert "spk-berlin.de:object-1" not in el.attrib
    # ...and it serialises and parses back, which is the point
    assert ET.fromstring(ET.tostring(el)) is not None


def test_a_foreign_name_is_still_named_in_the_error() -> None:
    pairs = [("verb", "Identify"), ("x:y", "1")]
    with pytest.raises(ProtocolError) as exc:
        parse_args(pairs)
    assert exc.value.code == "badArgument"
    assert "x:y" in exc.value.message


def test_a_value_needing_xml_escaping_round_trips() -> None:
    pairs = [("verb", "GetRecord"), ("metadataPrefix", "ria"),
             ("identifier", 'a & b <c> "d"')]
    el = make_request_el("http://example.org/oai", pairs)
    again = ET.fromstring(ET.tostring(el))
    assert again.get("identifier") == 'a & b <c> "d"'


@pytest.mark.parametrize("name", ["1bad", "with space", "-lead", "", "a:b", "üñí"])
def test_a_name_that_is_not_an_ncname_is_dropped_from_the_echo(name) -> None:
    el = make_request_el("http://example.org/oai", [(name, "v")])
    assert name not in el.attrib


@pytest.mark.parametrize("name", ["verb", "metadataPrefix", "set", "from", "_x", "a.b-c"])
def test_a_legal_ncname_is_echoed(name) -> None:
    el = make_request_el("http://example.org/oai", [(name, "v")])
    assert el.get(name) == "v"


# -- required vs empty ------------------------------------------------------


def test_an_empty_required_argument_is_missing_not_present() -> None:
    """A bound-but-empty value is not the same as absent, and the spec wants
    the argument to carry a value."""
    assert _code([("verb", "GetRecord"), ("metadataPrefix", "ria"),
                  ("identifier", "")]) == "badArgument"


def test_an_empty_exclusive_token_is_bad_argument() -> None:
    assert _code([("verb", "ListRecords"), ("resumptionToken", "")]) == "badArgument"


def test_an_error_response_keeps_the_oai_namespace() -> None:
    el = make_request_el("http://example.org/oai", [("verb", "Identify")])
    assert el.tag == q("request")

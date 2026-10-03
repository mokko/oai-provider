"""Protocol unit tests: no BaseX, no HTTP.

Covers the parts where being wrong is silent - argument exclusivity, token
forgery, token expiry, and day-granularity bounds.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from oai.config import Config
from oai.mapping import QueryBuilder
from oai.protocol import (
    Provider,
    ProtocolError,
    TokenState,
    decode_token,
    encode_token,
    normalise_from,
    normalise_until,
    parse_args,
)

ROOT = Path(__file__).resolve().parent.parent
SECRET = "test-secret"


@pytest.fixture()
def config() -> Config:
    return Config.load(ROOT / "oai.toml")


def fp() -> str:
    return QueryBuilder(Config.load(ROOT / "oai.toml").mapping).fingerprint()


# -- arguments --------------------------------------------------------------


def test_missing_verb_is_bad_verb() -> None:
    with pytest.raises(ProtocolError) as err:
        parse_args([("metadataPrefix", "ria")])
    assert err.value.code == "badVerb"


def test_unknown_verb_is_bad_verb() -> None:
    with pytest.raises(ProtocolError) as err:
        parse_args([("verb", "Harvest")])
    assert err.value.code == "badVerb"


def test_repeated_argument_is_bad_argument() -> None:
    with pytest.raises(ProtocolError) as err:
        parse_args([("verb", "Identify"), ("verb", "ListSets")])
    assert err.value.code == "badArgument"


def test_token_is_exclusive() -> None:
    """A resumptionToken combined with other arguments is badArgument: a
    harvester that does this is confused about which request it is resuming."""
    with pytest.raises(ProtocolError) as err:
        parse_args(
            [("verb", "ListRecords"), ("resumptionToken", "abc"), ("set", "mimo")]
        )
    assert err.value.code == "badArgument"
    assert "exclusive" in err.value.message


def test_token_alone_is_accepted() -> None:
    req = parse_args([("verb", "ListRecords"), ("resumptionToken", "abc")])
    assert req.token == "abc"


def test_illegal_argument_is_rejected() -> None:
    with pytest.raises(ProtocolError) as err:
        parse_args([("verb", "ListSets"), ("foo", "bar")])
    assert err.value.code == "badArgument"


def test_list_records_requires_metadata_prefix() -> None:
    with pytest.raises(ProtocolError) as err:
        parse_args([("verb", "ListRecords")])
    assert err.value.code == "badArgument"


def test_get_record_requires_both_arguments() -> None:
    with pytest.raises(ProtocolError):
        parse_args([("verb", "GetRecord"), ("identifier", "x")])
    ok = parse_args(
        [("verb", "GetRecord"), ("identifier", "x"), ("metadataPrefix", "ria")]
    )
    assert ok.args["identifier"] == "x"


# -- datestamps -------------------------------------------------------------


def test_day_granularity_bounds_expand_to_whole_days(config: Config) -> None:
    """A day-precision `until` means the whole of that day, not midnight at
    its start - otherwise a harvest to 2026-09-19 silently drops that day."""
    assert normalise_from("2026-09-19", "YYYY-MM-DDThh:mm:ssZ") == (
        "2026-09-19T00:00:00Z"
    )
    assert normalise_until("2026-09-19", "YYYY-MM-DDThh:mm:ssZ") == (
        "2026-09-19T23:59:59Z"
    )
    # explicit times pass through untouched
    assert normalise_until("2026-09-19T06:15:00Z", "YYYY-MM-DDThh:mm:ssZ") == (
        "2026-09-19T06:15:00Z"
    )


# -- resumption tokens ------------------------------------------------------


def state(**over) -> TokenState:
    base = dict(
        fingerprint=fp(),
        pinned_until="2026-09-29T12:00:00Z",
        last_datestamp="2026-09-19T06:15:00Z",
        last_identifier="spk-berlin.de:EM-objId-1001",
        prefix="ria",
        issued_at=int(time.time()),
        delivered=100,
        complete_list_size=250,
        set_spec="mimo",
        from_="2026-01-01T00:00:00Z",
    )
    base.update(over)
    return TokenState(**base)  # type: ignore[arg-type]


def test_token_roundtrip() -> None:
    original = state()
    back = decode_token(
        encode_token(original, SECRET), SECRET, fp(), ttl=3600
    )
    assert back.last_identifier == original.last_identifier
    assert back.pinned_until == original.pinned_until
    assert back.complete_list_size == 250
    assert back.set_spec == "mimo"


def test_token_is_opaque_not_readable() -> None:
    """Harvesters must treat tokens as opaque; ours should not invite them to
    parse it, so the payload is base64 rather than plain JSON."""
    token = encode_token(state(), SECRET)
    assert "spk-berlin" not in token
    assert "{" not in token


def test_tampered_token_is_rejected() -> None:
    token = encode_token(state(), SECRET)
    body, _, sig = token.partition(".")
    # Flip a character *inside* the signature, never the last one. The base64
    # is unpadded, so the final character carries padding bits: changing only
    # those decodes to the very same bytes, the signature still verifies, and
    # the test would fail for a reason that is not the feature. A character
    # that is not last always changes the decoded bytes.
    at = len(sig) // 2
    forged = body + "." + sig[:at] + ("A" if sig[at] != "A" else "B") + sig[at + 1 :]
    with pytest.raises(ProtocolError) as err:
        decode_token(forged, SECRET, fp(), ttl=3600)
    assert err.value.code == "badResumptionToken"


def test_token_from_another_mapping_is_rejected() -> None:
    """The whole point of the fingerprint: a token issued under a different
    mapping must not be honoured, because the same arguments no longer mean
    the same result set."""
    token = encode_token(state(fingerprint="deadbeefdeadbeef"), SECRET)
    with pytest.raises(ProtocolError) as err:
        decode_token(token, SECRET, fp(), ttl=3600)
    assert err.value.code == "badResumptionToken"
    assert "mapping" in err.value.message


def test_expired_token_is_rejected() -> None:
    token = encode_token(state(issued_at=int(time.time()) - 10_000), SECRET)
    with pytest.raises(ProtocolError) as err:
        decode_token(token, SECRET, fp(), ttl=3600)
    assert err.value.code == "badResumptionToken"
    assert "expired" in err.value.message


def test_unsigned_token_rejected_when_secret_configured() -> None:
    token = encode_token(state(), "")  # no signature
    with pytest.raises(ProtocolError) as err:
        decode_token(token, SECRET, fp(), ttl=3600)
    assert err.value.code == "badResumptionToken"


def test_garbage_token_is_rejected_not_crashed() -> None:
    for bad in ("", "!!!", "not.a.token", "YWJjZA"):
        with pytest.raises(ProtocolError):
            decode_token(bad, SECRET, fp(), ttl=3600)


def test_cursor_carries_a_total_order_key() -> None:
    """Both halves of the keyset cursor must be in the token. A datestamp
    alone is not unique, and a non-unique cursor either skips peers or
    repeats them - skipping ends the harvest as if complete."""
    back = decode_token(encode_token(state(), SECRET), SECRET, fp(), ttl=3600)
    assert back.last_datestamp and back.last_identifier


def test_fingerprint_changes_when_the_mapping_changes(config: Config) -> None:
    import dataclasses

    before = QueryBuilder(config.mapping).fingerprint()
    changed = dataclasses.replace(config.mapping, identifier="@uuid")
    assert QueryBuilder(changed).fingerprint() != before


# -- metadata formats -------------------------------------------------------


def test_unknown_prefix_is_cannot_disseminate_format(config: Config) -> None:
    provider = Provider(config, client=None)  # type: ignore[arg-type]
    with pytest.raises(ProtocolError) as err:
        provider.format_for("oai_dc")
    assert err.value.code == "cannotDisseminateFormat"


def test_configured_prefix_resolves(config: Config) -> None:
    provider = Provider(config, client=None)  # type: ignore[arg-type]
    assert provider.format_for("ria").namespace == (
        "http://www.zetcom.com/ria/ws/module"
    )

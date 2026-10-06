"""A bad resumption token must be *rejected*, never crash the server.

`decode_token` parses bytes a client sent with `json.loads`, so the payload is
attacker-controlled. A body that is valid JSON but not an object - `null`,
`[]`, `0`, `"x"`, `true` - decodes fine and then reaches the field reads, where
it used to raise `AttributeError`: an unhandled 500 where the harvester
deserves `badResumptionToken`. These tests pin that every malformed shape is a
`ProtocolError` with that code, and nothing else.

They are pure Python: no BaseX, so they run anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from oai.protocol import (
    MAX_TOKEN_LEN,
    TOKEN_VERSION,
    ProtocolError,
    TokenError,
    decode_token,
    encode_token,
    TokenState,
)

FP = "fingerprint-abc"
SECRET = "a-real-secret"


def _enc(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _signed(payload: object, secret: str = SECRET) -> str:
    """A token whose signature is valid, whatever the payload is."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()[:16]
    return f"{_enc(raw)}.{_enc(sig)}"


def _good(**overrides) -> dict:
    base = {
        "v": TOKEN_VERSION,
        "fp": FP,
        "u": "2026-01-01T00:00:00Z",
        "ds": "2020-01-01T00:00:00Z",
        "id": "spk-berlin.de:object-1",
        "p": "ria",
        "iss": int(time.time()),
        "n": 10,
        "c": 3,
        "s": "",
        "f": "",
    }
    base.update(overrides)
    return base


# -- valid round trip -------------------------------------------------------


def test_a_good_token_decodes() -> None:
    state = decode_token(_signed(_good()), SECRET, FP, ttl=3600)
    assert state.last_identifier == "spk-berlin.de:object-1"
    assert state.delivered == 3
    assert state.complete_list_size == 10


def test_unknown_extra_fields_are_ignored() -> None:
    """A token from a newer build must not be rejected merely for carrying a
    field this build does not know - only for disagreeing on the ones it does."""
    state = decode_token(_signed(_good(future="whatever")), SECRET, FP, ttl=3600)
    assert state.prefix == "ria"


# -- the crash this file exists for ----------------------------------------


@pytest.mark.parametrize("payload", [None, [], 0, "x", True, 3.5])
def test_a_json_body_that_is_not_an_object_is_rejected(payload) -> None:
    with pytest.raises(TokenError) as exc:
        decode_token(_signed(payload), SECRET, FP, ttl=3600)
    assert exc.value.code == "badResumptionToken"


@pytest.mark.parametrize(
    "field,bad",
    [("c", {"nested": 1}), ("c", "many"), ("n", []), ("n", "ten"), ("iss", "soon")],
)
def test_a_field_of_the_wrong_type_is_rejected(field, bad) -> None:
    with pytest.raises(TokenError) as exc:
        decode_token(_signed(_good(**{field: bad})), SECRET, FP, ttl=0)
    assert exc.value.code == "badResumptionToken"


def test_a_non_text_cursor_field_becomes_empty_not_a_crash() -> None:
    state = decode_token(_signed(_good(ds=["x"], id={"a": 1})), SECRET, FP, ttl=3600)
    assert state.last_datestamp == ""
    assert state.last_identifier == ""


# -- the other loud failures ------------------------------------------------


def test_wrong_version_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_token(_signed(_good(v=TOKEN_VERSION + 1)), SECRET, FP, ttl=3600)


def test_another_mapping_fingerprint_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_token(_signed(_good()), SECRET, "a-different-mapping", ttl=3600)


def test_missing_signature_when_a_secret_is_set() -> None:
    with pytest.raises(TokenError):
        decode_token(_enc(b'{"v":1}'), SECRET, FP, ttl=3600)


def test_an_unsigned_token_round_trips_when_no_secret_is_configured() -> None:
    """The dev/loopback path: no secret means unsigned tokens are the norm."""
    state = TokenState(
        fingerprint=FP, pinned_until="x", issued_at=int(time.time())
    )
    decoded = decode_token(encode_token(state, ""), "", FP, ttl=3600)
    assert decoded.fingerprint == FP


def test_an_over_long_token_is_rejected_before_any_decoding() -> None:
    with pytest.raises(TokenError):
        decode_token("A" * (MAX_TOKEN_LEN + 1), SECRET, FP, ttl=3600)


# -- the flaky-test lesson, made explicit ----------------------------------


def test_a_tampered_signature_is_rejected() -> None:
    """Flip a character that is **not** the last one.

    The final character of an unpadded base64url value carries only padding
    bits, so flipping it can decode to the *same* bytes - the signature still
    verifies and the test fails for a reason that is not the feature, about
    half the time. Mutating the middle survives the encoding.
    """
    token = _signed(_good())
    body, _, sig = token.partition(".")
    assert len(sig) > 4
    mid = len(sig) // 2
    flipped = "B" if sig[mid] == "A" else "A"
    tampered = f"{body}.{sig[:mid]}{flipped}{sig[mid + 1:]}"
    assert tampered != token
    with pytest.raises(TokenError):
        decode_token(tampered, SECRET, FP, ttl=3600)


# -- expiry -----------------------------------------------------------------


def test_ttl_zero_never_expires() -> None:
    """`tokenTTL = 0` is the configured meaning of "no expiry", so an old
    token must still be honoured."""
    old = _good(iss=int(time.time()) - 10_000_000)
    state = decode_token(_signed(old), SECRET, FP, ttl=0)
    assert state.prefix == "ria"


def test_a_token_past_its_ttl_expires() -> None:
    old = _good(iss=int(time.time()) - 3600)
    with pytest.raises(TokenError):
        decode_token(_signed(old), SECRET, FP, ttl=60)


def test_errors_are_protocol_errors() -> None:
    """The HTTP layer catches ProtocolError; a subclass keeps the error
    rendering in one place, so this must hold."""
    with pytest.raises(ProtocolError):
        decode_token(_signed(None), SECRET, FP, ttl=0)

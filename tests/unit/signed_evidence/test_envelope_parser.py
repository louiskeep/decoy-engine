"""SignatureEnvelopeV1 parsing: every header rule has its own failing case."""

from __future__ import annotations

import base64
import json

import pytest

from decoy_engine.signed_evidence import SignedEvidenceError
from decoy_engine.signed_evidence import _envelope as env

GOOD = {
    "alg": "ES256",
    "kid": "kv-1",
    "typ": env.TYP,
    "cty": env.CTY,
    "crit": ["decoy_signer", "decoy_key_version"],
    "decoy_signer": "decoy:acme",
    "decoy_key_version": "v1",
}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _obj(header: dict | bytes, sig: bytes = bytes(64)) -> bytes:
    hb = header if isinstance(header, bytes) else json.dumps(header).encode()
    return json.dumps({"protected": _b64(hb), "signature": _b64(sig)}).encode()


def _code(raw: bytes) -> str:
    with pytest.raises(SignedEvidenceError) as ei:
        env.parse_signature_object(raw)
    assert ei.value.status == "MALFORMED"
    return ei.value.code


def test_accepts_a_well_formed_envelope_and_keeps_transmitted_segments() -> None:
    raw = _obj(GOOD)
    e = env.parse_signature_object(raw)
    assert (e.kid, e.decoy_signer, e.decoy_key_version) == ("kv-1", "decoy:acme", "v1")
    assert e.protected_segment == json.loads(raw)["protected"], (
        "verification must use transmitted bytes"
    )
    assert len(e.signature) == 64 and e.envelope_digest.startswith("sha256:")


@pytest.mark.parametrize(
    "crit",
    [
        ["decoy_signer", "decoy_signer"],  # duplicates, right length
        ["decoy_signer", "exp"],  # wrong name, right length
        ["decoy_key_version", "x-other"],
        ["decoy_signer"],
        ["decoy_signer", "decoy_key_version", "decoy_signer"],
        [],
        "decoy_signer",
        [1, 2],
        None,
        {"decoy_signer": True},
    ],
)
def test_crit_must_be_exactly_the_profile_set(crit) -> None:
    assert _code(_obj({**GOOD, "crit": crit})) == "ENVELOPE_CRIT_INVALID"


def test_crit_order_is_not_significant() -> None:
    env.parse_signature_object(_obj({**GOOD, "crit": ["decoy_key_version", "decoy_signer"]}))


@pytest.mark.parametrize(
    "name", ["jwk", "jku", "x5u", "x5c", "x5t", "x5t#S256", "b64", "zip", "epk"]
)
def test_trust_and_encoding_headers_are_forbidden(name) -> None:
    assert _code(_obj({**GOOD, name: "x"})) == "ENVELOPE_HEADER_MEMBER_FORBIDDEN"


def test_unknown_noncritical_member_is_forbidden_too() -> None:
    assert _code(_obj({**GOOD, "note": "hi"})) == "ENVELOPE_HEADER_MEMBER_FORBIDDEN"


@pytest.mark.parametrize("name", ["alg", "kid", "typ", "cty", "decoy_signer", "decoy_key_version"])
def test_every_required_member_is_required(name) -> None:
    h = {k: v for k, v in GOOD.items() if k != name}
    assert _code(_obj(h)) == "ENVELOPE_HEADER_FIELD_MISSING"


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("alg", "ES384", "ENVELOPE_ALG_NOT_ES256"),
        ("alg", "es256", "ENVELOPE_ALG_NOT_ES256"),
        ("alg", None, "ENVELOPE_ALG_NOT_ES256"),
        ("typ", "JWT", "ENVELOPE_TYP_INVALID"),
        ("typ", "application/vnd.decoy.evidence-signature.v2+jws", "ENVELOPE_TYP_INVALID"),
        ("cty", "application/json", "ENVELOPE_CTY_INVALID"),
        ("kid", "has space", "ENVELOPE_HEADER_FIELD_INVALID"),
        ("kid", 5, "ENVELOPE_HEADER_FIELD_INVALID"),
        ("kid", "", "ENVELOPE_HEADER_FIELD_INVALID"),
        ("decoy_signer", "x" * 129, "ENVELOPE_HEADER_FIELD_INVALID"),
        ("decoy_key_version", ["v1"], "ENVELOPE_HEADER_FIELD_INVALID"),
    ],
)
def test_field_values(name, value, code) -> None:
    assert _code(_obj({**GOOD, name: value})) == code


def test_duplicate_member_in_protected_header_is_rejected() -> None:
    text = json.dumps(GOOD).replace('"alg": "ES256"', '"alg": "ES256", "alg": "ES256"')
    assert _code(_obj(text.encode())) == "ENVELOPE_DUPLICATE_MEMBER"


@pytest.mark.parametrize(
    ("doc", "code"),
    [
        ({"protected": "AA", "signature": "AA", "header": {}}, "ENVELOPE_UNPROTECTED_HEADER"),
        ({"protected": "AA", "signature": "AA", "payload": "AA"}, "ENVELOPE_PAYLOAD_PRESENT"),
        ({"signatures": []}, "ENVELOPE_SHAPE_UNSUPPORTED"),
        ({"protected": "AA"}, "ENVELOPE_SHAPE_UNSUPPORTED"),
        ({"protected": 1, "signature": 2}, "ENVELOPE_SHAPE_UNSUPPORTED"),
        ({"protected": "AA", "signature": "AA", "x": 1}, "ENVELOPE_SHAPE_UNSUPPORTED"),
        ([], "ENVELOPE_SHAPE_UNSUPPORTED"),
    ],
)
def test_envelope_shapes(doc, code) -> None:
    assert _code(json.dumps(doc).encode()) == code


@pytest.mark.parametrize("seg", ["a=b", "a+b", "a/b", "a b", "a=", "ab=="])
def test_signature_segment_must_be_unpadded_base64url(seg) -> None:
    raw = json.dumps({"protected": _b64(json.dumps(GOOD).encode()), "signature": seg}).encode()
    assert _code(raw) == "ENVELOPE_BASE64URL_INVALID"


def test_noncanonical_base64url_trailing_bits_are_rejected() -> None:
    sig = _b64(bytes(64))  # 86 chars; the last char carries 4 unused bits that must be zero
    bumped = sig[:-1] + ("B" if sig[-1] == "A" else "A")
    raw = json.dumps({"protected": _b64(json.dumps(GOOD).encode()), "signature": bumped}).encode()
    assert _code(raw) == "ENVELOPE_BASE64URL_INVALID"


@pytest.mark.parametrize("n", [0, 1, 32, 63, 65, 70, 72, 96, 128])
def test_signature_must_be_exactly_64_bytes(n) -> None:
    assert _code(_obj(GOOD, bytes(n))) == "SIGNATURE_ENCODING_INVALID"


def test_oversized_protected_header_is_refused() -> None:
    big = {**GOOD, "decoy_signer": "x" * 120}
    big_text = json.dumps(big).encode() + b" " * 4000
    assert _code(_obj(big_text)) == "ENVELOPE_TOO_LARGE"

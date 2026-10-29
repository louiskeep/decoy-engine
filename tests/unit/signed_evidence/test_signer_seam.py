"""The signer seam: DER-to-P1363 conversion and identity validation."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from decoy_engine.signed_evidence import (
    LocalEs256Signer,
    SignedEvidenceError,
    SigningIdentity,
    der_to_p1363,
)

N = int("ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16)
IDENT = SigningIdentity("decoy:acme", "kv-1", "v1")


def _der(r: int, s: int) -> bytes:
    return encode_dss_signature(r, s)


def _bad(der: bytes) -> str:
    with pytest.raises(SignedEvidenceError) as ei:
        der_to_p1363(der)
    return ei.value.code


def test_valid_der_converts_to_fixed_width_big_endian() -> None:
    out = der_to_p1363(_der(1, 2))
    assert out == (1).to_bytes(32, "big") + (2).to_bytes(32, "big") and len(out) == 64
    r, s = N - 1, N - 2
    assert der_to_p1363(_der(r, s)) == r.to_bytes(32, "big") + s.to_bytes(32, "big")


def test_high_bit_values_survive_the_der_sign_byte() -> None:
    r = (1 << 255) | 5  # DER prepends 0x00 to keep it positive
    out = der_to_p1363(_der(r, 7))
    assert int.from_bytes(out[:32], "big") == r


@pytest.mark.parametrize(
    "der",
    [
        b"",
        b"\x30",
        b"\x30\x00",
        b"\x30\x06\x02\x01\x01\x02\x01",  # truncated
        _der(1, 2) + b"\x00",  # trailing garbage
        b"\x30\x08\x02\x02\x00\x01\x02\x02\x00\x02",  # non-minimal: leading zero padding
        b"\x31\x06\x02\x01\x01\x02\x01\x02",  # wrong outer tag
        b"\x30\x06\x03\x01\x01\x02\x01\x02",  # wrong inner tag
    ],
)
def test_malformed_or_non_minimal_der_is_refused(der: bytes) -> None:
    assert _bad(der) == "SIGNER_BAD_SIGNATURE"


@pytest.mark.parametrize(("r", "s"), [(0, 1), (1, 0), (N, 1), (1, N), (N + 5, 2), (1, N + 5)])
def test_out_of_range_components_are_refused(r: int, s: int) -> None:
    assert _bad(_der(r, s)) == "SIGNER_BAD_SIGNATURE"


def test_non_bytes_input_is_refused() -> None:
    with pytest.raises((SignedEvidenceError, TypeError)):
        der_to_p1363("not bytes")  # type: ignore[arg-type]


def test_local_signer_signs_64_bytes_that_verify_against_its_own_jwk() -> None:
    from decoy_engine.signed_evidence import _jws

    signer = LocalEs256Signer(ec.generate_private_key(ec.SECP256R1()), IDENT)
    msg = b"abc.def"
    sig = signer.sign_signing_input(msg)
    assert len(sig) == 64
    protected = _jws.b64u_encode(b'{"alg":"ES256"}')
    # the JOSE layer builds the signing input itself; confirm the raw primitive agrees with it
    inp = (protected + "." + _jws.b64u_encode(b"payload")).encode()
    sig2 = signer.sign_signing_input(inp)
    assert _jws.verify_es256_signature(
        protected_segment=protected,
        payload_segment=_jws.b64u_encode(b"payload"),
        signature_segment=_jws.b64u_encode(sig2),
        public_jwk=dict(signer.public_jwk()),
    )


def test_public_jwk_has_exactly_the_four_public_members() -> None:
    signer = LocalEs256Signer(ec.generate_private_key(ec.SECP256R1()), IDENT)
    assert set(signer.public_jwk()) == {"kty", "crv", "x", "y"}
    assert signer.public_jwk()["crv"] == "P-256"


@pytest.mark.parametrize("curve", [ec.SECP384R1(), ec.SECP521R1(), ec.SECP256K1()])
def test_only_p256_keys_are_accepted(curve) -> None:
    with pytest.raises(SignedEvidenceError) as ei:
        LocalEs256Signer(ec.generate_private_key(curve), IDENT)
    assert ei.value.code == "SIGNER_KEY_INVALID"


def test_non_ec_key_is_refused() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    with pytest.raises(SignedEvidenceError):
        LocalEs256Signer(rsa.generate_private_key(65537, 2048), IDENT)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["signer", "kid", "key_version"])
@pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "-leading", "a/b", None, 5])
def test_identity_fields_are_validated(field, bad) -> None:
    kw = {"signer": "decoy:acme", "kid": "kv-1", "key_version": "v1"}
    kw[field] = bad  # type: ignore[assignment]
    with pytest.raises(SignedEvidenceError) as ei:
        SigningIdentity(**kw)
    assert ei.value.code == "SIGNER_IDENTITY_INVALID"


def test_identity_is_immutable() -> None:
    with pytest.raises(AttributeError):
        IDENT.kid = "other"  # type: ignore[misc]


def test_signatures_are_valid_ecdsa_over_the_exact_input() -> None:
    """Cross-check with the library directly: the P1363 output is a standard ECDSA signature."""
    key = ec.generate_private_key(ec.SECP256R1())
    signer = LocalEs256Signer(key, IDENT)
    msg = b"exact signing input"
    sig = signer.sign_signing_input(msg)
    key.public_key().verify(
        encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
        msg,
        ec.ECDSA(hashes.SHA256()),
    )

"""F13 §5 acceptance 4: per-algorithm validation, never a universal 32-byte rule."""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet

from decoy_engine.keyprovider import MaskSecretError
from decoy_engine.secret_providers import (
    AES_GCM_V1,
    FERNET_V1,
    HMAC_SHA256_V1,
    MASK_ROOT_V1,
    REQUIREMENTS,
    SecretMaterialInvalid,
    parse_ref,
    prepare_secret,
)
from decoy_engine.secret_providers._local import EnvSecretProvider


def admit(monkeypatch, requirement: str, value: str, ref_suffix: str):
    monkeypatch.setenv("F13_MATERIAL", value)
    ref = parse_ref(f"env:F13_MATERIAL{ref_suffix}")
    return prepare_secret(
        provider=EnvSecretProvider(), ref=ref, requirements=(REQUIREMENTS[requirement],)
    )


def raw(n: int) -> str:
    return "k" * n


def test_fernet_accepts_a_real_key_and_rejects_the_rest(monkeypatch):
    key = Fernet.generate_key().decode()
    assert admit(monkeypatch, FERNET_V1, key, "?encoding=raw")
    for bad in (raw(32), key[:-4], base64.b64encode(b"x" * 31).decode(), ""):
        with pytest.raises(SecretMaterialInvalid):
            admit(monkeypatch, FERNET_V1, bad, "?encoding=raw")


@pytest.mark.parametrize("n, ok", [(31, False), (32, True), (33, True), (64, True)])
def test_hmac_sha256_needs_at_least_32_bytes(monkeypatch, n, ok):
    if ok:
        assert admit(monkeypatch, HMAC_SHA256_V1, raw(n), "?encoding=raw")
    else:
        with pytest.raises(SecretMaterialInvalid):
            admit(monkeypatch, HMAC_SHA256_V1, raw(n), "?encoding=raw")


@pytest.mark.parametrize(
    "n, ok",
    [(15, False), (16, True), (20, False), (24, True), (31, False), (32, True), (33, False)],
)
def test_aes_gcm_accepts_exactly_16_24_32(monkeypatch, n, ok):
    if ok:
        assert admit(monkeypatch, AES_GCM_V1, raw(n), "?encoding=raw")
    else:
        with pytest.raises(SecretMaterialInvalid):
            admit(monkeypatch, AES_GCM_V1, raw(n), "?encoding=raw")


@pytest.mark.parametrize("n, ok", [(31, False), (32, True), (40, True)])
def test_mask_root_keeps_its_32_byte_minimum_and_legacy_decoding(monkeypatch, n, ok):
    value = (b"m" * n).hex()
    if ok:
        prepared = admit(monkeypatch, MASK_ROOT_V1, value, "")
        assert len(prepared.mask_key()) == 32
    else:
        with pytest.raises(MaskSecretError) as info:
            admit(monkeypatch, MASK_ROOT_V1, value, "")
        assert info.value.code == "weak_mask_secret"


def test_one_valid_key_is_not_judged_by_another_algorithms_rule(monkeypatch):
    # 16 bytes is a valid AES-128 key and an invalid HMAC-SHA256 / mask-root key.
    assert admit(monkeypatch, AES_GCM_V1, raw(16), "?encoding=raw")
    with pytest.raises(SecretMaterialInvalid):
        admit(monkeypatch, HMAC_SHA256_V1, raw(16), "?encoding=raw")
    # 24 bytes: valid AES-192, invalid for the 32-byte algorithms.
    assert admit(monkeypatch, AES_GCM_V1, raw(24), "?encoding=raw")
    with pytest.raises(SecretMaterialInvalid):
        admit(monkeypatch, HMAC_SHA256_V1, raw(24), "?encoding=raw")


@pytest.mark.parametrize("req", [FERNET_V1, HMAC_SHA256_V1, AES_GCM_V1])
def test_typed_requirements_demand_an_explicit_encoding(monkeypatch, req):
    with pytest.raises(MaskSecretError) as info:
        admit(monkeypatch, req, raw(32), "")
    assert info.value.code == "bad_secret_ref"


def test_legacy_hex_then_base64_applies_only_to_the_mask_root(monkeypatch):
    key = Fernet.generate_key().decode()
    # A valid Fernet key is also valid base64; legacy decoding would corrupt it.
    with pytest.raises(MaskSecretError):
        admit(monkeypatch, FERNET_V1, key, "")


@pytest.mark.parametrize("encoding, value", [("hex", "zz" * 32), ("base64", "!!notb64!!")])
def test_malformed_explicit_encodings_are_rejected(monkeypatch, encoding, value):
    with pytest.raises(SecretMaterialInvalid):
        admit(monkeypatch, HMAC_SHA256_V1, value, f"?encoding={encoding}")
    with pytest.raises(MaskSecretError) as info:
        admit(monkeypatch, MASK_ROOT_V1, value, f"?encoding={encoding}")
    assert info.value.code == "bad_secret_ref"


def test_explicit_hex_and_base64_decode_for_typed_requirements(monkeypatch):
    material = bytes(range(32))
    assert admit(monkeypatch, HMAC_SHA256_V1, material.hex(), "?encoding=hex")
    assert admit(
        monkeypatch, HMAC_SHA256_V1, base64.b64encode(material).decode(), "?encoding=base64"
    )

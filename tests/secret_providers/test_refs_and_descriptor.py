"""Reference grammar, value-free descriptors, cache keys (F13 §4.2, §5 acceptance 6)."""

from __future__ import annotations

import json

import pytest

from decoy_engine.keyprovider import MaskSecretError
from decoy_engine.secret_providers import SecretDescriptor, SecretRef, parse_ref
from decoy_engine.secret_providers._local import EnvSecretProvider, FileSecretProvider

GOOD = "vault://kv/team/app?version=3&field=key&encoding=base64"


def test_vault_ref_round_trips_its_parts():
    ref = parse_ref(GOOD)
    assert (ref.scheme, ref.locator, ref.version, ref.field, ref.encoding) == (
        "vault",
        "kv/team/app",
        "3",
        "key",
        "base64",
    )


@pytest.mark.parametrize(
    "bad",
    [
        "vault://user:pw@kv/a?version=1&field=f&encoding=raw",
        "vault://kv/a?version=1&field=f&encoding=raw#frag",
        "vault://kv/a?field=f&encoding=raw",
        "vault://kv/a?version=1&encoding=raw",
        "vault://kv/a?version=1&field=f",
        "vault://kv/a?version=latest&field=f&encoding=raw",
        "vault://kv/a?version=0&field=f&encoding=raw",
        "vault://kv/a?version=-1&field=f&encoding=raw",
        "vault://kv/a?version=1&version=2&field=f&encoding=raw",
        "vault://kv/../a?version=1&field=f&encoding=raw",
        "vault://kv/a/./b?version=1&field=f&encoding=raw",
        "vault://kv/a//b?version=1&field=f&encoding=raw",
        "vault://kv/%ff?version=1&field=f&encoding=raw",
        "vault://kv/a?version=1&field=f&encoding=utf16",
        "vault://kv/a?version=1&field=f&encoding=raw&extra=1",
        "vault://kv:8200/a?version=1&field=f&encoding=raw",
        "vault:///a?version=1&field=f&encoding=raw",
        "vault://kv?version=1&field=f&encoding=raw",
    ],
)
def test_vault_ref_grammar_rejects_unsafe_refs(bad):
    with pytest.raises(MaskSecretError) as info:
        parse_ref(bad)
    assert info.value.code == "bad_secret_ref"


def test_ref_errors_and_reprs_never_repeat_the_reference():
    secret_path = "vault://kv/F13PATHMARKER/x?version=0&field=F13FIELDMARKER&encoding=raw"
    with pytest.raises(MaskSecretError) as info:
        parse_ref(secret_path)
    assert "F13PATHMARKER" not in str(info.value) + repr(info.value)
    ref = parse_ref(GOOD)
    assert "kv/team" not in repr(ref) + str(ref)


def test_local_refs_keep_legacy_syntax_and_add_options():
    assert parse_ref("env:NAME").locator == "NAME"
    assert parse_ref("file:/etc/key").locator == "/etc/key"
    ref = parse_ref("env:NAME?encoding=raw&key_version=v2")
    assert (ref.locator, ref.encoding, ref.key_version) == ("NAME", "raw", "v2")
    with pytest.raises(MaskSecretError):
        parse_ref("env:")
    with pytest.raises(MaskSecretError):
        parse_ref("env:NAME?encoding=raw&encoding=hex")
    with pytest.raises(MaskSecretError):
        parse_ref("not a ref at all")


def test_unknown_schemes_parse_for_the_registry_to_reject():
    ref = parse_ref("kms:alias/x?encoding=raw")
    assert (ref.scheme, ref.locator, ref.encoding) == ("kms", "alias/x", "raw")


def test_cache_key_is_a_salted_hash_not_the_ref():
    ref = parse_ref(GOOD)
    key = ref.get_cache_key_component(("decoy-mask-root-v1",))
    assert "kv/team" not in key and len(key) == 64
    assert key == ref.get_cache_key_component(("decoy-mask-root-v1",))
    assert key != ref.get_cache_key_component(("fernet-v1",))
    assert key != parse_ref(GOOD.replace("version=3", "version=4")).get_cache_key_component(
        ("decoy-mask-root-v1",)
    )


@pytest.mark.parametrize("provider", [EnvSecretProvider(), FileSecretProvider()])
def test_describe_is_value_free_and_never_reads(provider, monkeypatch):
    monkeypatch.delenv("F13_NOT_THERE", raising=False)
    ref = parse_ref(f"{provider.scheme}:/nonexistent/F13_NOT_THERE")
    d = provider.describe(ref)
    assert isinstance(d, SecretDescriptor)
    assert set(d.to_dict()) == {
        "api_version",
        "scheme",
        "key_version",
        "created_at",
        "expires_at",
        "renewable",
        "cache_status",
    }
    blob = json.dumps(d.to_dict())
    assert "F13_NOT_THERE" not in blob
    assert d.key_version == "v1"


def test_secret_ref_is_frozen():
    ref = parse_ref(GOOD)
    assert isinstance(ref, SecretRef)
    with pytest.raises(Exception):
        ref.version = "9"  # type: ignore[misc]

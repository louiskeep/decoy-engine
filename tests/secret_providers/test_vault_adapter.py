"""Vault KV v2 adapter: protocol-level unit tests (isolated SDK stub with the REAL hvac exception
classes) plus the TLS-environment contract. Live-Vault behavior is in test_vault_live.py; these
stubs satisfy neither the adapter-ordering nor the leak acceptance on their own.
"""

from __future__ import annotations

import base64
import traceback

import pytest

hvac = pytest.importorskip("hvac")

from decoy_engine.keyprovider import MaskSecretError  # noqa: E402
from decoy_engine.secret_providers import (  # noqa: E402
    MASK_ROOT_V1,
    REQUIREMENTS,
    SecretCachePolicy,
    parse_ref,
)
from decoy_engine.secret_providers.vault import (  # noqa: E402
    VaultClientConfigError,
    VaultKV2Provider,
    client_from_env,
)
from tests.secret_providers._helpers import FakeClock  # noqa: E402

MARKER = "F13-VAULT-MARKER-0123456789abcdef0123"
KEY_B64 = base64.b64encode(b"v" * 40).decode()
REF = parse_ref("vault://kv/team/app?version=3&field=key&encoding=base64")
REQ = (REQUIREMENTS[MASK_ROOT_V1],)


def payload(*, version=3, value=KEY_B64, destroyed=False, deletion_time="", **top):
    body = {
        "request_id": "r",
        "lease_id": "",
        "renewable": False,
        "lease_duration": 0,
        "data": {
            "data": {"key": value},
            "metadata": {
                "created_time": "2026-01-01T00:00:00.123456Z",
                "deletion_time": deletion_time,
                "destroyed": destroyed,
                "version": version,
            },
        },
        "wrap_info": None,
        "warnings": None,
        "auth": None,
    }
    body.update(top)
    return body


class StubClient:
    """Counts round trips; behaves like hvac.Client for the one call the adapter makes."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result, self._raises = result, raises
        outer = self

        class V2:
            def read_secret_version(self, **kwargs):
                outer.calls.append(kwargs)
                if outer._raises is not None:
                    raise outer._raises
                return outer._result

        class KV:
            v2 = V2()

        class Secrets:
            kv = KV()

        self.secrets = Secrets()


def admit(client, clock=None):
    provider = VaultKV2Provider(client, clock=clock)
    return provider.preflight(REF, REQ, SecretCachePolicy())


def code_of(client, clock=None):
    with pytest.raises(MaskSecretError) as info:
        admit(client, clock)
    assert info.value.__cause__ is None and info.value.__context__ is None
    return info.value.code


def test_success_is_one_round_trip_for_the_exact_pinned_version():
    client = StubClient(payload())
    prepared = admit(client)
    assert len(client.calls) == 1
    assert client.calls[0] == {"path": "team/app", "version": 3, "mount_point": "kv"}
    assert prepared.key_version == "v3" and len(prepared.mask_key()) == 32


def test_describe_makes_no_provider_call():
    client = StubClient(payload())
    d = VaultKV2Provider(client).describe(REF)
    assert client.calls == [] and d.key_version == "v3" and d.renewable is False


@pytest.mark.parametrize(
    "exc, code",
    [
        (hvac.exceptions.Forbidden(MARKER), "secret_access_denied"),
        (hvac.exceptions.Unauthorized(MARKER), "secret_access_denied"),
        (hvac.exceptions.InvalidPath(MARKER), "secret_version_unavailable"),
        (hvac.exceptions.VaultDown(MARKER), "secret_provider_unavailable"),
        (hvac.exceptions.InternalServerError(MARKER), "secret_provider_unavailable"),
        (ConnectionError(MARKER), "secret_provider_unavailable"),
        (RuntimeError(MARKER), "secret_provider_unavailable"),
    ],
)
def test_sdk_failures_map_by_type_and_never_carry_vendor_text(exc, code):
    client = StubClient(raises=exc)
    with pytest.raises(MaskSecretError) as info:
        admit(client)
    err = info.value
    assert err.code == code and err.__cause__ is None and err.__context__ is None
    assert MARKER not in "".join(traceback.format_exception(err)) + str(err) + repr(err)


@pytest.mark.parametrize(
    "response, code",
    [
        (payload(destroyed=True), "secret_version_unavailable"),
        (payload(version=4), "secret_version_unavailable"),
        (payload(deletion_time="2020-01-01T00:00:00Z"), "secret_expired"),
        (payload(lease_id="database/creds/x/abc"), "secret_lease_unsupported"),
        (payload(renewable=True), "secret_lease_unsupported"),
        (payload(lease_duration=300), "secret_lease_unsupported"),
        (payload(wrap_info={"token": "t"}), "secret_lease_unsupported"),
        (payload(value=""), "secret_material_invalid"),
        (payload(value=123), "secret_material_invalid"),
        ({"data": {}}, "secret_provider_unavailable"),
        ({}, "secret_provider_unavailable"),
    ],
)
def test_metadata_and_shape_failures_are_registered_reasons(response, code):
    assert code_of(StubClient(response)) == code


def test_future_deletion_time_is_the_expiry_and_bounds_the_cache_ttl():
    clock = FakeClock(wall=1_700_000_000.0)
    future = "2023-11-14T22:14:00Z"  # 1_700_000_040 -> 40s after the fake wall clock
    client = StubClient(payload(deletion_time=future))
    policy = SecretCachePolicy(ttl_seconds=60, clock=clock)
    provider = VaultKV2Provider(client, clock=clock)
    provider.preflight(REF, REQ, policy).close()
    clock.advance(34)  # 40s - 5s skew = 35s effective TTL
    provider.preflight(REF, REQ, policy).close()
    assert len(client.calls) == 1
    clock.advance(1.5)
    provider.preflight(REF, REQ, policy).close()
    assert len(client.calls) == 2


def test_material_that_fails_the_algorithm_contract_is_rejected():
    short = base64.b64encode(b"s" * 8).decode()
    assert code_of(StubClient(payload(value=short))) == "weak_mask_secret"


@pytest.mark.parametrize("attribute", ["namespace", "token", "url"])
def test_the_client_security_context_scopes_the_cache(attribute):
    from types import SimpleNamespace

    from decoy_engine.secret_providers import default_cache

    default_cache().clear()
    client = StubClient(payload())
    client.url, client.token = "https://vault.test:8200", "tok-a"
    client.adapter = SimpleNamespace(namespace="tenant-a")
    provider = VaultKV2Provider(client)
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    provider.preflight(REF, REQ, policy).close()
    provider.preflight(REF, REQ, policy).close()
    assert len(client.calls) == 1  # positive control: same context, one read
    target = client.adapter if attribute == "namespace" else client
    setattr(target, attribute, getattr(target, attribute) + "-other")
    provider.preflight(REF, REQ, policy).close()
    assert len(client.calls) == 2
    default_cache().clear()


# --- client construction from the standard environment -------------------------------------


@pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes", "on"])
def test_vault_skip_verify_is_hard_rejected_not_warned(value):
    with pytest.raises(VaultClientConfigError) as info:
        client_from_env({"VAULT_ADDR": "https://v.example:8200", "VAULT_SKIP_VERIFY": value})
    assert "VAULT_SKIP_VERIFY" in str(info.value)


def test_tls_material_is_honored_and_verification_stays_on():
    client = client_from_env(
        {
            "VAULT_ADDR": "https://v.example:8200",
            "VAULT_TOKEN": "t",
            "VAULT_CACERT": "/etc/ca.pem",
            "VAULT_CLIENT_CERT": "/etc/c.pem",
            "VAULT_CLIENT_KEY": "/etc/c.key",
            "VAULT_NAMESPACE": "team-a",
        }
    )
    session = client.adapter.session
    assert session.verify == "/etc/ca.pem"
    assert session.cert == ("/etc/c.pem", "/etc/c.key")
    default = client_from_env({"VAULT_ADDR": "https://v.example:8200"})
    assert default.adapter.session.verify in (True, None) or default.adapter.session.verify is True


def test_insecure_override_exists_only_in_code():
    client = client_from_env(
        {"VAULT_ADDR": "https://v.example:8200"}, insecure_skip_tls_verify=True
    )
    assert client.adapter.session.verify is False


def test_half_a_client_cert_pair_and_missing_addr_are_rejected():
    with pytest.raises(VaultClientConfigError):
        client_from_env({"VAULT_ADDR": "https://v", "VAULT_CLIENT_CERT": "/c.pem"})
    with pytest.raises(VaultClientConfigError):
        client_from_env({})


def test_base_install_without_hvac_still_imports_and_fails_closed(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "hvac", None)  # simulates a missing optional extra
    from decoy_engine.secret_providers import vault as vault_mod

    assert vault_mod.VaultKV2Provider  # module import never needs hvac
    with pytest.raises(MaskSecretError) as info:
        client_from_env({"VAULT_ADDR": "https://v.example:8200"})
    assert info.value.code == "secret_provider_unavailable"

"""F13 r4: tests that kill surviving mutants of the r4 mutation run (see the r4 mutation report).

Each test pins a property a surviving mutant broke: the Vault context carries every routing and
TLS setting into the read, the read's transport is closed, value-bearing objects keep their fixed
renderings and refuse pickling, the context validator rejects lookalikes, an uncached load never
waits on a cached one, and the ref bridge hands a real policy to the provider.
"""

from __future__ import annotations

import pickle
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

hvac = pytest.importorskip("hvac")

from decoy_engine.keyprovider import MaskSecretError, key_provider_from_ref  # noqa: E402
from decoy_engine.secret_providers import (  # noqa: E402
    MASK_ROOT_V1,
    REQUIREMENTS,
    PreparedSecret,
    ResolveContext,
    ResolvedSecret,
    SecretBuffer,
    SecretCachePolicy,
    SecretProviderUnavailable,
    default_cache,
    parse_ref,
    prepare_secret,
)
from decoy_engine.secret_providers import vault as vault_mod  # noqa: E402
from decoy_engine.secret_providers.vault import (  # noqa: E402
    VaultClientConfigError,
    VaultKV2Provider,
    client_from_env,
)
from tests.secret_providers._helpers import FakeClock, ProtocolProvider  # noqa: E402
from tests.secret_providers.test_vault_adapter import payload  # noqa: E402

REQ = (REQUIREMENTS[MASK_ROOT_V1],)
VREF = parse_ref("vault://kv/team/app?version=3&field=key&encoding=base64")
REF = parse_ref("test:thing?encoding=raw")
FULL_ENV = {
    "VAULT_ADDR": "https://v.example:8200",
    "VAULT_TOKEN": "tok-env",
    "VAULT_NAMESPACE": "team-a",
    "VAULT_CACERT": "/etc/ca.pem",
    "VAULT_CLIENT_CERT": "/etc/c.pem",
    "VAULT_CLIENT_KEY": "/etc/c.key",
}


@pytest.fixture(autouse=True)
def _clean_cache():
    default_cache().clear()
    yield
    default_cache().clear()


class _Recorder:
    """Stands in for ``hvac.Client`` at read time: records how the read was routed."""

    def __init__(self, response: Any = None, raises: BaseException | None = None) -> None:
        self.built: list[dict] = []
        self.closed = 0
        self.response = response if response is not None else payload()
        self.raises = raises

    def __call__(self, **kwargs: Any) -> Any:
        self.built.append(kwargs)
        outer = self

        class V2:
            def read_secret_version(self, **_: Any) -> dict:
                if outer.raises is not None:
                    raise outer.raises
                return outer.response

        def close() -> None:
            outer.closed += 1

        return SimpleNamespace(
            secrets=SimpleNamespace(kv=SimpleNamespace(v2=V2())),
            adapter=SimpleNamespace(close=close),
        )


def _read_with(client: Any, monkeypatch, recorder: _Recorder | None = None) -> _Recorder:
    provider = VaultKV2Provider(client)  # built while hvac.Client is still the real class
    recorder = recorder or _Recorder()
    monkeypatch.setattr(hvac, "Client", recorder)
    provider.preflight(VREF, REQ, SecretCachePolicy()).close()
    return recorder


# --- the context carries every routing and TLS setting into the read -----------------------


def test_every_routing_and_tls_setting_reaches_the_read(monkeypatch):
    recorder = _read_with(client_from_env(FULL_ENV), monkeypatch)
    assert recorder.built == [
        {
            "url": "https://v.example:8200",
            "token": "tok-env",
            "namespace": "team-a",
            "verify": "/etc/ca.pem",
            "cert": ("/etc/c.pem", "/etc/c.key"),
        }
    ]
    assert recorder.closed == 1


def test_defaults_and_the_insecure_override_reach_the_read(monkeypatch):
    default = _read_with(client_from_env({"VAULT_ADDR": "https://v.example:8200"}), monkeypatch)
    assert default.built[0]["verify"] is True and default.built[0]["cert"] is None
    assert default.built[0]["namespace"] is None and default.built[0]["token"] is None
    monkeypatch.undo()
    insecure = client_from_env(
        {"VAULT_ADDR": "https://v.example:8200"}, insecure_skip_tls_verify=True
    )
    assert _read_with(insecure, monkeypatch).built[0]["verify"] is False


def _bare(**tls: Any) -> Any:
    client = SimpleNamespace(url="https://bare.test", token="t")
    client.adapter = SimpleNamespace(namespace=None, _kwargs=tls)
    return client


@pytest.mark.parametrize(
    "tls, verify, cert",
    [
        ({}, True, None),
        ({"verify": 123}, True, None),  # not bool/str: verification stays on
        ({"verify": ""}, True, None),  # an empty CA path is not "off"
        ({"verify": False}, False, None),
        ({"cert": "/etc/both.pem"}, True, "/etc/both.pem"),  # one combined cert+key file
        ({"cert": ["/c.pem", "/c.key"]}, True, ("/c.pem", "/c.key")),
    ],
)
def test_tls_settings_are_normalised_without_dropping_any(tls, verify, cert, monkeypatch):
    built = _read_with(_bare(**tls), monkeypatch).built[0]
    assert built["verify"] == verify and type(built["verify"]) is type(verify)
    assert built["cert"] == cert


def test_a_client_without_settings_routes_with_none(monkeypatch):
    built = _read_with(SimpleNamespace(), monkeypatch).built[0]
    assert built == {"url": None, "token": None, "namespace": None, "verify": True, "cert": None}


def test_an_unusable_client_cert_fails_closed_instead_of_being_dropped(monkeypatch):
    provider = VaultKV2Provider(_bare(cert=("/a", "/b", "/c")))
    recorder = _Recorder()
    monkeypatch.setattr(hvac, "Client", recorder)
    with pytest.raises(SecretProviderUnavailable):
        provider.preflight(VREF, REQ, SecretCachePolicy())
    assert recorder.built == []


def test_the_read_transport_is_closed_after_a_failure(monkeypatch):
    recorder = _Recorder(raises=hvac.exceptions.Forbidden("x"))
    with pytest.raises(MaskSecretError):
        _read_with(_bare(), monkeypatch, recorder)
    assert recorder.closed == 1


def test_a_settings_change_is_a_new_cache_scope(monkeypatch):
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    recorder = _Recorder()
    providers = [VaultKV2Provider(_bare(verify=v)) for v in (True, "/etc/other-ca.pem")]
    monkeypatch.setattr(hvac, "Client", recorder)
    for provider in providers:
        provider.preflight(VREF, REQ, policy).close()
    assert [b["verify"] for b in recorder.built] == [True, "/etc/other-ca.pem"]


# --- fixed renderings and no pickling for every object that holds a secret or a token -------


def test_value_bearing_objects_keep_fixed_renderings_and_refuse_pickling():
    provider = VaultKV2Provider(_bare())
    context = provider.resolve_context()
    assert repr(provider) == str(provider) == "VaultKV2Provider(<redacted>)"
    assert repr(context) == str(context) == "ResolveContext(scheme='vault')"
    with pytest.raises(TypeError, match="^secret providers cannot be pickled$"):
        pickle.dumps(provider)
    with pytest.raises(TypeError, match="^resolve contexts cannot be pickled$"):
        pickle.dumps(context)
    with pytest.raises(
        AttributeError, match="^VaultKV2Provider is immutable; build a new provider$"
    ):
        del provider._context

    prepared = PreparedSecret(
        SecretBuffer(b"k" * 32), key_version="v1", requirement_ids=("a",), expires_at=9.0
    )
    resolved = ResolvedSecret(SecretBuffer(b"k" * 32), key_version="v1")
    assert repr(prepared) == str(prepared) == "PreparedSecret(<redacted>)"
    assert repr(resolved) == str(resolved) == "ResolvedSecret(<redacted>)"
    for obj in (prepared, resolved, prepared.key_provider()):
        with pytest.raises(TypeError, match="^secrets cannot be pickled$"):
            pickle.dumps(obj)
    clone = prepared.clone()
    assert (clone.requirement_ids, clone.expires_at, clone.key_version) == (("a",), 9.0, "v1")


# --- the context validator rejects lookalikes and accepts tuple fields -------------------------


@dataclass(frozen=True, repr=False)
class _Lookalike:
    """Not a ResolveContext, but borrows its rendering."""

    scheme: str = "test"
    __repr__ = ResolveContext.__repr__
    __str__ = ResolveContext.__str__


class _SchemeText(str):
    pass


@dataclass(frozen=True, repr=False)
class _Parts(ResolveContext):
    parts: tuple = ()


@pytest.mark.parametrize(
    "make, accepted",
    [
        (lambda: _Lookalike(), False),
        (lambda: ResolveContext(scheme=_SchemeText("test")), False),
        (lambda: _Parts(scheme="test", parts=(["x"],)), False),
        (lambda: _Parts(scheme="test", parts=("a", 1, None, True)), True),
    ],
)
def test_the_context_validator(make, accepted):
    class Provider(ProtocolProvider):
        def resolve_context(self):
            return make()

    prov = Provider()
    if accepted:
        prepare_secret(provider=prov, ref=REF, requirements=REQ).close()
        assert prov.resolve_calls == 1
    else:
        with pytest.raises(SecretProviderUnavailable):
            prepare_secret(provider=prov, ref=REF, requirements=REQ)
        assert prov.resolve_calls == 0


def test_a_foreign_str_subclass_code_is_not_a_registered_code():
    class Code(str):
        pass

    class Provider(ProtocolProvider):
        def resolve(self, ref, context=None):  # type: ignore[override]
            raise MaskSecretError(code=Code("secret_access_denied"), message="x")

    with pytest.raises(MaskSecretError) as info:
        prepare_secret(provider=Provider(), ref=REF, requirements=REQ)
    assert info.value.code == "secret_provider_unavailable"


# --- an uncached load never takes the cache's single-flight lock -----------------------------


def test_an_uncached_load_never_waits_on_a_cached_load_of_the_same_key():
    entered, release = threading.Event(), threading.Event()

    class Slow(ProtocolProvider):
        def resolve(self, ref, context=None):  # type: ignore[override]
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            return super().resolve(ref, context)

    prov = Slow()
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    cached = threading.Thread(
        target=lambda: prepare_secret(
            provider=prov, ref=REF, requirements=REQ, cache_policy=policy
        ).close()
    )
    cached.start()
    assert entered.wait(5)
    done = threading.Event()
    uncached = threading.Thread(
        target=lambda: (
            prepare_secret(provider=prov, ref=REF, requirements=REQ).close(),
            done.set(),
        )
    )
    uncached.start()
    finished_first = done.wait(2)
    release.set()
    cached.join(5)
    uncached.join(5)
    assert finished_first, "a TTL-0 load waited on the cache's single-flight lock"


# --- the ref bridge hands the provider a real policy -----------------------------------------


def test_key_provider_from_ref_hands_a_default_policy_to_the_provider(registry):
    seen: list[object] = []

    class Recording(ProtocolProvider):
        def preflight(self, ref, requirements, cache_policy):
            seen.append(cache_policy)
            return super().preflight(ref, requirements, cache_policy)

    registry.register(Recording("rec"))
    key_provider_from_ref("rec:thing?encoding=raw")
    assert len(seen) == 1 and type(seen[0]) is SecretCachePolicy
    assert seen[0].ttl_seconds == 0


# --- Vault adapter details the earlier suite did not pin -------------------------------------


def test_describe_names_the_vault_scheme():
    assert VaultKV2Provider(_bare()).describe(VREF).scheme == "vault"


def _resolve(response: Any, monkeypatch, clock: Any = None) -> ResolvedSecret:
    provider = VaultKV2Provider(_bare(), clock=clock or FakeClock(wall=1_700_000_000.0))
    monkeypatch.setattr(hvac, "Client", _Recorder(response=response))
    return provider.resolve(VREF, provider.resolve_context())


def test_auth_data_in_a_response_is_a_lease(monkeypatch):
    with pytest.raises(MaskSecretError) as info:
        _read_with(_bare(), monkeypatch, _Recorder(response=payload(auth={"x": 1})))
    assert info.value.code == "secret_lease_unsupported"


def test_created_and_deletion_times_are_parsed_exactly(monkeypatch):
    resolved = _resolve(payload(deletion_time="2023-11-14T22:14:00.25Z"), monkeypatch)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() + 0.123456
    assert resolved.created_at == pytest.approx(created, abs=1e-6)
    assert resolved.expires_at == pytest.approx(1_700_000_040.25, abs=1e-6)
    resolved.close()


def test_a_deletion_time_of_exactly_now_is_expired(monkeypatch):
    with pytest.raises(MaskSecretError) as info:
        _resolve(payload(deletion_time="2023-11-14T22:13:20Z"), monkeypatch)
    assert info.value.code == "secret_expired"


# --- client_from_env: exact settings and fixed error texts -----------------------------------


def test_client_from_env_builds_exactly_the_configured_client():
    client = client_from_env(FULL_ENV)
    assert client.url == "https://v.example:8200"
    assert client.token == "tok-env"
    assert client.adapter.namespace == "team-a"
    bare = client_from_env({"VAULT_ADDR": "https://v.example:8200", "VAULT_NAMESPACE": ""})
    assert bare.adapter.namespace is None and bare.adapter.session.cert is None


@pytest.mark.parametrize(
    "env, text",
    [
        (
            {"VAULT_ADDR": "https://v", "VAULT_SKIP_VERIFY": "1"},
            "VAULT_SKIP_VERIFY is not honored: disabling TLS verification requires an explicit "
            "in-code override (insecure_skip_tls_verify=True).",
        ),
        ({}, "VAULT_ADDR is required to build a Vault client."),
        (
            {"VAULT_ADDR": "https://v", "VAULT_CLIENT_KEY": "/k"},
            "VAULT_CLIENT_CERT and VAULT_CLIENT_KEY must be set together.",
        ),
    ],
)
def test_client_from_env_errors_carry_their_fixed_text(env, text):
    with pytest.raises(VaultClientConfigError) as info:
        client_from_env(env)
    assert str(info.value) == text


def test_an_unset_setting_renders_as_empty():
    assert vault_mod._text(None) == "" and vault_mod._text(3) == "3"


# --- the reference grammar: exact accept/reject table, and parse_ref is a closed boundary ------

REJECTED_REFS = [
    7,
    "",
    "env:A\x00B",
    "env:N?encoding=",
    "env:N?encoding=raw&SECRETPATH",  # a malformed query once escaped as ValueError with its text
    "env:N?encoding=raw&&",
    "vault://kv/a?version=1&field=f",  # encoding missing
    "vault://kv/a?version=1&encoding=raw",  # field missing
    "vault://kv/a?version=1&field=f&encoding=raw&",  # strict query parsing
    "vault://kv/a?version=1&field=&encoding=raw",
    "vault://kv/a?version=1&field=f&encoding=raw&x=",  # an unknown key, even blank
    "vault://u@kv/a?version=1&field=f&encoding=raw",
    "vault://kv/%ff?version=1&field=f&encoding=raw",  # undecodable path
    "vault://%ff/a?version=1&field=f&encoding=raw",  # undecodable mount
    "vault://kv/a?version=1&field=%ff&encoding=raw",  # undecodable field
    "vault://kv/a\\b?version=1&field=f&encoding=raw",
    "vault://kv/a%01b?version=1&field=f&encoding=raw",
]


@pytest.mark.parametrize("text", REJECTED_REFS, ids=range(len(REJECTED_REFS)))
def test_malformed_refs_are_bad_secret_ref_and_never_echo_the_ref(text):
    with pytest.raises(MaskSecretError) as info:
        parse_ref(text)
    err = info.value
    assert type(err) is MaskSecretError and err.code == "bad_secret_ref"
    assert err.message == "The secret reference is invalid."
    assert err.__cause__ is None and err.__context__ is None
    assert "SECRETPATH" not in str(err) + repr(err)


@pytest.mark.parametrize(
    "text, locator, encoding, field",
    [
        ("env:encoding=raw", "encoding=raw", None, None),  # no "?": the whole rest is the name
        ("file:/p?a?encoding=raw", "/p?a", "raw", None),  # only the LAST "?" starts options
        ("env:N?", "N?", None, None),
        ("env:a?b", "a?b", None, None),  # a "?" without options is part of the name
        ("env:N?%ff=raw", "N?%ff=raw", None, None),  # not options: part of the name
        ("vault://kv/a%20b?version=1&field=f%20g&encoding=raw", "kv/a b", "raw", "f g"),
    ],
)
def test_well_formed_refs_parse_exactly(text, locator, encoding, field):
    ref = parse_ref(text)
    assert (ref.locator, ref.encoding, ref.field) == (locator, encoding, field)


def test_vault_times_are_utc_whatever_the_local_zone(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        resolved = _resolve(payload(deletion_time="2023-11-14T22:14:00Z"), monkeypatch)
        assert resolved.expires_at == 1_700_000_040.0
        resolved.close()
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_a_missing_created_time_is_not_a_failure(monkeypatch):
    response = payload()
    response["data"]["metadata"].pop("created_time")
    resolved = _resolve(response, monkeypatch)
    assert resolved.created_at is None
    resolved.close()

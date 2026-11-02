"""F13 r4: the read and the cache key share one immutable resolve context; one translation
boundary handles every ``BaseException`` an extension can raise.

Class 3 (state and concurrency): a barrier-forced A to B to A switch of a shared mutable client
during a load must not change which context the read used, so tenant B's secret can never be
cached under tenant A's key. Class 1 (value-free output): an extension raising a value-bearing
``BaseException`` must surface only a rebuilt engine error whose chain, notes, args and
``capture_locals`` rendering hold no canary; ``KeyboardInterrupt`` and ``SystemExit`` pass through
untouched. Red baseline against bf76e761: docs/plans/evidence/2026-10-30-f13-r4-red-baseline.txt.
"""

from __future__ import annotations

import base64
import os
import threading
import traceback
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine import secret_providers
from decoy_engine.keyprovider import MaskSecretError, key_provider_from_ref
from decoy_engine.secret_providers import (
    API_VERSION,
    MASK_ROOT_V1,
    REQUIREMENTS,
    SecretCachePolicy,
    SecretDescriptor,
    SecretProviderRegistry,
    SecretProviderUnavailable,
    admit_mask_key,
    default_cache,
    parse_ref,
    prepare_legacy,
    prepare_secret,
)
from tests.secret_providers._helpers import FakeClock, ProtocolProvider
from tests.secret_providers.test_leak_sentry import assert_absent, render_everywhere

# Optional so the module still collects at bf76e761, where the tests must fail for the intended
# reason (a poisoned cache, an escaped BaseException) and not on an import.
ResolveContext: Any = getattr(secret_providers, "ResolveContext", None)

REQ = (REQUIREMENTS[MASK_ROOT_V1],)
REF = parse_ref("test:thing?encoding=raw")
WAIT = 5


@pytest.fixture(autouse=True)
def _clean_cache():
    default_cache().clear()
    yield
    default_cache().clear()


class _Switch:
    """Another thread that flips a shared client to tenant B at ``mid`` and back at ``after``."""

    def __init__(self, to_b, to_a) -> None:
        self.mid, self.flipped = threading.Event(), threading.Event()
        self.after, self.restored = threading.Event(), threading.Event()
        self.ran = False

        def run() -> None:
            if not self.mid.wait(WAIT):
                return
            to_b()
            self.flipped.set()
            if self.after.wait(WAIT):
                to_a()
                self.ran = True
            self.restored.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def at_mid(self) -> None:
        self.mid.set()
        assert self.flipped.wait(WAIT)

    def at_after(self) -> None:
        self.after.set()
        assert self.restored.wait(WAIT)

    def join(self) -> None:
        self.thread.join(WAIT)
        assert self.ran, "the switch never happened: the test proved nothing"


# --- BLOCKER (class 3): the read uses the context the key was derived from -------------------

if ResolveContext is not None:

    @dataclass(frozen=True, repr=False)
    class TenantContext(ResolveContext):
        namespace: str = ""


class _SharedClientProvider(ProtocolProvider):
    """A conforming provider over a shared mutable client (its ``namespace``).

    It reads with the context it is handed. A caller that hands none (the r3 engine) leaves it
    only the live client to read through, which is exactly the defect under test.
    """

    def __init__(self, switch_at: str = "describe") -> None:
        super().__init__()
        self.namespace = "tenant-a"
        self.switch: _Switch | None = None
        self.switch_at = switch_at
        self.read_namespaces: list[str] = []

    def _context_taken(self) -> None:
        if self.switch is not None and self.switch_at == "context" and not self.switch.mid.is_set():
            self.switch.at_mid()  # tenant B from the moment the keyed context is taken

    def resolve_context(self):
        context = TenantContext(scheme=self.scheme, namespace=self.namespace)
        self._context_taken()
        return context

    def cache_context(self) -> tuple[str, ...]:  # the r3 hook; unused by the r4 engine
        context = (self.namespace,)
        self._context_taken()
        return context

    def describe(self, ref):
        if self.switch is not None and self.switch_at == "describe":
            self.switch.at_mid()  # the client is tenant B from here until the read returns
        return super().describe(ref)

    def resolve(self, ref, context=None):  # type: ignore[override]
        namespace = context.namespace if context is not None else self.namespace
        self.read_namespaces.append(namespace)
        self.value = namespace.encode() * 5
        resolved = super().resolve(ref, context)
        if self.switch is not None:
            self.switch.at_after()  # back to tenant A before the r3 after-read check
        return resolved

    def flip(self, namespace: str) -> None:
        self.namespace = namespace


def _expected(namespace: str) -> bytes:
    stable = ProtocolProvider("test", value=namespace.encode() * 5)
    with prepare_secret(provider=stable, ref=REF, requirements=REQ) as prepared:
        return prepared.mask_key()


@pytest.mark.parametrize("switch_at", ["describe", "context"])
def test_a_barrier_forced_switch_mid_load_cannot_change_the_read_context(switch_at):
    prov = _SharedClientProvider(switch_at)
    prov.switch = _Switch(lambda: prov.flip("tenant-b"), lambda: prov.flip("tenant-a"))
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    first = prepare_secret(provider=prov, ref=REF, requirements=REQ, cache_policy=policy)
    prov.switch.join()
    assert first.mask_key() == _expected("tenant-a"), "tenant B's secret served for tenant A"
    first.close()
    assert prov.read_namespaces == ["tenant-a"]

    prov.switch = None
    hit = prepare_secret(provider=prov, ref=REF, requirements=REQ, cache_policy=policy)
    assert hit.mask_key() == _expected("tenant-a"), "tenant B's secret cached under tenant A"
    hit.close()
    assert prov.resolve_calls == 1  # positive control: the A entry exists and was hit

    prov.flip("tenant-b")
    other = prepare_secret(provider=prov, ref=REF, requirements=REQ, cache_policy=policy)
    assert other.mask_key() == _expected("tenant-b")
    other.close()
    assert prov.read_namespaces == ["tenant-a", "tenant-b"]


# --- the same property through the shipped Vault adapter --------------------------------------

_TENANTS = {
    ("tenant-a", "tok-a"): base64.b64encode(b"a" * 40).decode(),
    ("tenant-b", "tok-b"): base64.b64encode(b"b" * 40).decode(),
}
VREF = parse_ref("vault://kv/team/app?version=3&field=key&encoding=base64")


def _answer(namespace: str | None, token: str | None) -> dict:
    from tests.secret_providers.test_vault_adapter import payload

    return payload(value=_TENANTS[(namespace or "", token or "")])


class _SharedVaultClient:
    """An authenticated client another thread may re-point (namespace and token) at any time."""

    def __init__(self) -> None:
        self.url, self.token = "https://vault.test:8200", "tok-a"
        self.adapter = SimpleNamespace(namespace="tenant-a", _kwargs={"verify": True, "cert": None})
        self.switch: _Switch | None = None
        self.reads: list[tuple[str, str]] = []
        self.transport_reads: list[tuple[str, str]] = []
        outer = self

        class V2:
            def read_secret_version(self, **_: Any) -> dict:
                return outer.serve(lambda: (outer.adapter.namespace, outer.token), outer.reads)

        self.secrets = SimpleNamespace(kv=SimpleNamespace(v2=V2()))

    def serve(self, route, log: list) -> dict:
        if self.switch is not None:
            self.switch.at_mid()
        namespace, token = route()
        log.append((namespace, token))
        answer = _answer(namespace, token)
        if self.switch is not None:
            self.switch.at_after()
        return answer

    def to(self, tenant: str) -> None:
        self.adapter.namespace, self.token = f"tenant-{tenant}", f"tok-{tenant}"


def _transport_for(shared: _SharedVaultClient):
    """``hvac.Client`` built from explicit settings: it routes by its constructor arguments."""

    class Transport:
        def __init__(self, url=None, token=None, namespace=None, **_: Any) -> None:
            outer = self

            class V2:
                def read_secret_version(self, **__: Any) -> dict:
                    return shared.serve(lambda: (namespace, token), shared.transport_reads)

            outer.secrets = SimpleNamespace(kv=SimpleNamespace(v2=V2()))

    return Transport


def _vault_key(shared_tenant: str, monkeypatch) -> bytes:
    from decoy_engine.secret_providers.vault import VaultKV2Provider

    stable = _SharedVaultClient()
    stable.to(shared_tenant)
    monkeypatch.setattr(_hvac(), "Client", _transport_for(stable))
    with VaultKV2Provider(stable).preflight(VREF, REQ, SecretCachePolicy()) as prepared:
        return prepared.mask_key()


def _hvac():
    return pytest.importorskip("hvac")


def test_a_barrier_forced_vault_client_switch_mid_load_cannot_change_the_read_context(
    monkeypatch,
):
    from decoy_engine.secret_providers.vault import VaultKV2Provider

    key_a, key_b = _vault_key("a", monkeypatch), _vault_key("b", monkeypatch)
    assert key_a != key_b  # non-vacuity: the tenants really hold different secrets

    shared = _SharedVaultClient()
    monkeypatch.setattr(_hvac(), "Client", _transport_for(shared))
    provider = VaultKV2Provider(shared)
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    shared.switch = _Switch(lambda: shared.to("b"), lambda: shared.to("a"))
    first = provider.preflight(VREF, REQ, policy)
    shared.switch.join()
    assert first.mask_key() == key_a, "tenant B's secret served for tenant A"
    first.close()

    shared.switch = None
    hit = provider.preflight(VREF, REQ, policy)
    assert hit.mask_key() == key_a, "tenant B's secret cached under tenant A"
    hit.close()
    # The read went through a transport built from the construction-time settings, never
    # through the caller's shared, mutable client.
    assert shared.reads == []
    assert shared.transport_reads == [("tenant-a", "tok-a")]


def test_a_vault_provider_is_immutable_and_never_renders_its_settings(monkeypatch):
    from decoy_engine.secret_providers.vault import VaultKV2Provider

    shared = _SharedVaultClient()
    monkeypatch.setattr(_hvac(), "Client", _transport_for(shared))
    provider = VaultKV2Provider(shared)
    for name in ("_context", "_clock", "_client", "token"):
        with pytest.raises(AttributeError):
            setattr(provider, name, None)
    shared.to("b")  # re-pointing the caller's client after construction changes nothing
    with provider.preflight(VREF, REQ, SecretCachePolicy()) as prepared:
        assert prepared.mask_key() == _vault_key("a", monkeypatch)
    context = provider.resolve_context()
    for rendered in (repr(provider), str(provider), repr(context), str(context)):
        assert (
            "tok-a" not in rendered and "tenant-a" not in rendered and "vault.test" not in rendered
        )


# --- the engine accepts only an immutable, value-free context ------------------------------


def _with_context(make):
    class Provider(ProtocolProvider):
        def resolve_context(self):
            return make(self)

    return Provider()


def _bad_contexts() -> dict[str, Any]:
    @dataclass(repr=False)
    class Mutable:
        scheme: str = "test"

    @dataclass(frozen=True)
    class Rendering(ResolveContext):  # dataclass repr would print every field
        token: str = "tok"

    @dataclass(frozen=True, repr=False)
    class HoldsAList(ResolveContext):
        parts: list = field(default_factory=list)

    class NotADataclass(ResolveContext):
        pass

    return {
        "not-a-context": lambda p: Mutable(),
        "dataclass-repr": lambda p: Rendering(scheme=p.scheme),
        "mutable-field": lambda p: HoldsAList(scheme=p.scheme),
        "plain-subclass": lambda p: NotADataclass(scheme=p.scheme),
        "other-scheme": lambda p: ResolveContext(scheme="other"),
    }


@pytest.mark.parametrize(
    "case", ["not-a-context", "dataclass-repr", "mutable-field", "plain-subclass", "other-scheme"]
)
def test_a_context_that_is_not_immutable_and_value_free_fails_closed(case):
    assert ResolveContext is not None, "the r4 resolve context is missing"
    prov = _with_context(_bad_contexts()[case])
    with pytest.raises(SecretProviderUnavailable):
        prepare_secret(
            provider=prov,
            ref=REF,
            requirements=REQ,
            cache_policy=SecretCachePolicy(ttl_seconds=30, clock=FakeClock()),
        )
    assert prov.resolve_calls == 0
    assert len(default_cache()) == 0


# --- HIGH (class 1): one translation boundary for every BaseException ------------------------

CANARY = "F13-R4-CANARY-dsn://svc:hunter2@vault.internal/secret/path"


class ValueBearingBase(BaseException):
    """Raised by extension code: its args, notes and repr all carry the canary."""

    def __repr__(self) -> str:
        return f"ValueBearingBase({CANARY})"


def _boom() -> None:
    token = CANARY  # a value-bearing local in the raising frame
    exc = ValueBearingBase(token)
    exc.__notes__ = [token]  # what ``add_note`` sets on 3.11+
    raise exc


@dataclass
class _BaseCanaryProvider:
    token: str = CANARY
    api_version = API_VERSION
    scheme = "canary"

    def __repr__(self) -> str:
        return f"_BaseCanaryProvider({self.token})"

    def describe(self, ref):
        return SecretDescriptor(scheme=self.scheme, key_version="v1", renewable=False)

    def resolve(self, ref, context=None):
        _boom()

    def preflight(self, ref, requirements, cache_policy):
        _boom()


@dataclass
class _BaseCanaryKeyProvider:
    token: str = CANARY
    key_version: int = 1

    def mask_key(self) -> bytes:
        _boom()
        return b""


@dataclass
class _BaseCanaryRegistry(SecretProviderRegistry):
    provider: object = None
    token: str = CANARY

    def __post_init__(self) -> None:
        SecretProviderRegistry.__init__(self, (self.provider,))  # type: ignore[arg-type]


_CFG = {"global_settings": {"mask_secret_ref": "canary:x"}, "tables": []}
_KEYED = {
    "global_settings": {},
    "tables": [{"name": "t", "columns": [{"name": "email", "strategy": "hash", "namespace": "e"}]}],
}

BASE_CASES = {
    "prepare_secret-resolve": lambda: prepare_secret(
        provider=_BaseCanaryProvider(), ref=parse_ref("canary:x?encoding=raw"), requirements=REQ
    ),
    "admit-registry-preflight": lambda: admit_mask_key(
        _CFG, registry=_BaseCanaryRegistry(provider=_BaseCanaryProvider())
    ),
    "admit-key-provider": lambda: admit_mask_key(_KEYED, key_provider=_BaseCanaryKeyProvider()),
    "prepare_legacy-key-provider": lambda: prepare_legacy(_BaseCanaryKeyProvider(), REQ),
    "key_provider_from_ref-preflight": lambda: key_provider_from_ref("canary:x"),
}


@pytest.fixture
def canary_registry(registry):
    registry.register(_BaseCanaryProvider())
    return registry


def _raised(call) -> BaseException:
    # Canaries are built inside ``call`` so no frame of this module binds one.
    try:
        call()
    except BaseException as exc:
        return exc
    raise AssertionError("expected an admission failure")


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is s for s in seen):
            continue
        seen.append(cur)
        todo += [cur.__cause__, cur.__context__]
    return seen


@pytest.mark.parametrize("case", sorted(BASE_CASES))
def test_an_extension_base_exception_is_rebuilt_from_a_fixed_code(case, canary_registry):
    exc = _raised(BASE_CASES[case])
    assert isinstance(exc, MaskSecretError), "a value-bearing BaseException escaped the boundary"
    assert exc.code == "secret_provider_unavailable"
    assert exc.__cause__ is None and exc.__context__ is None and exc.__suppress_context__
    for link in _chain(exc):
        texts = [repr(link), str(link), *map(repr, link.args)]
        texts += list(getattr(link, "__notes__", []))
        assert not any(CANARY in t for t in texts)
        assert not isinstance(link, ValueBearingBase)


@pytest.mark.skipif(
    "MUTANT_UNDER_TEST" in os.environ,
    reason="mutmut wraps every function in a trampoline frame that holds the call arguments",
)
@pytest.mark.parametrize("case", sorted(BASE_CASES))
def test_an_extension_base_exception_leaves_no_value_bearing_local(
    case, canary_registry, caplog, capsys
):
    exc = _raised(BASE_CASES[case])
    assert isinstance(exc, MaskSecretError), "a value-bearing BaseException escaped the boundary"
    locals_view = "".join(
        traceback.TracebackException.from_exception(exc, capture_locals=True).format()
    )
    assert "_preflight.py" in locals_view  # positive control: the boundary frames were rendered
    surfaces = render_everywhere(exc, caplog, capsys)
    surfaces["capture_locals_r4"] = locals_view
    assert_absent(surfaces, [CANARY, "hunter2", "vault.internal"])


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_interrupts_and_exits_pass_through_untouched(kind):
    signal = kind()

    class Interrupting(ProtocolProvider):
        def resolve(self, ref, context=None):  # type: ignore[override]
            raise signal

    with pytest.raises(kind) as info:
        prepare_secret(provider=Interrupting(), ref=REF, requirements=REQ)
    assert info.value is signal


# --- hostile error objects: classifying one can never raise out of the handler ----------------


class _NoInitError(MaskSecretError):
    def __init__(self) -> None:  # an extension subclass that never sets ``code`` or ``message``
        pass


class _RaisingCodeError(MaskSecretError):
    @property  # type: ignore[override]
    def code(self) -> str:
        raise ValueBearingBase(CANARY)

    @code.setter
    def code(self, _: object) -> None:
        pass


class _ForeignCode(str):
    def __eq__(self, other: object) -> bool:
        raise ValueBearingBase(CANARY)

    __hash__ = str.__hash__


class _NoMessageBadRefError(MaskSecretError):
    code = "bad_secret_ref"

    def __init__(self) -> None:
        pass


HOSTILE_ERRORS = {
    "no-init": (_NoInitError, "secret_provider_unavailable"),
    "raising-code": (lambda: _RaisingCodeError(code="x"), "secret_provider_unavailable"),
    "foreign-str-code": (
        lambda: MaskSecretError(code=_ForeignCode("secret_access_denied"), message=CANARY),
        "secret_provider_unavailable",
    ),
    "bad-ref-without-message": (_NoMessageBadRefError, "bad_secret_ref"),
}


@pytest.mark.parametrize("case", sorted(HOSTILE_ERRORS))
def test_a_hostile_error_object_is_classified_without_escaping(case):
    make, code = HOSTILE_ERRORS[case]

    class Hostile(ProtocolProvider):
        def resolve(self, ref, context=None):  # type: ignore[override]
            raise make()

    exc = _raised(lambda: prepare_secret(provider=Hostile(), ref=REF, requirements=REQ))
    assert isinstance(exc, MaskSecretError) and exc.code == code
    assert exc.__cause__ is None and exc.__context__ is None
    assert CANARY not in str(exc) + repr(exc)

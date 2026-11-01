"""Assertions added from the r3 mutation report: error texts, boundaries and ownership.

Each test names the behavior a surviving mutant used to get away with; the equivalent mutants
that remain are listed with reasons in docs/plans/evidence/2026-10-30-f13-r3-mutation-report.md.
"""

from __future__ import annotations

import pickle
import threading
import time
from types import SimpleNamespace

import pytest

from decoy_engine import keyprovider
from decoy_engine.keyprovider import (
    KEY_REQUIRED_GUIDANCE,
    KeyedStrategyRequiresSecret,
    MaskSecretError,
    MissingMaskSecret,
    SecretKeyProvider,
    SeedKeyProvider,
    WeakMaskSecret,
)
from decoy_engine.secret_providers import (
    EXPIRY_SKEW_SECONDS,
    HMAC_SHA256_V1,
    MASK_ROOT_V1,
    REQUIREMENTS,
    AdmissionScope,
    PreparedSecret,
    SecretBuffer,
    SecretCache,
    SecretCachePolicy,
    SecretExpired,
    SecretProviderRegistry,
    _preflight,
    admit_mask_key,
    default_cache,
    parse_ref,
    prepare_legacy,
    prepare_secret,
)
from decoy_engine.secret_providers import _errors as errors
from tests.secret_providers._helpers import (
    FakeClock,
    ProtocolProvider,
    keyed_config,
    unkeyed_config,
)

REQ = (REQUIREMENTS[MASK_ROOT_V1],)
REF = parse_ref("test:thing?encoding=raw")


@pytest.fixture(autouse=True)
def _clean_cache():
    default_cache().clear()
    yield
    default_cache().clear()


def _prepared(expires_at: float | None = None) -> PreparedSecret:
    return PreparedSecret(
        SecretBuffer.adopt(bytearray(b"k" * 32)),
        key_version="v1",
        requirement_ids=(MASK_ROOT_V1,),
        expires_at=expires_at,
    )


# --- error classes: fixed code and constant text ------------------------------------------------

CLOSED = [
    errors.SecretProviderUnknown,
    errors.SecretProviderUnavailable,
    errors.SecretAccessDenied,
    errors.SecretVersionUnavailable,
    errors.SecretExpired,
    errors.SecretLeaseUnsupported,
    errors.SecretMaterialInvalid,
]


@pytest.mark.parametrize("cls", CLOSED, ids=lambda c: c.__name__)
def test_closed_errors_carry_their_constant_text(cls):
    exc = cls()
    assert exc.message == cls._MESSAGE and exc.message
    assert str(exc) == f"{exc.code}: {cls._MESSAGE}"
    assert repr(exc) == f"{cls.__name__}(code={exc.code!r})"


def test_the_closed_buffer_error_text_and_legacy_error_defaults():
    assert str(errors.SecretClosedError()) == "secret buffer is closed"
    for cls in (KeyedStrategyRequiresSecret, MissingMaskSecret, WeakMaskSecret):
        assert cls().message == "" and str(cls()) == cls().code
        assert cls("why").message == "why"


def test_closed_error_keeps_engine_texts_and_drops_every_other():
    ref = errors.closed_error(
        MaskSecretError(code="bad_secret_ref", message=errors.BAD_REF_MESSAGE)
    )
    assert ref.message == errors.BAD_REF_MESSAGE
    enc = errors.closed_error(
        MaskSecretError(code="bad_secret_ref", message=errors.BAD_ENCODING_MESSAGE)
    )
    assert enc.message == errors.BAD_ENCODING_MESSAGE
    other = errors.closed_error(MaskSecretError(code="bad_secret_ref", message="/secret/path"))
    assert other.message == errors.BAD_REF_MESSAGE
    plain = errors.closed_error(MaskSecretError(code="bad_secret_ref"))
    assert plain.message == errors.BAD_REF_MESSAGE
    assert errors.closed_error(ValueError("x")).code == "secret_provider_unavailable"


# --- opaque box ---------------------------------------------------------------------------------


def test_the_opaque_box_has_a_fixed_rendering_and_cannot_be_pickled():
    box = _preflight._OpaqueBox("F13-CANARY")
    assert repr(box) == "<opaque>" == str(box) == f"{box}"
    with pytest.raises(TypeError, match="^opaque boxes cannot be pickled$"):
        pickle.dumps(box)


# --- admission: requirement ids, policy handoff, ownership --------------------------------------


def test_a_different_requirement_set_never_hits_another_sets_entry():
    prov = ProtocolProvider()
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    ref = parse_ref("test:thing?encoding=raw")
    prepare_secret(provider=prov, ref=ref, requirements=REQ, cache_policy=policy).close()
    prepare_secret(
        provider=prov,
        ref=ref,
        requirements=(REQUIREMENTS[HMAC_SHA256_V1],),
        cache_policy=policy,
    ).close()
    assert prov.resolve_calls == 2


def test_a_one_second_ttl_is_cached():
    prov = ProtocolProvider()
    policy = SecretCachePolicy(ttl_seconds=1, clock=FakeClock())
    for _ in range(2):
        prepare_secret(provider=prov, ref=REF, requirements=REQ, cache_policy=policy).close()
    assert prov.resolve_calls == 1


def test_the_mask_root_output_wins_whatever_the_requirement_order():
    value = b"0123456789abcdef0123456789abcdef-tail"
    prov = ProtocolProvider(value=value)
    prepared = prepare_secret(
        provider=prov,
        ref=REF,
        requirements=(REQUIREMENTS[HMAC_SHA256_V1], REQUIREMENTS[MASK_ROOT_V1]),
    )
    assert prepared.mask_key() == SecretKeyProvider(value).mask_key()
    assert prepared.requirement_ids == (HMAC_SHA256_V1, MASK_ROOT_V1)


def test_a_value_expiring_exactly_now_is_expired():
    clock = FakeClock()
    prov = ProtocolProvider(expires_at=clock.wall())
    with pytest.raises(SecretExpired):
        prepare_secret(
            provider=prov,
            ref=REF,
            requirements=REQ,
            cache_policy=SecretCachePolicy(clock=clock),
        )


def test_prepared_secrets_report_their_requirement_ids():
    assert prepare_secret(
        provider=ProtocolProvider(), ref=REF, requirements=REQ
    ).requirement_ids == (MASK_ROOT_V1,)
    legacy = prepare_legacy(SecretKeyProvider(b"x" * 40), REQ)
    assert legacy.requirement_ids == (MASK_ROOT_V1,)


class _Recording(ProtocolProvider):
    def __init__(self) -> None:
        super().__init__("rec")
        self.policies: list[object] = []

    def preflight(self, ref, requirements, cache_policy):  # type: ignore[override]
        self.policies.append(cache_policy)
        return super().preflight(ref, requirements, cache_policy)


def test_admit_hands_the_callers_policy_or_a_default_to_the_provider(tmp_path):
    prov = _Recording()
    registry = SecretProviderRegistry((prov,))
    cfg = keyed_config(tmp_path, mask_secret_ref="rec:x?encoding=raw")
    admit_mask_key(cfg, registry=registry).close()
    given = SecretCachePolicy(ttl_seconds=5, clock=FakeClock())
    admit_mask_key(cfg, registry=registry, cache_policy=given).close()
    default, passed = prov.policies
    assert isinstance(default, SecretCachePolicy) and default.ttl_seconds == 0
    assert passed is given


def test_admission_outcomes_own_their_prepared_secret(tmp_path):
    prov = ProtocolProvider("own")
    registry = SecretProviderRegistry((prov,))
    cfg = keyed_config(tmp_path, mask_secret_ref="own:x?encoding=raw")
    admitted = admit_mask_key(cfg, registry=registry)
    assert admitted.prepared is not None and not admitted.prepared.closed
    admitted.close()
    assert admitted.prepared.closed

    legacy_provider = SecretKeyProvider(b"y" * 40)
    legacy = admit_mask_key(keyed_config(tmp_path), key_provider=legacy_provider)
    assert legacy.prepared is not None and legacy.requirements
    legacy.close()
    assert legacy.prepared.closed

    unkeyed = admit_mask_key(unkeyed_config(tmp_path), key_provider=legacy_provider)
    assert unkeyed.key_provider is legacy_provider and unkeyed.prepared is None


def test_the_scope_closes_what_it_adopted(tmp_path):
    cfg = keyed_config(tmp_path)
    scope = AdmissionScope()
    admitted = scope.adopt(admit_mask_key(cfg, key_provider=SecretKeyProvider(b"z" * 40)))
    assert scope.admitted is admitted
    with scope:
        pass
    assert admitted.prepared is not None and admitted.prepared.closed


def test_keyed_gates_fail_with_the_guidance_text(tmp_path, monkeypatch):
    with pytest.raises(KeyedStrategyRequiresSecret) as none:
        admit_mask_key(keyed_config(tmp_path))
    assert none.value.message == KEY_REQUIRED_GUIDANCE
    with pytest.raises(KeyedStrategyRequiresSecret) as seed:
        prepare_legacy(SeedKeyProvider(b"12345678"), REQ)
    assert seed.value.message == KEY_REQUIRED_GUIDANCE
    with pytest.raises(KeyedStrategyRequiresSecret) as short:
        prepare_legacy(SimpleNamespace(mask_key=lambda: b"short", key_version=1), REQ)  # type: ignore[arg-type]
    assert short.value.message == KEY_REQUIRED_GUIDANCE

    monkeypatch.setattr(_preflight, "secret_requirements_for_plan", lambda plan: REQ)
    with pytest.raises(KeyedStrategyRequiresSecret) as late:
        _preflight.assert_plan_requirements_admitted(object(), _preflight.AdmittedKey(None))  # type: ignore[arg-type]
    assert late.value.message == KEY_REQUIRED_GUIDANCE


def test_require_mask_key_gates_carry_the_guidance_text(monkeypatch):
    plan = SimpleNamespace(seed_envelope=SimpleNamespace(job_seed=b"12345678"))
    monkeypatch.setattr(keyprovider, "plan_has_keyed_strategy", lambda _plan: True)
    for provider in (
        None,
        SeedKeyProvider(b"12345678"),
        SimpleNamespace(mask_key=lambda: b"short"),
    ):
        with pytest.raises(KeyedStrategyRequiresSecret) as info:
            keyprovider.require_mask_key(plan, provider)  # type: ignore[arg-type]
        assert info.value.message == KEY_REQUIRED_GUIDANCE


# --- cache: capacity, boundaries, ownership, flights --------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_capacity_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError, match="^cache capacity must be a positive integer$"):
        SecretCache(bad)  # type: ignore[arg-type]


def test_capacity_one_is_valid_and_bounds_the_cache():
    cache = SecretCache(1)
    clock, first, second = FakeClock(), object(), object()
    cache.get_or_load("a", owners=(first,), ttl=30, clock=clock, loader=_prepared).close()
    assert len(cache) == 1
    old = next(iter(cache._entries.values())).master
    cache.get_or_load("b", owners=(second,), ttl=30, clock=clock, loader=_prepared).close()
    assert len(cache) == 1 and old.closed  # the evicted entry was wiped


def test_a_reload_closes_the_entry_it_replaces():
    cache, owners, clock = SecretCache(), (object(),), FakeClock()
    cache.get_or_load("k", owners=owners, ttl=60, clock=clock, loader=_prepared).close()
    old = next(iter(cache._entries.values())).master
    clock.advance(2)
    cache.get_or_load("k", owners=owners, ttl=1, clock=clock, loader=_prepared).close()
    assert old.closed and len(cache) == 1


def test_invalidate_removes_and_wipes_the_entry():
    cache, owners = SecretCache(), (object(),)
    cache.get_or_load("k", owners=owners, ttl=30, clock=FakeClock(), loader=_prepared).close()
    master = next(iter(cache._entries.values())).master
    cache.invalidate("k", owners=owners)
    assert len(cache) == 0 and master.closed


def test_an_owner_mismatch_or_expiry_evicts_and_misses():
    cache, owners, clock = SecretCache(), (object(),), FakeClock()
    cache.get_or_load("k", owners=owners, ttl=30, clock=clock, loader=_prepared).close()
    key = cache._key("k", owners)
    assert cache._hit_locked(key, (object(),), 30.0) is None and len(cache) == 0

    cache.get_or_load("k", owners=owners, ttl=30, clock=clock, loader=_prepared).close()
    clock.advance(31)
    assert cache._hit_locked(key, owners, 30.0) is None and len(cache) == 0


def test_the_remaining_lifetime_boundary_is_exclusive():
    clock = FakeClock()
    exact = _prepared(expires_at=clock.wall() + EXPIRY_SKEW_SECONDS)
    short = _prepared(expires_at=clock.wall() + EXPIRY_SKEW_SECONDS + 0.5)
    for prepared, cached in ((exact, 0), (short, 1)):
        cache = SecretCache()
        cache.get_or_load(
            "k", owners=(object(),), ttl=30, clock=clock, loader=lambda p=prepared: p.clone()
        ).close()
        assert len(cache) == cached


def _wait(predicate) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_a_second_invalidation_fences_a_load_that_began_after_the_first():
    cache, owners, clock = SecretCache(), (object(),), FakeClock()
    a_in, a_go, b_in, b_go = (threading.Event() for _ in range(4))

    def slow(started, go):
        def loader() -> PreparedSecret:
            started.set()
            assert go.wait(5)
            return _prepared()

        return loader

    def call(loader):
        return threading.Thread(
            target=lambda: cache.get_or_load(
                "k", owners=owners, ttl=30, clock=clock, loader=loader
            ).close()
        )

    first, second = call(slow(a_in, a_go)), call(slow(b_in, b_go))
    first.start()
    assert a_in.wait(5)
    second.start()  # queues on the same flight behind the first
    _wait(lambda: any(f.callers == 2 for f in cache._locks.values()))
    cache.invalidate("k", owners=owners)
    a_go.set()
    assert b_in.wait(5)  # the queued load starts only after the first invalidation
    cache.invalidate("k", owners=owners)
    b_go.set()
    first.join(5)
    second.join(5)
    assert len(cache) == 0


def test_a_second_clear_fences_a_load_that_began_after_the_first():
    cache, owners = SecretCache(), (object(),)
    cache.clear()
    started, release = threading.Event(), threading.Event()

    def loader() -> PreparedSecret:
        started.set()
        assert release.wait(5)
        return _prepared()

    worker = threading.Thread(
        target=lambda: cache.get_or_load(
            "k", owners=owners, ttl=30, clock=FakeClock(), loader=loader
        ).close()
    )
    worker.start()
    assert started.wait(5)
    cache.clear()
    release.set()
    worker.join(5)
    assert len(cache) == 0


def test_flights_count_their_callers_and_outlive_all_but_the_last():
    cache, owners = SecretCache(), (object(),)
    clock = FakeClock()
    first_in, release_first, release_second = (threading.Event() for _ in range(3))
    expired = _prepared(expires_at=clock.wall() - 100)  # never cached: forces a second real load

    def slow_first() -> PreparedSecret:
        first_in.set()
        assert release_first.wait(5)
        return expired.clone()

    def slow_second() -> PreparedSecret:
        assert release_second.wait(5)
        return _prepared(expires_at=clock.wall() - 100)

    def call(loader):
        return threading.Thread(
            target=lambda: cache.get_or_load(
                "k", owners=owners, ttl=30, clock=clock, loader=loader
            ).close()
        )

    a, b = call(slow_first), call(slow_second)
    a.start()
    assert first_in.wait(5)
    b.start()
    _wait(lambda: any(f.callers == 2 for f in cache._locks.values()))
    release_first.set()
    a.join(5)
    assert [f.callers for f in cache._locks.values()] == [1]  # still held for the waiter
    release_second.set()
    b.join(5)
    assert not cache._locks

"""C6b-fakerfix: byte-parity + reuse proof for the shared Faker in text_mask.

``_mask_faker`` now reuses one thread-local ``Faker`` (``_faker_span.shared_faker``)
re-seeded per span, instead of constructing a fresh ``Faker()`` per span. Byte
parity is the whole contract: reuse MUST reproduce fresh-per-span output
char-for-char. These tests pin that contract and the reuse itself.

The fresh-per-span reference (``_fresh_reference``) reproduces the PRE-FIX
``_mask_faker`` body exactly: ``Faker()`` per call, ``seed_instance(seed)``, the
``getattr`` method lookup, the callable check, the ``str(method())`` exception
boundary, and the ``fake.name()`` fallback WITHOUT reseeding. It reads
``text_mask._FAKER_METHOD`` live so a monkeypatch to the map is mirrored.

Test 4a (same-thread reuse, observed at the production call site) pins the fix
itself: an unchanged fresh-``Faker()``-per-span implementation fails it. See the
module-level RED demonstration note in the slice handback.
"""

from __future__ import annotations

import hashlib
import threading

import pytest
from faker import Faker

import decoy_engine.transforms._faker_span as faker_span_mod
import decoy_engine.transforms.text_mask as text_mask_mod
from decoy_engine.transforms.text_mask import _mask_faker, _span_key, mask_cell

# ── fixtures / helpers ──────────────────────────────────────────────────────────

_MASK_KEY = b"\x11" * 32  # stable test key; not a real secret


@pytest.fixture(autouse=True)
def _reset_faker_tls():
    """Clear the thread-local cache before each test so reuse observations are
    hermetic (a stale instance from a prior test cannot mask a regression)."""
    faker_span_mod._FAKER_TLS = threading.local()
    yield
    faker_span_mod._FAKER_TLS = threading.local()


# Detector id -> the Faker method it reaches (the parity-sensitive set), plus an
# unmapped id that must default to "name".
_DETECTORS: list[tuple[str, str]] = [
    ("person_name", "name"),
    ("first_name", "first_name"),
    ("last_name", "last_name"),
    ("address", "address"),
    ("location", "city"),
    ("__c6b_unmapped__", "name"),
]

_SPAN_TEXTS = [
    "John Smith",
    "Jane Doe",
    "42 Oakwood Terrace",
    "Boston",
    "Dr. Alice Johnson",
    "O'Brien",
    "María García",
    "a",
    "",
    "123 Main St, Apt 4B",
]


def _span_keys() -> list[bytes]:
    """A wide seed sample: both 4-byte extremes, keys derived from varied span
    texts, and a deterministic spread. ``_mask_faker`` uses only ``span_key[:4]``."""
    keys: list[bytes] = [
        b"\x00\x00\x00\x00" + b"pad-min",  # seed 0
        b"\xff\xff\xff\xff" + b"pad-max",  # seed 0xffffffff
    ]
    keys.extend(_span_key(_MASK_KEY, t) for t in _SPAN_TEXTS)
    keys.extend(hashlib.sha256(f"c6b-seed-{i}".encode()).digest() for i in range(40))
    return keys


def _fresh_reference(span_key: bytes, detector_id: str) -> str:
    """Faithful fresh-``Faker()``-per-span reproduction of the PRE-FIX body.

    Reads ``text_mask._FAKER_METHOD`` live so map monkeypatches are mirrored.
    """
    method_name = text_mask_mod._FAKER_METHOD.get(detector_id, "name")
    seed = int.from_bytes(span_key[:4], "big")
    fake = Faker()  # fresh per call: the PRE-FIX behavior
    fake.seed_instance(seed)
    method = getattr(fake, method_name, None)
    if callable(method):
        try:
            return str(method())
        except Exception:
            pass
    return str(fake.name())


# ── Test 1: exhaustive byte-parity vs the fresh-per-span reference ───────────────


def test_byte_parity_all_methods_all_seeds():
    """Reused ``_mask_faker`` == fresh-per-span reference char-for-char, for every
    reachable method (name/first_name/last_name/address/city) via its detector id
    plus an unmapped id, across the wide seed sample incl. 0 and 0xffffffff."""
    keys = _span_keys()
    for detector_id, _method in _DETECTORS:
        for span_key in keys:
            reused = _mask_faker("ignored-matched-text", span_key, detector_id)
            fresh = _fresh_reference(span_key, detector_id)
            assert reused == fresh, (
                f"byte-parity MISMATCH detector={detector_id!r} "
                f"seed={int.from_bytes(span_key[:4], 'big')}: "
                f"reused={reused!r} fresh={fresh!r}"
            )


def test_byte_parity_address_and_last_name_literals():
    """Explicit address + last_name coverage (the committed KATs pin only
    first_name/name/city): each must equal the fresh reference and actually
    exercise its own method (output differs from the name() fallback)."""
    for detector_id in ("address", "last_name"):
        for span_key in _span_keys():
            reused = _mask_faker("x", span_key, detector_id)
            assert reused == _fresh_reference(span_key, detector_id)
            # Prove the method is actually exercised, not the name() fallback: for the
            # same seed, address()/last_name() produce a different string than name()
            # (dennis LOW-1 - stronger than a mere non-empty guard).
            name_fallback = _fresh_reference(span_key, "__unmapped_to_name__")
            assert reused not in ("", name_fallback)


def test_byte_parity_cell_level_faker_override_end_to_end():
    """A detector whose default is non-faker (email -> redact), routed to faker via
    per_detector_strategy, runs end-to-end through ``mask_cell`` and produces the
    reference faker value (email is unmapped -> name)."""
    text = "Reach me at alice@example.com today."
    matched = "alice@example.com"
    output = mask_cell(text, _MASK_KEY, strategy_map={"email": "faker"})
    span_key = _span_key(_MASK_KEY, matched)
    expected = _fresh_reference(span_key, "email")
    assert expected != ""
    assert expected in output
    assert matched not in output  # the raw email is gone


# ── Test 2: fallback-path parity with a seed-dependent failure stub ──────────────


def _failing_draw_then_raise(self, *args, **kwargs):
    """Consume a SEED-DEPENDENT number of draws, THEN raise (not an immediate
    raise). An immediate raise would pass even if the fix erroneously reseeded
    before the fallback."""
    n = self.random.randint(0, 7)
    for _ in range(n):
        self.random.random()
    raise ValueError("stub failure after seed-dependent draws")


def test_fallback_parity_method_raises_mid_draw(monkeypatch):
    """Method raises after seed-dependent draws -> fallback name(). Patch is on the
    Faker class, so it applies identically to the fresh reference's new instances
    and to the reused cached instance (bound via getattr at call time)."""
    monkeypatch.setattr(Faker, "first_name", _failing_draw_then_raise, raising=False)
    for span_key in _span_keys():
        reused = _mask_faker("x", span_key, "first_name")
        fresh = _fresh_reference(span_key, "first_name")
        assert reused == fresh


def test_fallback_parity_attr_non_callable(monkeypatch):
    """Attr present but not callable -> fallback name(), no draws consumed first."""
    monkeypatch.setattr(Faker, "last_name", "not_a_callable_sentinel", raising=False)
    for span_key in _span_keys():
        reused = _mask_faker("x", span_key, "last_name")
        fresh = _fresh_reference(span_key, "last_name")
        assert reused == fresh


def test_fallback_parity_attr_unavailable(monkeypatch):
    """Attr absent (getattr -> None) -> fallback name(). Mapped via a patched
    ``_FAKER_METHOD`` entry pointing at a method Faker genuinely lacks; the
    reference reads the same patched map."""
    patched = dict(text_mask_mod._FAKER_METHOD)
    patched["__c6b_missing__"] = "no_such_faker_method_zzz"
    monkeypatch.setattr(text_mask_mod, "_FAKER_METHOD", patched)
    # sanity: the bogus method really is unavailable on a Faker instance
    assert getattr(Faker(), "no_such_faker_method_zzz", None) is None
    for span_key in _span_keys():
        reused = _mask_faker("x", span_key, "__c6b_missing__")
        fresh = _fresh_reference(span_key, "__c6b_missing__")
        assert reused == fresh


# ── Test 3: reseed isolation / order independence ────────────────────────────────


def test_reseed_isolation_order_independence():
    """Same span_key -> same output regardless of intervening spans drawn on the
    SAME reused instance. seed_instance resets the generator before every span."""
    key_a = _span_key(_MASK_KEY, "John Smith")
    key_b = _span_key(_MASK_KEY, "42 Oakwood Terrace")
    key_c = _span_key(_MASK_KEY, "Boston")

    b_first = _mask_faker("x", key_b, "address")
    # Intervening spans on the same reused instance (different seeds/methods).
    _mask_faker("x", key_a, "person_name")
    _mask_faker("x", key_c, "location")
    _mask_faker("x", key_a, "last_name")
    b_after = _mask_faker("x", key_b, "address")

    assert b_first == b_after
    assert b_first == _fresh_reference(key_b, "address")


# ── Test 4: production reuse (same-thread) + thread safety (forced overlap) ───────


def test_production_reuse_same_thread_observed_at_call_site(monkeypatch):
    """4a: observe the instance ``_mask_faker`` ACTUALLY uses by patching
    ``text_mask.shared_faker`` (the name text_mask imported) with a recording
    wrapper that returns the REAL cached instance and records its id per call.

    Across successive same-thread calls the id must be identical (reuse) and the
    observation count must be the expected nonzero N (zero cannot pass). A
    fresh-``Faker()``-per-span implementation fails this (N distinct ids)."""
    real = faker_span_mod.shared_faker
    recorded: list[int] = []

    def _recording_shared_faker() -> Faker:
        inst = real()
        recorded.append(id(inst))
        return inst

    monkeypatch.setattr(text_mask_mod, "shared_faker", _recording_shared_faker)

    n = 6
    keys = _span_keys()[:n]
    for i, span_key in enumerate(keys):
        _mask_faker("x", span_key, _DETECTORS[i % len(_DETECTORS)][0])

    assert len(recorded) == n, "shared_faker was not called once per span (not wired)"
    assert len(set(recorded)) == 1, "instance id changed across spans (no reuse)"


def test_thread_safety_forced_overlap_distinct_instances(monkeypatch):
    """4b: threads whose draws are forced to OVERLAP after each has reseeded
    (barrier between seed_instance and the draw). Each live thread must use a
    DISTINCT thread-local instance, and every result must equal the single-thread
    fresh reference."""
    n_threads = 4
    barrier = threading.Barrier(n_threads, timeout=30)
    lock = threading.Lock()
    ids_by_thread: dict[int, int] = {}
    real = faker_span_mod.shared_faker

    def _overlapping_shared_faker() -> Faker:
        inst = real()
        if not getattr(inst, "_c6b_barriered", False):
            orig_seed = inst.seed_instance

            def _seed_then_rendezvous(seed, *a, **k):
                result = orig_seed(seed, *a, **k)
                barrier.wait()  # all threads have reseeded; now overlap the draw
                return result

            inst.seed_instance = _seed_then_rendezvous  # worker-thread instance only
            inst._c6b_barriered = True
        with lock:
            ids_by_thread[threading.get_ident()] = id(inst)
        return inst

    monkeypatch.setattr(text_mask_mod, "shared_faker", _overlapping_shared_faker)

    # Distinct (key, detector) per thread so overlap is observable, not degenerate.
    work = [
        (_span_key(_MASK_KEY, "John Smith"), "person_name"),
        (_span_key(_MASK_KEY, "42 Oakwood Terrace"), "address"),
        (_span_key(_MASK_KEY, "Boston"), "location"),
        (_span_key(_MASK_KEY, "Jane Doe"), "last_name"),
    ]
    results: dict[int, str] = {}
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def _worker(idx: int) -> None:
        span_key, detector_id = work[idx]
        try:
            out = _mask_faker("x", span_key, detector_id)
            with results_lock:
                results[idx] = out
        except BaseException as exc:  # surface barrier timeouts etc. as failures
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert all(not t.is_alive() for t in threads), "a worker thread hung"
    # Distinct thread-local instance per live thread (isolation).
    assert len(set(ids_by_thread.values())) == n_threads
    # Every result equals the single-thread fresh reference despite the overlap.
    for idx, (span_key, detector_id) in enumerate(work):
        assert results[idx] == _fresh_reference(span_key, detector_id)

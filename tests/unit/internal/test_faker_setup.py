"""Unit tests for decoy_engine.internal.faker_setup.

QA-internal-synth-providers F1 (2026-06-01, CRITICAL determinism) and
F4 (HIGH correctness) pin the atomic-swap contract on the DB-backed
custom Faker provider registry. The platform's
`sync_db_custom_faker_providers` previously bulk-unregistered then
re-registered providers one at a time; a concurrent reader saw zero
DB-backed providers during the window. The atomic_swap_db_providers
helper closes that window via a single locked swap.

These cells also pin the F4 corrupted-row contract by demonstrating
that bad provider entries are caller-skipped before the swap, so a
corrupted row only affects its own provider and never partially-
unregisters the rest.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from faker import Faker

from decoy_engine.internal.faker_setup import (
    _CUSTOM_FAKER_PROVIDER_VALUES,
    _CUSTOM_FAKER_PROVIDERS,
    atomic_swap_db_providers,
    get_custom_faker_provider_values,
    get_faker_providers,
    list_custom_faker_list_providers,
    register_faker_list_provider,
    unregister_faker_provider,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each cell starts with an empty DB-provider registry.

    The module-level dicts are process-global; without isolation a
    previous cell's registrations leak into the next cell's snapshot
    + the concurrent-reader cell sees ghost providers."""
    snapshot_fns = dict(_CUSTOM_FAKER_PROVIDERS)
    snapshot_vals = dict(_CUSTOM_FAKER_PROVIDER_VALUES)
    _CUSTOM_FAKER_PROVIDERS.clear()
    _CUSTOM_FAKER_PROVIDER_VALUES.clear()
    yield
    _CUSTOM_FAKER_PROVIDERS.clear()
    _CUSTOM_FAKER_PROVIDER_VALUES.clear()
    _CUSTOM_FAKER_PROVIDERS.update(snapshot_fns)
    _CUSTOM_FAKER_PROVIDER_VALUES.update(snapshot_vals)


def _values_fn(values: list[Any]) -> Any:
    """Mimic _make_list_provider_fn from the platform side."""

    def _provider(fake: Faker) -> Any:
        return str(fake.random.choice(values))

    return _provider


class TestQaInternalF1AtomicSwap:
    """QA-internal-synth-providers F1 (CRITICAL determinism):
    atomic_swap_db_providers replaces a set of DB-backed providers
    under one lock acquisition. Readers see either the old set or the
    new set, never an empty in-between."""

    def test_swap_replaces_provider_set_atomically(self):
        register_faker_list_provider("medical_record_number", ["mrn-001", "mrn-002"])
        register_faker_list_provider("internal_employee_id", ["emp-100", "emp-101"])
        assert "medical_record_number" in list_custom_faker_list_providers()
        assert "internal_employee_id" in list_custom_faker_list_providers()

        # New set replaces the old: medical_record_number values change
        # + internal_employee_id is dropped + a new provider appears.
        new_fns = {
            "medical_record_number": _values_fn(["mrn-A", "mrn-B"]),
            "regional_routing_number": _values_fn(["rtn-1", "rtn-2"]),
        }
        new_vals = {
            "medical_record_number": ["mrn-A", "mrn-B"],
            "regional_routing_number": ["rtn-1", "rtn-2"],
        }
        atomic_swap_db_providers(
            unregister={"medical_record_number", "internal_employee_id"},
            new_fn_map=new_fns,
            new_val_map=new_vals,
        )

        assert get_custom_faker_provider_values("medical_record_number") == ["mrn-A", "mrn-B"]
        assert get_custom_faker_provider_values("internal_employee_id") is None
        assert get_custom_faker_provider_values("regional_routing_number") == ["rtn-1", "rtn-2"]

    def test_swap_with_empty_new_maps_just_unregisters(self):
        register_faker_list_provider("acme_widget_id", ["w1", "w2"])
        atomic_swap_db_providers(
            unregister={"acme_widget_id"},
            new_fn_map={},
            new_val_map={},
        )
        assert get_custom_faker_provider_values("acme_widget_id") is None

    def test_swap_snapshots_values_list(self):
        """The caller's `new_val_map` list is copied at swap time so
        post-swap caller-side mutation cannot leak into generation."""
        caller_list = ["v1", "v2"]
        atomic_swap_db_providers(
            unregister=set(),
            new_fn_map={"snap_test": _values_fn(caller_list)},
            new_val_map={"snap_test": caller_list},
        )
        # Mutate caller list AFTER the swap; the registry copy must be
        # unaffected.
        caller_list.append("v3-LEAKED")
        registered = get_custom_faker_provider_values("snap_test")
        assert registered == ["v1", "v2"], (
            f"QA-internal F1: values list snapshot leaked caller mutation. "
            f"Got {registered}, expected ['v1', 'v2']."
        )

    def test_concurrent_reader_never_sees_empty_set(self):
        """The lock contract: a reader running concurrently with a
        swap sees either the prior state or the new state, NEVER an
        in-between zero-provider snapshot.

        We pre-register N providers, then have a writer thread call
        atomic_swap_db_providers replacing them with N different
        providers, while a reader thread loops calling
        get_faker_providers and recording the count. The recorded
        count set must never include 0 (the catastrophic in-between).
        """
        # Seed the registry with five providers.
        baseline_names = [f"baseline_{i}" for i in range(5)]
        for name in baseline_names:
            register_faker_list_provider(name, [f"{name}-v1"])

        new_fns = {f"new_{i}": _values_fn([f"new_{i}-v1"]) for i in range(5)}
        new_vals = {f"new_{i}": [f"new_{i}-v1"] for i in range(5)}

        counts_seen: list[int] = []
        reader_errors: list[BaseException] = []
        stop_reader = threading.Event()
        fake = Faker()

        def reader_loop() -> None:
            # Dennis NIT-1: capture exceptions so a lock regression that
            # tears the dict mid-iteration (RuntimeError: dictionary
            # changed size during iteration) surfaces as an assertion
            # failure instead of a silently-dead thread.
            while not stop_reader.is_set():
                try:
                    providers = get_faker_providers(fake)
                except BaseException as exc:
                    reader_errors.append(exc)
                    return
                custom_count = sum(
                    1 for n in providers if n.startswith("baseline_") or n.startswith("new_")
                )
                counts_seen.append(custom_count)
                # Tight loop, no sleep: maximise chance of catching
                # the swap window if the lock is broken.

        def writer_swap() -> None:
            # Brief delay so the reader gets going first.
            time.sleep(0.01)
            atomic_swap_db_providers(
                unregister=set(baseline_names),
                new_fn_map=new_fns,
                new_val_map=new_vals,
            )

        reader = threading.Thread(target=reader_loop)
        writer = threading.Thread(target=writer_swap)
        reader.start()
        writer.start()
        writer.join()
        # Give the reader a few more cycles after the swap to confirm
        # it converges on the new state.
        time.sleep(0.05)
        stop_reader.set()
        reader.join()

        # Dennis NIT-1: a silently-dead reader thread would otherwise
        # pass the bad_counts assertion vacuously. Surface any exception
        # the reader caught.
        assert not reader_errors, (
            f"QA-internal F1: reader thread crashed during concurrent "
            f"swap probe (lock contract regression suspected): "
            f"{type(reader_errors[0]).__name__}: {reader_errors[0]}"
        )

        # Contract: every observed count is either 5 (baseline state)
        # or 5 (new state). The 0 in-between window must never appear.
        bad_counts = [c for c in counts_seen if c not in (5,)]
        assert not bad_counts, (
            f"QA-internal F1: concurrent reader observed non-5 counts "
            f"({sorted(set(bad_counts))}); lock contract violated. "
            f"Sample of observations: {counts_seen[:50]}"
        )


class TestQaInternalF4PerRowSkip:
    """QA-internal-synth-providers F4 (HIGH correctness): the platform
    side now builds the new registration map BEFORE swapping, so a
    corrupted row only skips itself (not the entire teardown).

    These engine-side cells pin the contract that the atomic_swap
    helper accepts a partial new_fn_map (some rows skipped by the
    caller) and never observes the corrupted entries."""

    def test_swap_accepts_partial_new_fn_map(self):
        """The caller skips bad rows; atomic_swap_db_providers happily
        applies whatever the caller built. No assumption about
        completeness."""
        register_faker_list_provider("p1_old", ["a"])
        register_faker_list_provider("p2_old", ["b"])
        register_faker_list_provider("p3_old", ["c"])

        # Imagine caller skipped p2 due to corrupted values_json.
        atomic_swap_db_providers(
            unregister={"p1_old", "p2_old", "p3_old"},
            new_fn_map={
                "p1_new": _values_fn(["aa"]),
                # p2_new intentionally absent: caller logged + skipped.
                "p3_new": _values_fn(["cc"]),
            },
            new_val_map={
                "p1_new": ["aa"],
                "p3_new": ["cc"],
            },
        )

        assert get_custom_faker_provider_values("p1_new") == ["aa"]
        assert get_custom_faker_provider_values("p2_new") is None
        assert get_custom_faker_provider_values("p3_new") == ["cc"]
        # All old names cleared.
        for old in ("p1_old", "p2_old", "p3_old"):
            assert get_custom_faker_provider_values(old) is None


class TestQaInternalF7MakeFakerInvalidLocale:
    """QA-internal-synth-providers F7 (2026-06-01, MEDIUM correctness):
    make_faker logs a warning when the requested locale is invalid +
    the fallback to en_US fires. Pre-fix the operator who typed
    `faker_locale: de_AT` got silent en_US output with no log line."""

    def test_invalid_locale_logs_warning(self, caplog):
        from decoy_engine.internal.faker_setup import make_faker

        with caplog.at_level("WARNING", logger="decoy_engine.internal.faker_setup"):
            result = make_faker("invalid_locale_xyz")

        # Fallback still produces a working Faker.
        assert result is not None
        # The warning surfaced the locale string + the fallback intent.
        assert any(
            "invalid_locale_xyz" in rec.message
            and "falling back to en_US" in rec.message
            for rec in caplog.records
        ), (
            "QA-internal F7: invalid locale must produce a WARNING log "
            "line so operators can see their locale request was ignored."
        )

    def test_valid_locale_does_not_log_warning(self, caplog):
        from decoy_engine.internal.faker_setup import make_faker

        with caplog.at_level("WARNING", logger="decoy_engine.internal.faker_setup"):
            result = make_faker("en_GB")

        assert result is not None
        # No locale-warning lines.
        locale_warnings = [
            rec for rec in caplog.records
            if "falling back" in rec.message
        ]
        assert locale_warnings == []

    def test_none_locale_does_not_log_warning(self, caplog):
        from decoy_engine.internal.faker_setup import make_faker

        with caplog.at_level("WARNING", logger="decoy_engine.internal.faker_setup"):
            result = make_faker(None)

        assert result is not None
        locale_warnings = [
            rec for rec in caplog.records
            if "falling back" in rec.message
        ]
        assert locale_warnings == []


class TestRegistryLockingPreservesExistingApi:
    """Sanity: adding the lock must not break the existing
    register/unregister/get/list public surface. These cells exercise
    the standalone path that doesn't go through atomic_swap_db_providers."""

    def test_register_then_unregister_round_trip(self):
        register_faker_list_provider("rt_test", ["x", "y", "z"])
        assert get_custom_faker_provider_values("rt_test") == ["x", "y", "z"]
        unregister_faker_provider("rt_test")
        assert get_custom_faker_provider_values("rt_test") is None

    def test_list_custom_faker_list_providers_sorted(self):
        register_faker_list_provider("zzz", ["a"])
        register_faker_list_provider("aaa", ["b"])
        register_faker_list_provider("mmm", ["c"])
        names = list_custom_faker_list_providers()
        assert names == sorted(names)
        assert {"zzz", "aaa", "mmm"} <= set(names)

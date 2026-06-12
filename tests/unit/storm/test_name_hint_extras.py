"""Tests for the per-scan additive name-hint extras (ContextVar-backed).

The shipped YAML library lives at decoy_engine/storm/name_hints/v1/ and
its coverage is pinned by tests/snapshots/test_name_hints_baseline.py.
This file covers the OPT-IN per-scan additions the platform installs
via the name_hint_extras() context manager when its
storm_detector_overrides table has rows.

What this test surface guarantees:
  - Adding an extra term ADDS coverage; shipped patterns still apply.
  - The extras are scoped to the lifetime of the context manager.
  - Concurrent scans (same process) cannot see each other's extras
    -- ContextVar isolation.
  - Empty / None extras is a no-op.
  - Exceptions inside the context still clean up the ContextVar.
"""

from __future__ import annotations

import threading

import pytest

from decoy_engine.storm.detectors import (
    _NAME_HINT_EXTRAS,
    hits_name_hint,
    name_hint_extras,
)

# ── additive semantics ──────────────────────────────────────────────────


class TestAdditive:
    def test_extra_adds_coverage_for_new_header(self) -> None:
        """A header that the shipped patterns don't match becomes a
        hit when an extras entry matches it."""
        # 'practitioner_uid' is not in the shipped npi patterns
        # (npi / natl_provider / national_provider / provider_npi /
        # physician_id / provider_id). Sanity: confirm before extras.
        assert hits_name_hint("npi", "practitioner_uid") is False

        with name_hint_extras({"npi": ["practitioner_uid"]}):
            assert hits_name_hint("npi", "practitioner_uid") is True

        # Outside the context, back to shipped-only.
        assert hits_name_hint("npi", "practitioner_uid") is False

    def test_shipped_pattern_still_matches_inside_extras(self) -> None:
        """Installing extras does not displace shipped coverage."""
        # 'email' is in the shipped patterns.
        assert hits_name_hint("email", "email") is True
        with name_hint_extras({"email": ["corp_addr"]}):
            assert hits_name_hint("email", "email") is True
            assert hits_name_hint("email", "corp_addr") is True

    def test_extras_for_other_detector_dont_leak(self) -> None:
        """Adding extras for `npi` does not affect lookups for `ssn`."""
        with name_hint_extras({"npi": ["custom_npi"]}):
            assert hits_name_hint("npi", "custom_npi") is True
            # ssn unaffected -- still uses shipped only.
            assert hits_name_hint("ssn", "custom_npi") is False
            assert hits_name_hint("ssn", "ssn") is True

    def test_term_for_unknown_detector_id_is_ignored(self) -> None:
        """Extras for a detector that doesn't exist in REGISTERED_DETECTORS
        are loadable but the engine never consults them (because no
        detect_* function calls hits_name_hint for that id)."""
        # No crash; the extras dict just has an entry no one asks about.
        with name_hint_extras({"never_registered_detector": ["anything"]}):
            # Adjacent detector still works.
            assert hits_name_hint("email", "email") is True


# ── empty / no-op paths ─────────────────────────────────────────────────


class TestEmptyAndNoop:
    def test_none_extras_is_noop(self) -> None:
        with name_hint_extras(None):
            assert hits_name_hint("email", "email") is True

    def test_empty_dict_extras_is_noop(self) -> None:
        with name_hint_extras({}):
            assert hits_name_hint("email", "email") is True

    def test_empty_term_list_silently_skipped(self) -> None:
        """`{detector_id: []}` is treated as no extras for that
        detector. Doesn't crash; doesn't add coverage."""
        with name_hint_extras({"npi": []}):
            assert hits_name_hint("npi", "practitioner_uid") is False
            assert hits_name_hint("npi", "npi") is True  # shipped still works


# ── isolation ───────────────────────────────────────────────────────────


class TestIsolation:
    def test_context_restores_on_clean_exit(self) -> None:
        """ContextVar resets after the with block."""
        assert _NAME_HINT_EXTRAS.get() is None
        with name_hint_extras({"npi": ["custom_npi"]}):
            assert _NAME_HINT_EXTRAS.get() is not None
        assert _NAME_HINT_EXTRAS.get() is None

    def test_context_restores_on_exception(self) -> None:
        """ContextVar resets even when the wrapped code raises."""
        assert _NAME_HINT_EXTRAS.get() is None
        with pytest.raises(RuntimeError, match="simulated"):
            with name_hint_extras({"npi": ["custom_npi"]}):
                assert _NAME_HINT_EXTRAS.get() is not None
                raise RuntimeError("simulated")
        assert _NAME_HINT_EXTRAS.get() is None

    def test_concurrent_scans_dont_cross_contaminate(self) -> None:
        """Two threads, each running their own context, see only
        their own extras. ContextVar copies per-thread on creation
        so the assignments don't leak.
        """
        # Each thread will:
        #   - install its own extras
        #   - assert the OTHER thread's term doesn't match in this thread
        #   - assert its own term DOES match
        # Failures from inside threads bubble up via the list below.
        failures: list[str] = []
        ready = threading.Event()
        proceed = threading.Event()

        def worker_a() -> None:
            with name_hint_extras({"npi": ["a_only_term"]}):
                ready.set()
                proceed.wait(timeout=2.0)
                if hits_name_hint("npi", "a_only_term") is not True:
                    failures.append("worker_a missing own term")
                if hits_name_hint("npi", "b_only_term") is True:
                    failures.append("worker_a saw worker_b's term")

        def worker_b() -> None:
            ready.wait(timeout=2.0)
            with name_hint_extras({"npi": ["b_only_term"]}):
                proceed.set()
                if hits_name_hint("npi", "b_only_term") is not True:
                    failures.append("worker_b missing own term")
                if hits_name_hint("npi", "a_only_term") is True:
                    failures.append("worker_b saw worker_a's term")

        t_a = threading.Thread(target=worker_a)
        t_b = threading.Thread(target=worker_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=3.0)
        t_b.join(timeout=3.0)
        assert failures == [], f"isolation failure: {failures}"
        # Outer thread's view: no extras installed here.
        assert hits_name_hint("npi", "a_only_term") is False
        assert hits_name_hint("npi", "b_only_term") is False


# ── case-insensitive (same shape as shipped patterns) ───────────────────


class TestCaseInsensitive:
    def test_extras_compile_case_insensitive_via_shared_hint_helper(self) -> None:
        """The extras compile via the same _hint() helper as shipped
        patterns -- so they are case-insensitive and respect the
        same word-boundary rules."""
        with name_hint_extras({"npi": ["custom_npi"]}):
            assert hits_name_hint("npi", "custom_npi") is True
            assert hits_name_hint("npi", "CUSTOM_NPI") is True
            assert hits_name_hint("npi", "Custom_NPI") is True
            # Substring inside another token does NOT match
            # (matches the shipped _hint regex semantics).
            assert hits_name_hint("npi", "customnpiwithsuffix") is False


# ── Safe-Harbor item Q photo coverage (audit M2) ─────────────────────────


class TestPhotoColumnsHitBiometricId:
    """Audit M2 (2026-06-12): the HIPAA disguise claimed item Q
    (full-face photographs) was 'flagged by biometric_id name hints',
    but the shipped patterns carried no photo/face terms — the claim was
    false at the detector level. These cells pin the now-true claim:
    photo path/URL columns hit biometric_id, and the token-fullmatch
    semantics keep generic words from matching inside unrelated names."""

    @pytest.mark.parametrize(
        "header",
        ["patient_photo", "photo_url", "photo_path", "face_id", "facial_image", "headshot"],
    )
    def test_photo_reference_columns_match(self, header: str) -> None:
        assert hits_name_hint("biometric_id", header) is True

    @pytest.mark.parametrize("header", ["surface_area", "photography_dept", "interface_id"])
    def test_unrelated_columns_do_not_match(self, header: str) -> None:
        assert hits_name_hint("biometric_id", header) is False

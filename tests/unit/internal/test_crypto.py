"""Unit tests for decoy_engine.internal.crypto.

QA-internal-synth-providers F12 (2026-06-01, LOW security):
deterministic_hash now emits a DeprecationWarning so accidental
callers (e.g. a new masking strategy copied from an older one)
surface in CI tools that treat warnings as errors. The function
itself is still callable for backwards compat; the warning is the
deprecation signal.
"""

from __future__ import annotations

import warnings

from decoy_engine.internal.crypto import deterministic_hash, hmac_hex


class TestQaInternalF12DeterministicHashDeprecated:
    """F12: deterministic_hash emits DeprecationWarning on every
    call. The warning text names hmac_hex as the preferred replacement."""

    def test_deterministic_hash_emits_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            deterministic_hash("some-value", seed=42)
        depr_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(depr_warnings) == 1, (
            f"QA-internal F12: expected exactly one DeprecationWarning, got {len(depr_warnings)}"
        )
        assert "hmac_hex" in str(depr_warnings[0].message)

    def test_deterministic_hash_still_returns_expected_output(self):
        """Backwards compat: callers that intentionally use this path
        must still get the same hash output (no behavior change, only
        the warning is new)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out_a = deterministic_hash("value-x", seed=1)
            out_b = deterministic_hash("value-x", seed=1)
            out_c = deterministic_hash("value-x", seed=2)
        assert out_a == out_b  # same inputs -> same output
        assert out_a != out_c  # different seed -> different output
        assert isinstance(out_a, str)
        assert len(out_a) == 64  # sha256 hex digest

    def test_deterministic_hash_returns_none_on_none_input(self):
        """Backwards compat preservation: None input still returns
        None (and still emits the warning before the guard)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert deterministic_hash(None) is None

    def test_hmac_hex_does_not_emit_deprecation_warning(self):
        """Sanity check: the preferred replacement is clean."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            hmac_hex(b"key" * 11, "value-x")
        depr_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert depr_warnings == [], (
            f"QA-internal F12: hmac_hex should not emit DeprecationWarning. "
            f"Got: {[str(w.message) for w in depr_warnings]}"
        )

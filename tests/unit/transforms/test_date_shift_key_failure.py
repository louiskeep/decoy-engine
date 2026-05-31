"""Dennis H2 closure regression (QA triage 2026-06-01).

Pin the behavior that `DateShiftStrategy._column_key` RAISES when its
`derive_key` callable throws, instead of silently falling back to
seed-only MD5. Mirrors the FPE F1 fix.

Before the fix: a `derive_key` failure was logged at WARNING but the
method returned None, causing apply() to use a seed-only MD5 path for
the shift offsets. Output was no longer recoverable from the master
key + didn't match a successful re-run with proper key derivation.

After the fix: derive_key failure raises RuntimeError; the job fails
with a typed manifest error.
"""

from __future__ import annotations

import pytest

from decoy_engine.transforms.date_shift import DateShiftStrategy


def _bad_derive_key(namespace: str) -> bytes:
    """Stand-in derive_key that always raises (simulates broken master
    key infrastructure)."""
    raise RuntimeError("simulated master-key resolution failure")


class TestDateShiftKeyResolutionFailureRaises:
    """The DateShift strategy must surface a derive_key failure as an
    exception rather than silently degrading to the seed-only MD5 path."""

    def test_column_key_raises_when_derive_key_throws(self):
        strategy = DateShiftStrategy(seed=42, derive_key=_bad_derive_key)
        with pytest.raises(RuntimeError, match="(?i)DateShift column key derivation failed"):
            strategy._column_key("birthdate")

    def test_column_key_returns_none_when_derive_key_is_none(self):
        """The legacy seed-only opt-out (derive_key=None passed by the
        caller) still returns None. That's an explicit opt-out, not a
        silent degradation."""
        strategy = DateShiftStrategy(seed=42, derive_key=None)
        assert strategy._column_key("birthdate") is None

    def test_column_key_returns_bytes_when_derive_key_succeeds(self):
        """Normal path: a working derive_key returns its bytes through."""
        def good_derive_key(namespace: str) -> bytes:
            return b"x" * 32

        strategy = DateShiftStrategy(seed=42, derive_key=good_derive_key)
        result = strategy._column_key("birthdate")
        assert result == b"x" * 32

    def test_raise_message_includes_exception_type(self):
        """The raised error includes the underlying exception type for
        operator debugging."""
        strategy = DateShiftStrategy(seed=42, derive_key=_bad_derive_key)
        with pytest.raises(RuntimeError) as exc_info:
            strategy._column_key("birthdate")
        assert "RuntimeError" in str(exc_info.value)

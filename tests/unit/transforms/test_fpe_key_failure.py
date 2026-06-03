"""Dennis M2 closure regression (QA gate review 2026-05-31).

Pin the behavior that `FPEStrategy._column_key` RAISES when its
`derive_key` callable throws, instead of silently falling back to
seed-only encryption.

Before the fix: a `derive_key` failure was logged at WARNING but the
method returned None, causing `apply()` to build the encryption key
from a SHA-256 hash of ``f"fpe-legacy-{seed}-{column_name}"``. Output
was no longer recoverable from the master key + didn't match a
successful re-run. Invisible to the operator.

After the fix: derive_key failure raises RuntimeError; the job fails
with a typed manifest error.
"""

from __future__ import annotations

import pytest

from decoy_engine.transforms.fpe import FPEStrategy


def _bad_derive_key(namespace: str) -> bytes:
    """Stand-in derive_key that always raises (simulates broken master
    key infrastructure)."""
    raise RuntimeError("simulated master-key resolution failure")


class TestFPEKeyResolutionFailureRaises:
    """The FPE strategy must surface a derive_key failure as an
    exception rather than silently degrading to the seed-only path."""

    def test_column_key_raises_when_derive_key_throws(self):
        strategy = FPEStrategy(seed=42, derive_key=_bad_derive_key)
        with pytest.raises(RuntimeError, match="(?i)FPE column key derivation failed"):
            strategy._column_key("ssn")

    def test_column_key_returns_none_when_derive_key_is_none(self):
        """The legacy seed-only opt-out (derive_key=None passed by the
        caller) still returns None. That's an explicit opt-out, not a
        silent degradation."""
        strategy = FPEStrategy(seed=42, derive_key=None)
        assert strategy._column_key("ssn") is None

    def test_column_key_returns_bytes_when_derive_key_succeeds(self):
        """Normal path: a working derive_key returns its bytes through."""

        def good_derive_key(namespace: str) -> bytes:
            return b"x" * 32

        strategy = FPEStrategy(seed=42, derive_key=good_derive_key)
        result = strategy._column_key("ssn")
        assert result == b"x" * 32

    def test_raise_message_includes_exception_type(self):
        """The raised error includes the underlying exception type for
        operator debugging."""
        strategy = FPEStrategy(seed=42, derive_key=_bad_derive_key)
        with pytest.raises(RuntimeError) as exc_info:
            strategy._column_key("ssn")
        # The chained __cause__ carries the original exception.
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "simulated master-key" in str(exc_info.value.__cause__)

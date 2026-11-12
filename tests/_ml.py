"""Shared gating for tests that need the classifier stack (scikit-learn + lightgbm).

Both are base dependencies, so a normal install always has them. The one exception is a host
where lightgbm cannot load its native library: `import lightgbm` then raises OSError (macOS
without libomp, Linux without libgomp1), which `pytest.importorskip` does not catch and which
aborts collection. Every ML test gates through this module so the rule lives in one place.

DECOY_REQUIRE_ML=1, set by the ml-gate CI job, turns that skip into a failure, so a runner that
lost libgomp1 or got a broken wheel cannot report an all-skipped green (same pattern as
DECOY_REQUIRE_NER in tests/_ner.py).
"""

from __future__ import annotations

import os

import pytest


def ml_stack_error() -> str | None:
    """Why the classifier stack cannot import here, or None when it can."""
    try:
        import lightgbm  # noqa: F401
        import sklearn  # noqa: F401
    except (ImportError, OSError) as exc:
        return f"scikit-learn/lightgbm unavailable: {exc}"
    return None


def ml_required() -> bool:
    """True when the environment demands the stack (read per call so tests can set it)."""
    return os.environ.get("DECOY_REQUIRE_ML") == "1"


def ml_stack_available() -> bool:
    """For `skipif` marks. Under DECOY_REQUIRE_ML an unusable stack fails instead of skipping."""
    reason = ml_stack_error()
    if reason is not None and ml_required():
        pytest.fail(f"DECOY_REQUIRE_ML=1 but {reason}", pytrace=False)
    return reason is None


def require_ml_stack() -> None:
    """Skip the calling test, or the whole module when called at module level."""
    reason = ml_stack_error()
    if reason is None:
        return
    if ml_required():
        pytest.fail(f"DECOY_REQUIRE_ML=1 but {reason}", pytrace=False)
    pytest.skip(reason, allow_module_level=True)

"""Shared gating for tests that need the classifier stack (scikit-learn + lightgbm).

Both are base dependencies, so a normal install always has them. The one exception is a host
where lightgbm cannot load its native library: `import lightgbm` then raises OSError (macOS
without libomp, Linux without libgomp1), which `pytest.importorskip` does not catch and which
aborts collection. Every ML test gates through this module so the rule lives in one place.
"""

from __future__ import annotations

import pytest


def ml_stack_error() -> str | None:
    """Why the classifier stack cannot import here, or None when it can."""
    try:
        import lightgbm  # noqa: F401
        import sklearn  # noqa: F401
    except (ImportError, OSError) as exc:
        return f"scikit-learn/lightgbm unavailable: {exc}"
    return None


def ml_stack_available() -> bool:
    return ml_stack_error() is None


def require_ml_stack() -> None:
    """Skip the calling test, or the whole module when called at module level."""
    reason = ml_stack_error()
    if reason is not None:
        pytest.skip(reason, allow_module_level=True)

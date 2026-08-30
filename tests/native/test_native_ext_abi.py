"""Companion build/packaging scaffold (Task 2.1, build-system only).

Tests the compiled MODULE directly, not the loader: `load_compiled_crypto_kernel`
stays the Phase 0 always-raising stub until a later slice wires it to the real
load + ABI check. Two states, proven in whichever one the running environment
is actually in:

- companion-present: `decoy_engine_native._kernel` imports and reports the
  pinned ABI tag. Skipped when the companion is not installed; the
  companion-present CI job installs it and runs this case.
- companion-absent: the core imports Rust-free and `decoy_engine_native` is
  not importable. Skipped when the companion IS installed (that guard belongs
  to the companion-absent CI job); this is the default state in a plain
  `.[dev]` install, so it runs there today.

The two are mutually exclusive in one venv by construction, so exactly one
runs per environment; both are demonstrated locally by installing then
uninstalling the companion (see the Task 2.1 report for the exact commands).
"""

from __future__ import annotations

import importlib.util

import pytest

PINNED_ABI_TAG = "decoy-native-abi-1"

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None


@pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)
def test_companion_kernel_reports_pinned_abi_tag() -> None:
    import decoy_engine_native._kernel as kernel

    assert kernel.abi_version() == PINNED_ABI_TAG


@pytest.mark.skipif(
    _COMPANION_PRESENT,
    reason="companion is installed in this environment; the companion-absent CI job covers this",
)
def test_core_imports_rust_free_without_companion() -> None:
    import decoy_engine  # noqa: F401 -- import itself is the assertion (no hard dependency)

    with pytest.raises(ImportError):
        import decoy_engine_native  # noqa: F401

"""Companion-present gate for the real-kernel parity suites (test-side only).

A real-kernel suite proves the compiled companion's output; it may skip only when there is
nothing to prove, that is when no companion is installed at all. A companion that IS installed
but does not load (import failure, ABI tag other than the engine's, failed known-answer
self-test) is the exact failure mode such a suite exists to catch, so it must FAIL loudly and
never skip: a skip there would turn a broken or stale binary into a green run.

`companion_state()` separates the two: installed means `importlib.util.find_spec` finds the
`decoy_engine_native` distribution; loadable means `load_compiled_crypto_kernel()` returns.
`require_companion()` is the fixture body: skip on absent, fail on present-but-broken.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec

import pytest

from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)

DISTRIBUTION = "decoy_engine_native"


@dataclass(frozen=True)
class CompanionState:
    installed: bool
    error: str | None = None

    @property
    def absent(self) -> bool:
        return not self.installed

    @property
    def broken(self) -> bool:
        return self.installed and self.error is not None

    @property
    def ready(self) -> bool:
        return self.installed and self.error is None


def companion_state() -> CompanionState:
    """Absent (nothing installed), broken (installed, does not load) or ready."""
    try:
        installed = find_spec(DISTRIBUTION) is not None
    except (ImportError, ValueError):
        installed = True  # something is there and it is malformed: present-but-broken
    if not installed:
        return CompanionState(installed=False)
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError as exc:
        return CompanionState(installed=True, error=str(exc))
    return CompanionState(installed=True)


def require_companion() -> CompanionState:
    """Skip when no companion is installed; FAIL when one is installed but does not load."""
    state = companion_state()
    if state.absent:
        pytest.skip(f"{DISTRIBUTION} is not installed: no compiled kernel to prove")
    if state.broken:
        pytest.fail(
            f"{DISTRIBUTION} is installed but the compiled kernel does not load; the "
            f"real-kernel suite fails rather than skips so a stale or broken companion cannot "
            f"pass vacuously: {state.error}"
        )
    return state

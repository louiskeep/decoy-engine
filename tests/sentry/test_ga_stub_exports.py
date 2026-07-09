"""Sentry test: public stub exports cannot silently ship into GA.

consultant-2026-07-09 F4: `SchemaInspector` and `LicenseVerifier` are
top-level public exports (`decoy_engine.__all__`) that are intentionally
fake pre-GA (`NotImplementedError` / always-free-tier). That's fine pre-GA,
CLI/platform code needs the symbols importable, but nothing currently stops
`RELEASE_PHASE` flipping to `"ga"` (`decoy_engine.release`) while these stay
exported, which would ship silently-fake behavior on the public surface. See
docs/engine-consultant-findings-2026-07-09.md.

This sentry is the release gate: pre-GA it just pins the current stub
registry; the moment GA is flipped, it fails unless every entry below has
been resolved (implemented, moved to an experimental namespace, or dropped
from `decoy_engine.__all__`) and removed from `STUB_EXPORTS`.
"""

from __future__ import annotations

import decoy_engine
from decoy_engine.release import is_pre_ga

# name -> why it's still a stub / where the real implementation is tracked.
# Resolve the export, THEN delete the entry here. Do not delete the entry
# to make this test pass without actually resolving the stub.
STUB_EXPORTS: dict[str, str] = {
    "SchemaInspector": (
        "raises NotImplementedError; connector schema introspection, "
        "planned Phase 2 per SHARED_ENGINE_ARCHITECTURE.md"
    ),
    "LicenseVerifier": (
        "verify() always returns a free-tier license; real JWT verification "
        "replaces this before paid-tier launch"
    ),
}


def test_stub_exports_are_still_exported_pre_ga() -> None:
    for name in STUB_EXPORTS:
        assert name in decoy_engine.__all__, (
            f"{name!r} is in STUB_EXPORTS but no longer in decoy_engine.__all__; "
            "if it was resolved, drop it from STUB_EXPORTS instead."
        )


def test_no_unresolved_public_stubs_at_ga() -> None:
    if is_pre_ga():
        return
    assert not STUB_EXPORTS, (
        "decoy_engine.RELEASE_PHASE is 'ga' but STUB_EXPORTS still lists "
        f"unresolved public stubs: {sorted(STUB_EXPORTS)}. Implement, move "
        "behind an experimental namespace, or remove from "
        "decoy_engine.__all__ before shipping GA."
    )

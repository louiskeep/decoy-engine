"""Sentry test: public stub exports cannot silently ship into GA.

consultant-2026-07-09 F4: `SchemaInspector` and `LicenseVerifier` are
top-level public exports (`decoy_engine.__all__`) that are intentionally
fake pre-GA (`NotImplementedError` / always-free-tier). That's fine pre-GA,
CLI/platform code needs the symbols importable, but nothing currently stops
`RELEASE_PHASE` flipping to `"ga"` (`decoy_engine.release`) while these stay
exported, which would ship silently-fake behavior on the public surface. See
docs/engine-consultant-findings-2026-07-09.md.

dennis review caught that a dict-driven gate (`STUB_EXPORTS`) is gameable:
emptying the dict satisfies the assertion without touching the actual stub
behavior. So the GA-phase check below does not read `STUB_EXPORTS` at all;
it directly re-derives each stub's exact current fake behavior and asserts
it is gone by GA. The only way to pass at GA is to actually change the
behavior. `STUB_EXPORTS` still exists as a pre-GA documentation/registry
(kept in sync with `decoy_engine.__all__` by the first test below) but is
not load-bearing for the release gate itself.
"""

from __future__ import annotations

import decoy_engine
from decoy_engine.license import LicenseVerifier
from decoy_engine.release import is_pre_ga
from decoy_engine.schema import SchemaInspector

# name -> why it's still a stub / where the real implementation is tracked.
# Purely documentation pre-GA; the GA gate below does not consult this dict,
# so emptying it does not weaken the gate. Resolve the export, THEN delete
# the entry here to keep the registry accurate.
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

    still_stub: list[str] = []

    try:
        SchemaInspector()
    except NotImplementedError:
        still_stub.append("SchemaInspector still raises NotImplementedError")

    result = LicenseVerifier.verify()
    if result == {"tier": "free", "features": [], "expires_at": None}:
        still_stub.append(
            "LicenseVerifier.verify() still returns the exact hardcoded stub response"
        )

    assert not still_stub, (
        "decoy_engine.RELEASE_PHASE is 'ga' but the following public exports "
        f"still exhibit their pre-GA stub behavior: {still_stub}. Implement, "
        "move behind an experimental namespace, or remove from "
        "decoy_engine.__all__ before shipping GA."
    )

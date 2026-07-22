"""Shared helper for tests written against the pre-DPS-Scope-B
`generate_tables(config_dict)` signature.

`generate_tables` is Plan-only now (guide section 4.8, defect F3): every
caller compiles a `Plan` first. Tests exercising generator/transform
behavior in isolation don't otherwise need a real `Profile` (they have no
masking columns, relationships, or FK structure to profile), so this
wraps `compile_plan` with an empty one and forwards straight through to
`generate_tables(plan, ...)`, keeping call sites elsewhere in the suite a
near-mechanical one-line change instead of hand-rolling the same
boilerplate in a dozen files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from decoy_engine.profile import Profile


def empty_profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )


def compile_and_generate(
    config: dict[str, Any],
    *,
    profile: Profile | None = None,
    derive_key: bytes | None = None,
    instance_default_locale: str | None = None,
) -> dict[str, Any]:
    """`compile_plan` + `generate_tables`, forwarding the same kwargs
    `generate_tables(config, derive_key=..., instance_default_locale=...)`
    used to accept directly."""
    plan = compile_plan(config, profile or empty_profile(), decoy_engine_version="test")
    return generate_tables(
        plan, derive_key=derive_key, instance_default_locale=instance_default_locale
    )

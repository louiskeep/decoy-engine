"""``statistical`` generate-column dispatch, split out of ``synthesize.py``.

Module-size decomposition (tests/sentry/test_module_size.py ALLOWLIST):
``synthesize.py``'s own docstring has flagged the `statistical`/`derived`/
`formula` per-type dispatch branches as the standing decomposition target
since DPS Scope B (2026-07-22); this module carries the `statistical`
branch out, mirroring the existing `transforms/derived_aggregate.py` split
the same allowlist entry names as precedent.

Behavior is unchanged: this is a pure code move, not a rewrite.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.generation.statistical import StatisticalSpec
from decoy_engine.generators.derivation import GenDeriveContext


def statistical_generate(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any,
    generated: dict[str, list[Any]],
    table_name: str,
    statistical_specs: dict[tuple[str, str], StatisticalSpec],
) -> list[Any]:
    """WS3 statistical synthesis: sample from a distribution-snapshot/v1
    artifact (see generation/statistical for the methodology + privacy
    gate). ADDITIVE generator type -- the existing types stay
    parity-frozen to V1. `generated` carries the table's already-built
    columns so `condition_on` can read its conditioning sibling
    (declared-order sequential conditional sampling).

    DPS Scope B (guide section 4.8): the spec comes from the Plan's
    already-validated, already-pinned ``statistical_specs`` mapping, keyed
    by ``(table_name, column_name)`` -- this function never opens a
    snapshot path itself. The mapping is built once by ``generate_tables``
    from ``GenerationPlan.statistical_specs``, which `compile_plan` froze
    from the exact bytes it read at compile time (guide section 4.7),
    closing the TOCTOU window a raw ``load_spec(col)`` call would reopen.
    """
    from decoy_engine.generation.statistical import sample_column
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    col_name = str(col.get("name"))
    spec = statistical_specs.get((table_name, col_name))
    if spec is None:
        raise StatisticalSpecError(
            code="statistical_spec_not_pinned",
            message=(
                f"statistical column {col_name!r} in table {table_name!r} has no pinned "
                "spec in this Plan's GenerationPlan. This should be unreachable through "
                "compile_plan -- every type: statistical column that compiles "
                "successfully is pinned."
            ),
        )
    parent_values: list[Any] | None = None
    if spec.condition_on is not None:
        parent_values = generated.get(spec.condition_on)
        if parent_values is None:
            raise StatisticalSpecError(
                code="statistical_condition_column_unavailable",
                message=(
                    f"statistical column {spec.column!r} conditions on "
                    f"{spec.condition_on!r}, which is not generated yet. Declare "
                    f"{spec.condition_on!r} BEFORE {spec.column!r} in generate_columns."
                ),
            )
    # Reuse the Plan's already-pinned digest (guide section 4.7/4.8, defect
    # F4) instead of letting the fingerprint step reopen snapshot_file.
    digest = f"sha256:{spec.snapshot_digest}" if spec.snapshot_digest else None
    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key,
        column_config=col,
        fallback_seed=seed,
        snapshot_content_digest=digest,
    ).base_int("np")
    return sample_column(spec, n, col_seed=col_seed, parent_values=parent_values)

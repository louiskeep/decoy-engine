"""Chunked-entry helpers split out of `_chunked_entry` to hold its 700-LOC ceiling.

`resolve_admitted_pools` resolves every faker column's pool before a route is committed;
`prepared_categoricals` prepares the native-admissible categorical columns once per run.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.execution.native._categorical_prepared import (
    PreparedCategorical,
    prepare_chunked_categoricals,
)
from decoy_engine.execution.native._chunk_masking import (
    _resolve_faker_pools,
    pool_values_are_strings,
)
from decoy_engine.execution.native._dispatch import NativeRouteEvidence, _downgrade_to_oracle
from decoy_engine.execution.native._route_decline_codes import RouteDeclineCode, column_decline
from decoy_engine.generation.pool import ValuePool


def resolve_admitted_pools(
    state: Any, *, table: str, decision: NativeRouteEvidence
) -> tuple[NativeRouteEvidence, dict[str, ValuePool]]:
    """Resolve every faker column's pool once, from the caller's registry, before
    the route is committed; the native loop then uses these same pools.

    A provider whose non-null output is not string-compatible cannot go through
    the string pool the native sampler gathers from, so the table reroutes to the
    oracle with a coded reason instead of failing after admission. The pools stay
    in `state.pool_cache`, so the oracle route's own warm-up is a cache hit.
    """
    plan = state.plan
    table_seed = next((ts for (name, ts) in plan.seed_envelope.per_table if name == table), None)
    if table_seed is None:  # pragma: no cover - admission implies a seed envelope
        raise AssertionError(
            f"native route admitted {table!r} but the compiled plan has no seed envelope for it."
        )
    col_seed_by_name = dict(table_seed.per_column)
    pools = _resolve_faker_pools(
        col_seed_by_name,
        job_seed=plan.seed_envelope.job_seed,
        pool_cache=state.pool_cache,
        registry=state.registry,
    )
    for column, pool in pools.items():
        if not pool_values_are_strings(pool):
            provider = col_seed_by_name[column].provider
            return (
                _downgrade_to_oracle(
                    decision,
                    f"faker_provider_output_not_string:{column}:{provider}",
                    column_decline(
                        RouteDeclineCode.FAKER_PROVIDER_OUTPUT_NOT_STRING, table, column
                    ),
                ),
                {},
            )
    return decision, pools


def prepared_categoricals(state: Any, *, table: str) -> dict[str, PreparedCategorical]:
    """The native-admissible categorical columns of `table`, prepared once for the run.

    Config-admissible AND a string first-chunk source, whichever route the table takes:
    the output-type pin and the native chunk call read this one result."""
    table_seed = next(
        (ts for (name, ts) in state.plan.seed_envelope.per_table if name == table), None
    )
    if table_seed is None:  # pragma: no cover - a validated mask table always has a seed envelope
        return {}
    return prepare_chunked_categoricals(dict(table_seed.per_column), state.first.schema)

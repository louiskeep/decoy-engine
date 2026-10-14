"""Schema-aware retention signal for `decide_execution_route`.

The out-of-core FK joiner fixes one output Arrow type per FK component before it
sees data (`out_of_core/_batch_join._resolve_output_types`) and rejects, with
`out_of_core_fk_key_dtype_unsupported`, every key type whose Python round trip
is value-dependent (decimal, uint64, zone-aware timestamps, date64, time,
duration, fixed-size binary, dictionary) and every candidate mix it cannot pin
(string with binary, ...). The pandas oracle and the sequential route complete
all of those. `out_of_core_compatible` inspects the plan and graph, never the
source schemas, so it admits such a job and the reject surfaces only at run
time.

When the router prefers a bounded route for a job that fits full-frame, that
job must not turn from an oracle success into an out-of-core reject. This module
answers the one question the router needs: would out-of-core type this job's FK
components without rejecting? It dry-runs the joiner's own typing function over
the masked parent-key types (derived from the plan's strategies and the source
schemas) and the child key types, so the verdict keys on masked output types and
the orphan policy, not on source dtypes alone. An int64/float64 mix is flagged
too: its fixed type is float64, but whether a run's integers survive that cast
depends on the values, so it cannot be predicted from a schema.

Conservative by construction: an unknown schema, a missing seed or an
untypeable strategy counts as a hazard, which only ever keeps a job off
out-of-core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource
    from decoy_engine.relationships import RelationshipGraph

__all__ = ["fk_out_of_core_dtype_hazard"]


def _schema_of(source: pa.Table | LazySource | None) -> pa.Schema | None:
    return None if source is None else source.schema


def fk_out_of_core_dtype_hazard(
    plan: Plan,
    graph: RelationshipGraph,
    *,
    caller_sources: dict[str, pa.Table | LazySource],
) -> bool:
    """True when out-of-core could reject (or cannot pin the type of) a relationship
    key of this job although the oracle completes it."""
    from decoy_engine.execution.out_of_core._batch_join import (
        _python_roundtrip_type,
        _resolve_output_types,
    )
    from decoy_engine.execution.out_of_core._compat import _column_seed
    from decoy_engine.execution.out_of_core._mask import masked_output_type
    from decoy_engine.relationships._graph import OrphanPolicy

    for edge in graph.edges:
        parent_schema = _schema_of(caller_sources.get(edge.parent_table))
        child_schema = _schema_of(caller_sources.get(edge.child_table))
        if parent_schema is None or child_schema is None:
            return True
        try:
            parent_types = tuple(parent_schema.field(c).type for c in edge.parent_columns)
            child_types = tuple(child_schema.field(c).type for c in edge.child_columns)
        except KeyError:
            return True
        seeds = tuple(_column_seed(plan, edge.parent_table, c) for c in edge.parent_columns)
        if edge.orphan_policy is OrphanPolicy.REMAP and any(s is None for s in seeds):
            return True
        try:
            masked = tuple(
                src if seed is None else masked_output_type(seed, src)
                for seed, src in zip(seeds, parent_types, strict=True)
            )
            resolved = _resolve_output_types(
                edge=edge,
                masked_types=masked,
                child_key_types=child_types,
                remap_seeds=(
                    tuple(s for s in seeds if s is not None)
                    if edge.orphan_policy is OrphanPolicy.REMAP
                    else None
                ),
            )
            matched = tuple(_python_roundtrip_type(t) for t in masked)
        except ExecutionError:
            return True
        # A fixed type that an orphan or remapped candidate changed (bool with int64,
        # int64 with float64), or a float type fed by integer keys, is exact only while
        # the run's values happen to fit it. That depends on the data, not the schema.
        if resolved != matched or (
            any(pa.types.is_floating(t) for t in resolved)
            and any(pa.types.is_integer(t) for t in parent_types + child_types)
        ):
            return True
    return False

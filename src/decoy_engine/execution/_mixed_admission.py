"""Semantic admission core for a MIXED (generate + mask) job.

Two callers share these checks: the production mixed route
(`_mixed_static` / `_pipeline_mixed_route`) and the shadow coordinator's mixed
dispatch (`execution/physical/_shadow_mixed.py`). They live here, at the
`execution` parent level, so production never imports `execution.physical`
(the import-disconnection sentry) and the shadow depends on the shared rule
instead of owning a private copy that could drift from the route that ships.

What is SEMANTIC (shared, here):

  * edge topology: zero generate-parent -> mask-child edges (independent
    mixed) or exactly ONE (coupled mixed); more than one declines;
  * the coupled edge is single-column on both sides and its child key is an
    int/string Arrow type; the parent key type is checked later, against the
    materialized generate output (`require_admitted_parent_key_type`);
  * complete-graph restriction: no OTHER relationship edge may touch a mask
    table on either side;
  * an `out_of_core` mask driver is an unconditional decline (a mixed job never
    legitimately reaches the out-of-core route);
  * every generate output must survive the oracle's pandas echo byte-for-byte
    (`require_roundtrip_stable_generate_outputs`): no nulls, no floating NaN,
    no nested types.

What is NOT here, on purpose: the shadow-only inertness guards (a requested
sink, source loader, vault writer, fidelity report, quarantine, validators,
`mask_secret_ref`) stay in the shadow wrapper. They exist because the shadow
coordinator structurally cannot publish or lazily load; the production route
publishes and may stream, and enforces its own job-setting restrictions in
`_mixed_static`. Moving them here would silently narrow production, and
dropping them from the shadow would silently widen it.

The checks raise `MixedDeclineError` (a plain exception carrying a stable code and a
privacy-safe detail); each caller maps it to its own failure type. Codes equal
the shadow catalog's values so the mapping is the identity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.relationships._graph import RelationshipEdge, RelationshipGraph

__all__ = [
    "CHUNK_INVARIANT_WARNING_CODES",
    "CHUNK_VARIANT_WARNING_CODES",
    "GENERATION_SHAPE_UNSUPPORTED",
    "MIXED_DRIVER_UNSUPPORTED",
    "MIXED_FK_CROSS_GENERATE_UNSUPPORTED",
    "MIXED_FK_TOPOLOGY_UNSUPPORTED",
    "MixedDeclineError",
    "chunk_unstable_warning_column",
    "is_admitted_fk_key_type",
    "require_admitted_parent_key_type",
    "require_no_out_of_core_driver",
    "require_roundtrip_stable_generate_outputs",
    "select_admitted_coupled_edge",
]

# Values mirror `physical/_shadow_diff_codes.py`; a test pins the equality.
GENERATION_SHAPE_UNSUPPORTED = "generation-shape-unsupported"
MIXED_DRIVER_UNSUPPORTED = "mixed-driver-unsupported"
MIXED_FK_CROSS_GENERATE_UNSUPPORTED = "mixed-fk-cross-generate-unsupported"
MIXED_FK_TOPOLOGY_UNSUPPORTED = "mixed-fk-topology-unsupported"


# Every warning code a mask strategy can declare, classified by whether the chunk-by-chunk run
# reproduces the full-frame warning. A chunk-invariant code is emitted identically by every chunk,
# so the chunked merge's equality dedup leaves exactly the oracle's single warning. The others
# carry a per-frame count or row index, so each chunk emits a chunk-local copy and the merge keeps
# several partial warnings (with the wrong denominators and order) where the oracle emits one.
# A test requires every declared code to appear in exactly one set, so a newly declared code must
# be classified here before it can be admitted.
#
# Deferred: correct cross-chunk warning aggregation (denominators, ordering, partial chunks) to
# re-admit the count and row-index strategies to the mixed route. v1 declines them.
CHUNK_INVARIANT_WARNING_CODES: frozenset[str] = frozenset({"fpe_join_group_active"})
CHUNK_VARIANT_WARNING_CODES: frozenset[str] = frozenset(
    {
        "fpe_partial_plaintext_disclosure",
        "text_mask_sub_floor_span_handled",
        "top_code_generalized",
        "geo_generalize_cascade",
        "nested_cell_json_parse_error",
        "nested_jsonpath_path_overlap",
    }
)


def chunk_unstable_warning_column(columns: Iterable[Any]) -> tuple[str, str] | None:
    """`(column, strategy)` of the first mask column whose warnings are not chunk-invariant.

    A column is admitted only when every warning code its strategy declares
    (`capabilities_for(...).warning_codes`) is in `CHUNK_INVARIANT_WARNING_CODES`. A code that is
    not there, classified variant or not classified at all, declines, and so does a strategy with
    no capability entry: anything that cannot be shown chunk-invariant fails closed.
    """
    from decoy_engine.execution.native._capabilities import capabilities_for

    for c in columns:
        if not isinstance(c, dict):
            continue
        strategy = str(c.get("strategy") or "passthrough")
        try:
            codes = capabilities_for(strategy).warning_codes
        except KeyError:
            return str(c.get("name")), strategy
        if any(code not in CHUNK_INVARIANT_WARNING_CODES for code in codes):
            return str(c.get("name")), strategy
    return None


class MixedDeclineError(DecoyError):
    """A coded, privacy-safe decline of the mixed shape. `detail` names a
    location and a type only, never a value."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def is_admitted_fk_key_type(arrow_type: pa.DataType | None) -> bool:
    """A single-column int or string key; composite keys and every other scalar
    type (float, decimal, bool, timestamp, ...) stay declined."""
    return arrow_type is not None and (
        pa.types.is_integer(arrow_type)
        or pa.types.is_string(arrow_type)
        or pa.types.is_large_string(arrow_type)
    )


def require_no_out_of_core_driver(*, out_of_core_present: bool) -> None:
    """An `out_of_core` mask driver can never legitimately reach a mixed job:
    production never routes mixed to it, so it is a total guard, not a
    deferred capability."""
    if out_of_core_present:
        raise MixedDeclineError(
            MIXED_DRIVER_UNSUPPORTED, "out_of_core mask driver in a mixed dispatch"
        )


def select_admitted_coupled_edge(
    graph: RelationshipGraph,
    generate_table_names: frozenset[str],
    mask_table_names: frozenset[str],
    child_key_type: Callable[[str, str], pa.DataType | None],
) -> RelationshipEdge | None:
    """The AT MOST ONE `generate-parent -> mask-child` edge the mask half may
    couple its FK resolution to. `None` is the independent case. The reverse
    direction (a mask parent referenced by a generate child) is rejected at
    generation-config validation, so only this direction is checked here.

    `child_key_type(table, column)` returns the child key's Arrow type from
    whatever the caller holds (a resident snapshot, a lazy footer), or `None`
    when it is unknown."""
    try:
        edges = graph.edges
    except AttributeError as exc:
        raise MixedDeclineError(
            GENERATION_SHAPE_UNSUPPORTED, "relationship graph has no edges attribute"
        ) from exc

    crossing: list[RelationshipEdge] = []
    for candidate in edges:
        try:
            parent_table = candidate.parent_table
            child_table = candidate.child_table
        except AttributeError as exc:
            raise MixedDeclineError(
                GENERATION_SHAPE_UNSUPPORTED, "a relationship edge is malformed"
            ) from exc
        try:
            crosses = parent_table in generate_table_names and child_table in mask_table_names
        except TypeError as exc:
            raise MixedDeclineError(
                GENERATION_SHAPE_UNSUPPORTED, "a relationship edge table name is unhashable"
            ) from exc
        if crosses:
            crossing.append(candidate)

    if not crossing:
        return None
    if len(crossing) > 1:
        raise MixedDeclineError(
            MIXED_FK_TOPOLOGY_UNSUPPORTED,
            f"{len(crossing)} generate-parent -> mask-child edges present",
        )
    edge = crossing[0]
    if len(edge.parent_columns) != 1 or len(edge.child_columns) != 1:
        raise MixedDeclineError(
            MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
            f"parent={edge.parent_table!r} child={edge.child_table!r}: "
            "composite FK key not admitted",
        )
    key_type = child_key_type(edge.child_table, edge.child_columns[0])
    if not is_admitted_fk_key_type(key_type):
        raise MixedDeclineError(
            MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
            f"child={edge.child_table!r}.{edge.child_columns[0]!r}: "
            f"key type {key_type} not admitted",
        )
    # No OTHER edge may touch a mask table. The admitted edge's child is always a
    # mask table, so this one rule subsumes "no other incoming edge to the child",
    # "no outgoing edge from the child or the generated parent reaching a mask
    # table" and "no FK edge involving any other mask table".
    for other in edges:
        if other is edge:
            continue
        if other.parent_table in mask_table_names or other.child_table in mask_table_names:
            raise MixedDeclineError(
                MIXED_FK_TOPOLOGY_UNSUPPORTED, "another relationship edge touches a mask table"
            )
    return edge


def require_admitted_parent_key_type(
    edge: RelationshipEdge, generate_outputs: dict[str, pa.Table]
) -> None:
    """The parent half of the key-type rule, deferred until generation ran: a
    generate table's actual column type is only knowable against its output."""
    parent_table = generate_outputs.get(edge.parent_table)
    column_name = edge.parent_columns[0]
    key_type = (
        parent_table.schema.field(column_name).type
        if parent_table is not None and column_name in parent_table.schema.names
        else None
    )
    if not is_admitted_fk_key_type(key_type):
        raise MixedDeclineError(
            MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
            f"parent={edge.parent_table!r}.{column_name!r}: key type {key_type} not admitted",
        )


def require_roundtrip_stable_generate_outputs(generate_outputs: dict[str, pa.Table]) -> None:
    """Decline a mixed job whose GENERATED output is not byte-stable through the
    oracle's pandas echo.

    In a mixed job the oracle merges `generate_outputs` into its mask sources,
    the pandas adapter round-trips every source frame through
    `to_pandas`/`from_pandas`, and the stitch lets that echoed copy WIN the name
    tie. A route that keeps the raw Arrow generate output therefore diverges
    for three column shapes:

      * any null (`null_count > 0`): an integer column widens to double and null
        slots refill differently;
      * a floating NaN (even with `null_count == 0`): `from_pandas` folds a NaN
        back to an Arrow null on re-import;
      * a nested type (`list`/`struct`/`map`/union): element types widen through
        pandas the same way a top-level numeric does.

    The check reads the MATERIALIZED tables rather than the config knobs, so it
    catches instability from any source (`null_probability`, a `None`/NaN in
    `categories`, a nested category) in one place."""
    for table_name, table in generate_outputs.items():
        # A zero-row table has no values to infer a type from: the pandas echo gives every column
        # `null`, while the generation core declares `string`. Checked per TABLE, not only for the
        # coupled parent, so an independent or lookup zero-row table also falls back to the
        # full-frame continuation and publishes what the oracle does.
        if table.num_rows == 0:
            raise MixedDeclineError(
                GENERATION_SHAPE_UNSUPPORTED,
                f"table={table_name!r}: a zero-row generate output has no values to type, so its "
                "schema is not stable through the oracle's pandas echo in a mixed job (deferred)",
            )
        for column_name in table.column_names:
            column = table.column(column_name)
            field_type = column.type
            if _is_nested_arrow_type(field_type):
                raise MixedDeclineError(
                    GENERATION_SHAPE_UNSUPPORTED,
                    f"table={table_name!r} column={column_name!r}: nested generate output "
                    f"type {field_type} is not round-trip-stable through the oracle's pandas "
                    "echo in a mixed job (deferred)",
                )
            if column.null_count > 0:
                raise MixedDeclineError(
                    GENERATION_SHAPE_UNSUPPORTED,
                    f"table={table_name!r} column={column_name!r}: a nullable generate output "
                    "is not round-trip-stable through the oracle's pandas echo in a mixed job "
                    "(deferred)",
                )
            if pa.types.is_floating(field_type) and _float_column_has_nan(column):
                raise MixedDeclineError(
                    GENERATION_SHAPE_UNSUPPORTED,
                    f"table={table_name!r} column={column_name!r}: a floating generate output "
                    "carries NaN, which the oracle's pandas echo folds to null (not "
                    "round-trip-stable in a mixed job; deferred)",
                )


def _is_nested_arrow_type(field_type: pa.DataType) -> bool:
    return (
        pa.types.is_list(field_type)
        or pa.types.is_large_list(field_type)
        or pa.types.is_fixed_size_list(field_type)
        or pa.types.is_struct(field_type)
        or pa.types.is_map(field_type)
        or pa.types.is_union(field_type)
    )


def _float_column_has_nan(column: pa.ChunkedArray) -> bool:
    # A NaN is distinct from a null in Arrow, so `null_count` misses it. `pc.is_nan`
    # is defined only on floating input (guarded by the caller) and returns null for
    # null slots; `skip_nulls=True` treats those as "no NaN here".
    return bool(
        pc.any(pc.is_nan(column), skip_nulls=True).as_py()  # type: ignore[attr-defined, unused-ignore]
    )

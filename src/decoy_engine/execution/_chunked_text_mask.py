"""text_mask `when` gate for chunked execution (Phase 4 slice 3).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_dgrn.py` / `_chunked_group_key.py`. See `_chunked.py`'s module
docstring for the text_mask CHUNK_SAFE admission and
`docs/plans/2026-09-01-p4-slice3-text-mask-chunked.md` for the full design.

Unlike `windowed_date` (position-keyed) and `group_key` (sibling-keyed),
`text_mask` is keyed on its own column's value: it joins `CHUNK_SAFE_
STRATEGIES` directly (`_chunked_fk.py`) and needs no separate admitted set
or FK-exclusion here. The one thing this slice still rejects at compile
time is `text_mask` + `when:` (Trap E): the handler `str()`-converts every
non-null non-string cell (`_text_mask.py`), so a text_mask source can be
numeric. A when-gated column would leave non-matching rows at their
original (possibly numeric) dtype while matching rows become masked
strings, so a chunk of all-non-matching rows keeps the source dtype while
a chunk with matches turns to object -- a chunk-boundary-dependent output
dtype, the same hazard `reject_group_key_when` closes. The auto planner
already blanket-rejects `when` for auto-routing; this is the manual
`run_mask_pipeline_chunked` entry's own gate.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.plan._errors import PlanCompileError

# text_mask's domain is text: a STRING / LARGE_STRING source ingests to a
# chunk-stable pandas dtype. A non-string source does not -- an int-with-nulls
# column widens to float64 on a null-bearing chunk but stays int64 on a
# null-free one, and the handler `str()`-converts every cell, so the SAME value
# yields "1" vs "1.0" depending purely on which other rows share its chunk (and
# on the whole-column presence of nulls, which the manual entry cannot see).
# That breaks byte-parity and FK RI. Only these types are provably chunk-stable.
_SAFE_TEXT_MASK_SOURCE_CHECKS = (pa.types.is_string, pa.types.is_large_string)


def _text_mask_node_columns(ordered_work: list[Any], table: str) -> list[str]:
    return [
        node.columns[0]
        for node in ordered_work
        if node.table == table and node.kind == "scalar" and node.strategy == "text_mask"
    ]


def _unsafe_cols_in_schema(text_mask_cols: list[str], schema: pa.Schema) -> list[str]:
    """Of `text_mask_cols`, those NOT provably chunk-stable string in `schema`
    (a non-string type, or absent)."""
    offending: list[str] = []
    for name in text_mask_cols:
        idx = schema.get_field_index(name)
        if idx < 0 or not any(
            check(schema.field(idx).type) for check in _SAFE_TEXT_MASK_SOURCE_CHECKS
        ):
            offending.append(name)
    return sorted(offending)


def unsafe_text_mask_source_columns(
    ordered_work: list[Any], source_schema: pa.Schema, *, table: str
) -> list[str]:
    """text_mask column names on `table` whose SOURCE Arrow type is not a
    chunk-stable string type (Trap: int-with-nulls widening). Empty when every
    text_mask column has a string / large_string source.

    The auto route's collector: `_planner._runtime_source_rejections` calls this
    directly (it already has an ordered work list and the whole-source schema);
    the manual entry validates PER CHUNK instead (see `text_mask_source_columns`
    + `reject_unsafe_text_mask_chunk_schema`), because it takes an arbitrary
    chunk iterable whose dtype could drift across chunks.
    """
    return _unsafe_cols_in_schema(_text_mask_node_columns(ordered_work, table), source_schema)


def text_mask_source_columns(
    plan: Any, registry: Any, relationship_graph: Any, *, table: str
) -> list[str]:
    """The text_mask column names on `table`, resolved once from the plan's work
    list, so the manual entry can validate every chunk's schema against them
    without rebuilding the work list per chunk."""
    from decoy_engine.execution._runner import build_work_list, order_work

    ordered_work = order_work(build_work_list(plan, registry), relationship_graph)
    return _text_mask_node_columns(ordered_work, table)


def reject_unsafe_text_mask_chunk_schema(
    schema: pa.Schema, text_mask_cols: list[str], *, table: str
) -> None:
    """Reject if any of `text_mask_cols` is not a chunk-stable string type in
    `schema`. Called on the FIRST chunk (admission) AND on EVERY subsequent
    chunk of the manual `run_mask_pipeline_chunked` iterable, since a caller can
    feed a string first chunk and a divergent (e.g. int) later chunk.

    Raises:
        PlanCompileError: ``code='chunked_text_mask_source_dtype_unsupported'``.
    """
    offending = _unsafe_cols_in_schema(text_mask_cols, schema)
    if not offending:
        return
    raise PlanCompileError(
        code="chunked_text_mask_source_dtype_unsupported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(offending)} apply 'text_mask' to a non-string "
            "source on the chunked route. text_mask str()-converts every cell, so a "
            "non-string (e.g. integer-with-nulls) source diverges by chunk boundary "
            "(a null-free chunk stays int64 -> '1'; a null-bearing chunk widens to "
            "float64 -> '1.0'). Use a string source, or run full-frame on the oracle."
        ),
    )


def reject_unsafe_text_mask_source_dtype(
    plan: Any,
    source_schema: pa.Schema,
    *,
    table: str,
    registry: Any,
    relationship_graph: Any,
) -> None:
    """Convenience admission wrapper (resolve names + check one schema), kept for
    direct unit tests. The manual entry inlines the two steps so it can reuse the
    resolved names across every chunk.

    Raises:
        PlanCompileError: ``code='chunked_text_mask_source_dtype_unsupported'``.
    """
    cols = text_mask_source_columns(plan, registry, relationship_graph, table=table)
    reject_unsafe_text_mask_chunk_schema(source_schema, cols, table=table)


def reject_text_mask_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a `text_mask` column that also carries a `when:` predicate.

    Raises:
        PlanCompileError: ``code='chunked_text_mask_when_not_supported'``.
    """
    text_mask_when_cols = sorted(
        str(col_entry.get("name", "?"))
        for col_entry in table_cfg.get("columns") or []
        if isinstance(col_entry, dict)
        and col_entry.get("strategy") == "text_mask"
        and col_entry.get("when")
    )
    if not text_mask_when_cols:
        return
    raise PlanCompileError(
        code="chunked_text_mask_when_not_supported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(text_mask_when_cols)} combine 'text_mask' "
            "with a 'when:' predicate, which is not supported on the chunked "
            "route: the handler str()-converts every non-null cell, so a "
            "when-gated column leaves non-matching rows at their original "
            "(possibly numeric) dtype while matching rows become masked "
            "strings -- a chunk-boundary-dependent output dtype."
        ),
    )

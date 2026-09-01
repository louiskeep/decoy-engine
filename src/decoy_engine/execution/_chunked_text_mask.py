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


def unsafe_text_mask_source_columns(
    ordered_work: list[Any], source_schema: pa.Schema, *, table: str
) -> list[str]:
    """text_mask column names on `table` whose SOURCE Arrow type is not a
    chunk-stable string type (Trap: int-with-nulls widening). Empty when every
    text_mask column has a string / large_string source.

    Shared reason-collector: `reject_unsafe_text_mask_source_dtype` (the manual
    `run_mask_pipeline_chunked` entry, raises) and `_planner._runtime_source_
    rejections` (the auto route, collects a reason string) both call this so the
    two routes render the identical admission judgment. Reads the text_mask nodes
    off the work list (mirroring the group_key collector), not the raw config.
    """
    offending: list[str] = []
    for node in ordered_work:
        if node.table != table or node.kind != "scalar" or node.strategy != "text_mask":
            continue
        name = node.columns[0]
        idx = source_schema.get_field_index(name)
        if idx < 0 or not any(
            check(source_schema.field(idx).type) for check in _SAFE_TEXT_MASK_SOURCE_CHECKS
        ):
            offending.append(name)
    return sorted(offending)


def reject_unsafe_text_mask_source_dtype(
    plan: Any,
    source_schema: pa.Schema,
    *,
    table: str,
    registry: Any,
    relationship_graph: Any,
) -> None:
    """Reject a `text_mask` column whose SOURCE type is not chunk-stable string.

    The manual `run_mask_pipeline_chunked` entry's gate; the auto route calls
    `unsafe_text_mask_source_columns` directly from `_planner._runtime_source_
    rejections` (it already has an ordered work list).

    Raises:
        PlanCompileError: ``code='chunked_text_mask_source_dtype_unsupported'``.
    """
    from decoy_engine.execution._runner import build_work_list, order_work

    ordered_work = order_work(build_work_list(plan, registry), relationship_graph)
    offending = unsafe_text_mask_source_columns(ordered_work, source_schema, table=table)
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

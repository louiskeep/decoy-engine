"""DGRN admission + domain guard for chunked execution (Phase 4 slice 1).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_fk.py` / `_chunked_fk_dtype.py`. See `_chunked.py`'s module
docstring for the DGRN-admitted-strategy summary and
`docs/plans/2026-08-31-p4-slice1-dgrn-windowed-date.md` for the full design.

`windowed_date` is the first strategy admitted onto the chunked route by
consuming the durable global row number (DGRN, `base_row_offset` in
`run_mask_pipeline_chunked`) instead of being value-keyed: its output is
`derive(seed, ns, row_index.to_bytes(8, "big"))`, keyed on the row's
POSITION in the whole, unchunked source. Two correctness properties this
module enforces:

1. `windowed_date` must stay OUT of `CHUNK_SAFE_STRATEGIES`
   (`_chunked_fk.py`). That set is reused verbatim by
   `_chunked_fk.gate_fk_child_edges` as the FK-self-mask allowlist,
   which assumes every member is VALUE-keyed (parent and child compute
   the same masked bytes from the same raw value, independent of where
   that row landed). A `windowed_date` FK column would instead compute
   its own row's chunk-relative position, which generally differs from
   its parent's position, silently breaking referential integrity for
   matched keys. `CHUNK_DGRN_STRATEGIES` is therefore a SEPARATE set;
   `windowed_date` is admitted into `check_chunked_compatibility`'s
   per-column loop via this set, not by joining `CHUNK_SAFE_STRATEGIES`,
   so it stays correctly rejected by the FK gate exactly as before this
   slice (`chunked_fk_parent_strategy_not_safe`).
2. `windowed_date` + `when:` must be rejected. `run_with_when_gate`
   (`_when_gate.py`) passes only the MATCHING rows to the handler, so
   the full-frame oracle's `enumerate(anchor_series)` numbers the
   FILTERED subset `0..matches-1`, not each row's physical position. A
   chunk enumerating from its own durable `row_offset` diverges from
   that filtered numbering whenever any preceding row (in this chunk or
   an earlier one) does not match, so byte parity cannot hold. The
   auto-planner separately rejects ALL `when` predicates for auto-
   routing (`_planner._whole_column_state_rejections`), but that gate
   does not run for a direct `run_mask_pipeline_chunked` call, so
   `check_chunked_compatibility` -- the public entry point's own gate --
   must reject it too.

The domain guard (`i.to_bytes(8, "big")` accepts only `0 <= i <=
2**64-1`) is enforced at two points, because `run_mask_pipeline_chunked`
takes an arbitrary `Iterable[pa.Table]` with no whole-stream row count:
`validate_base_row_offset` checks the caller-supplied starting position
once, at entry; `validate_chunk_row_offset_range` checks each chunk's own
row range immediately before that chunk is masked. Both raise the
deliberate `chunked_row_offset_out_of_domain` code rather than letting an
out-of-range value surface later as an incidental `OverflowError` inside
`int.to_bytes(8)`.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.plan._errors import PlanCompileError

# Admitted via the durable global row offset rather than by value. See the
# module docstring for why this must stay disjoint from CHUNK_SAFE_STRATEGIES.
CHUNK_DGRN_STRATEGIES: frozenset[str] = frozenset({"windowed_date"})

# `i.to_bytes(8, "big")` in `apply_windowed_date` accepts only this range.
ROW_OFFSET_DOMAIN_MAX: int = 2**64 - 1


def validate_base_row_offset(base_row_offset: int) -> None:
    """Reject a `base_row_offset` outside the DGRN domain, or non-int.

    Raises:
        ExecutionError: ``code='chunked_row_offset_out_of_domain'``.
    """
    # bool is an int subclass; reject it explicitly so `base_row_offset=True`
    # (an easy config-plumbing mistake) does not silently mask as row 1.
    if isinstance(base_row_offset, bool) or not isinstance(base_row_offset, int):
        raise ExecutionError(
            code="chunked_row_offset_out_of_domain",
            message=(
                f"base_row_offset must be an int in [0, {ROW_OFFSET_DOMAIN_MAX}]; "
                f"got {base_row_offset!r} ({type(base_row_offset).__name__})."
            ),
        )
    if not (0 <= base_row_offset <= ROW_OFFSET_DOMAIN_MAX):
        raise ExecutionError(
            code="chunked_row_offset_out_of_domain",
            message=(
                f"base_row_offset {base_row_offset} is outside the DGRN domain "
                f"[0, {ROW_OFFSET_DOMAIN_MAX}] (`i.to_bytes(8, 'big')`'s range)."
            ),
        )


def validate_chunk_row_offset_range(chunk_base_offset: int, num_rows: int) -> None:
    """Reject a chunk whose row range would exceed the DGRN domain.

    `chunk_base_offset` is the durable row number of the chunk's first row;
    the chunk's last row is `chunk_base_offset + num_rows - 1`.

    Raises:
        ExecutionError: ``code='chunked_row_offset_out_of_domain'``.
    """
    if num_rows == 0:
        return
    last_row = chunk_base_offset + num_rows - 1
    if last_row > ROW_OFFSET_DOMAIN_MAX:
        raise ExecutionError(
            code="chunked_row_offset_out_of_domain",
            message=(
                f"chunk spanning rows [{chunk_base_offset}, {last_row}] exceeds the "
                f"DGRN domain [0, {ROW_OFFSET_DOMAIN_MAX}]."
            ),
        )


def reject_windowed_date_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a `windowed_date` column that also carries a `when:` predicate.

    Raises:
        PlanCompileError: ``code='chunked_windowed_date_when_not_supported'``.
    """
    windowed_date_when_cols = sorted(
        str(col_entry.get("name", "?"))
        for col_entry in table_cfg.get("columns") or []
        if isinstance(col_entry, dict)
        and col_entry.get("strategy") == "windowed_date"
        and col_entry.get("when")
    )
    if not windowed_date_when_cols:
        return
    raise PlanCompileError(
        code="chunked_windowed_date_when_not_supported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(windowed_date_when_cols)} combine "
            "'windowed_date' with a 'when:' predicate, which is not supported "
            "on the chunked route: `when` passes only matching rows to the "
            "handler, so the oracle enumerates the filtered subset "
            "0..matches-1, not each row's physical position -- a chunk's "
            "durable row offset cannot reproduce that filtered enumeration."
        ),
    )

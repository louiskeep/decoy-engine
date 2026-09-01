"""bucket_perturb (explicit date_format) admission gates for chunked execution
(Phase 4 slice 5).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_code_set.py` / `_chunked_text_mask.py`. See `_chunked.py`'s module
docstring for the bucket_perturb-admitted-shape summary and
`docs/plans/2026-09-01-p4-slice5-bucket-perturb-chunked.md` for the full
design.

`apply_bucket_perturb` (`transforms/bucket_perturb.py`) parses each date
string with the resolved `strptime` format, snaps it to a deterministic
position within its bucket via `derive(job_seed, namespace,
canonicalize(value))`, and reformats with `strftime`. With the format fixed,
this is a pure function of `(value, job_seed, namespace, bucket,
date_format)` -- no row position, no whole-column reduction -- so per-chunk
masking reproduces whole-column masking value-for-value, the chunk
invariant. Three things this module enforces so that holds in practice:

1. **Config-shape admission** (`bucket_perturb_conditional_failures`): an
   explicit `date_format` only. Without one, `fmt = date_format or
   _detect_format(series)` detects the format from the WHOLE series (a
   cross-row reduction whose result can differ between a chunk and the full
   column); the handler resolves an empty-string `date_format` the same as
   absent (`cfg.get("date_format") or None`), so `""` is rejected here too.

2. **Runtime source-dtype gate** (the text_mask/code_set trio, renamed):
   `apply_bucket_perturb` operates on date STRINGS (`series.astype(str)`,
   `pd.to_datetime(series, format=fmt)`). A non-string source is the clear
   unsafe case: an Arrow `int64` chunk with nulls renders to pandas
   `float64`, turning `"20240101"` into `"20240101.0"`, a chunk-boundary-
   dependent promotion. Only a chunk-stable string / large_string source is
   admitted (a deliberately-proven subset -- date32/timestamp sources may in
   fact convert chunk-stably, but that is unproven here).

3. **FK-key exclusion, BOTH orientations** (`reject_bucket_perturb_fk_keys`):
   `bucket_perturb` is admitted as a CONDITIONAL strategy, never joining
   `CHUNK_SAFE_STRATEGIES`, so `_chunked_fk.gate_fk_child_edges`'s self-mask
   allowlist correctly excludes a `bucket_perturb` CHILD key edge. But that
   gate only inspects edges where the chunked table is the CHILD, so a
   `bucket_perturb` PARENT key column would otherwise slip through this
   module's own conditional admission unchallenged -- the parent would chunk
   (its bucket_perturb key masked independently per chunk) while the child
   cannot self-mask bucket_perturb at all, a mixed-route referential-
   integrity split this slice does not take on. This gate rejects `table`
   fail-closed whenever it participates, as parent or child, in any FK edge
   whose key column is `bucket_perturb`.

Unlike `code_set`, there is no corpus to pin (no cross-chunk resolution
state) and the handler returns no evidence (`run` returns `(result, [])`),
so this module carries no pinning/aggregation counterpart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.plan._errors import PlanCompileError

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

# bucket_perturb's domain is a date string: a STRING / LARGE_STRING source
# ingests to a chunk-stable pandas dtype. A non-string source does not, for
# the exact reason text_mask's / code_set's do not (see those modules): an
# int-with-nulls column widens to float64 on a null-bearing chunk but stays
# int64 on a null-free one, and the same value round-trips to a different
# string depending purely on which other rows share its chunk.
_SAFE_BUCKET_PERTURB_SOURCE_CHECKS = (pa.types.is_string, pa.types.is_large_string)


def bucket_perturb_conditional_failures(col_entry: dict[str, Any]) -> list[str]:
    """Unmet chunked-admission conditions for a `bucket_perturb` column: an
    explicit `date_format`. Empty list means the column is admitted.
    """
    cfg = col_entry.get("provider_config") or {}
    failures: list[str] = []
    if not cfg.get("date_format"):
        failures.append(
            "requires an explicit date_format (autodetect resolves the format "
            "from the whole column, a cross-row reduction that can differ "
            "between a chunk and the full column; the handler treats an "
            "empty string the same as absent)"
        )
    return failures


def _bucket_perturb_node_columns(ordered_work: list[WorkNode], table: str) -> list[str]:
    return [
        node.columns[0]
        for node in ordered_work
        if node.table == table and node.kind == "scalar" and node.strategy == "bucket_perturb"
    ]


def _unsafe_cols_in_schema(bucket_perturb_cols: list[str], schema: pa.Schema) -> list[str]:
    """Of `bucket_perturb_cols`, those NOT provably chunk-stable string in
    `schema` (a non-string type, or absent)."""
    offending: list[str] = []
    for name in bucket_perturb_cols:
        idx = schema.get_field_index(name)
        if idx < 0 or not any(
            check(schema.field(idx).type) for check in _SAFE_BUCKET_PERTURB_SOURCE_CHECKS
        ):
            offending.append(name)
    return sorted(offending)


def unsafe_bucket_perturb_source_columns(
    ordered_work: list[WorkNode], source_schema: pa.Schema, *, table: str
) -> list[str]:
    """bucket_perturb column names on `table` whose SOURCE Arrow type is not
    a chunk-stable string type. Empty when every bucket_perturb column has a
    string / large_string source. The auto route's collector
    (`_planner._runtime_source_rejections`); the manual entry validates PER
    CHUNK instead (`bucket_perturb_source_columns` + `reject_unsafe_bucket_
    perturb_chunk_schema`), because it takes an arbitrary chunk iterable
    whose dtype could drift across chunks.
    """
    return _unsafe_cols_in_schema(_bucket_perturb_node_columns(ordered_work, table), source_schema)


def bucket_perturb_source_columns(
    plan: Plan, registry: ProviderRegistry, relationship_graph: RelationshipGraph, *, table: str
) -> list[str]:
    """The bucket_perturb column names on `table`, resolved once from the
    plan's work list, so the manual entry can validate every chunk's schema
    against them without rebuilding the work list per chunk."""
    from decoy_engine.execution._runner import build_work_list, order_work

    ordered_work = order_work(build_work_list(plan, registry), relationship_graph)
    return _bucket_perturb_node_columns(ordered_work, table)


def reject_unsafe_bucket_perturb_chunk_schema(
    schema: pa.Schema, bucket_perturb_cols: list[str], *, table: str
) -> None:
    """Reject if any of `bucket_perturb_cols` is not a chunk-stable string
    type in `schema`. Called on the FIRST chunk (admission) AND on EVERY
    subsequent chunk of the manual `run_mask_pipeline_chunked` iterable,
    since a caller can feed a string first chunk and a divergent (e.g. int)
    later chunk.

    Raises:
        PlanCompileError: ``code='chunked_bucket_perturb_source_dtype_unsupported'``.
    """
    offending = _unsafe_cols_in_schema(bucket_perturb_cols, schema)
    if not offending:
        return
    raise PlanCompileError(
        code="chunked_bucket_perturb_source_dtype_unsupported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(offending)} apply 'bucket_perturb' to a "
            "non-string source on the chunked route. bucket_perturb parses each "
            "value as a date string, so a non-string (e.g. integer-with-nulls) "
            "source diverges by chunk boundary (a null-free chunk stays int64 -> "
            "'1'; a null-bearing chunk widens to float64 -> '1.0'). Use a string "
            "source, or run full-frame on the oracle."
        ),
    )


def reject_bucket_perturb_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a `bucket_perturb` column that also carries a `when:` predicate.

    Raises:
        PlanCompileError: ``code='chunked_bucket_perturb_when_not_supported'``.
    """
    when_cols = sorted(
        str(col_entry.get("name", "?"))
        for col_entry in table_cfg.get("columns") or []
        if isinstance(col_entry, dict)
        and col_entry.get("strategy") == "bucket_perturb"
        and col_entry.get("when")
    )
    if not when_cols:
        return
    raise PlanCompileError(
        code="chunked_bucket_perturb_when_not_supported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(when_cols)} combine 'bucket_perturb' with a "
            "'when:' predicate, which is not supported on the chunked route: "
            "a when-gated predicate can carry a pandas-eval whole-column "
            "reduction whose per-chunk evaluation selects different rows "
            "than the whole-frame evaluation."
        ),
    )


def _column_strategy(config: dict[str, Any], table: str, column: str) -> object:
    for tbl in config.get("tables") or []:
        if not isinstance(tbl, dict) or tbl.get("name") != table:
            continue
        for col in tbl.get("columns") or []:
            if isinstance(col, dict) and col.get("name") == column:
                return col.get("strategy")
    return None


def reject_bucket_perturb_fk_keys(config: dict[str, Any], *, table: str) -> None:
    """Reject `table` when it participates, as PARENT or CHILD, in any FK
    edge whose key column uses `bucket_perturb` (see module docstring
    point 3).

    Raises:
        PlanCompileError: ``code='chunked_bucket_perturb_fk_key_unsupported'``.
    """
    offending: set[str] = set()
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        parent_info = rel_entry.get("parent") or {}
        if isinstance(parent_info, dict) and parent_info.get("table") == table:
            for col in parent_info.get("columns") or []:
                if (
                    isinstance(col, str)
                    and _column_strategy(config, table, col) == "bucket_perturb"
                ):
                    offending.add(col)
        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict) or child_info.get("table") != table:
                continue
            for col in child_info.get("columns") or []:
                if (
                    isinstance(col, str)
                    and _column_strategy(config, table, col) == "bucket_perturb"
                ):
                    offending.add(col)
    if not offending:
        return
    raise PlanCompileError(
        code="chunked_bucket_perturb_fk_key_unsupported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(sorted(offending))} on table {table!r} use "
            "'bucket_perturb' as an FK key column (parent or child side). "
            "bucket_perturb is not in CHUNK_SAFE_STRATEGIES, and the FK "
            "self-mask gate only inspects child-side edges, so a "
            "bucket_perturb PARENT key would otherwise chunk independently "
            "while its child (which cannot self-mask bucket_perturb) falls "
            "to full-frame -- a mixed-route referential-integrity split. Use "
            "run_pipeline or run_sequential instead."
        ),
    )

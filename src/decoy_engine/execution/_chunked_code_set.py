"""code_set admission + corpus-pinning gates for chunked execution (Phase 4
slice 4).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_text_mask.py` / `_chunked_group_key.py`. See `_chunked.py`'s module
docstring for the code_set-admitted-shape summary and
`docs/plans/2026-09-01-p4-slice4-code-set-chunked.md` for the full design.

`code_set` mask mode (`transforms/code_set._pick_mask`) selects a corpus code
via `HMAC(derive(ctx.mask_key, namespace or "code_set", salt), value) %
candidate_count`: a pure function of (value, corpus record, mask_key,
namespace), all per-column constants except the value, so per-chunk masking
reproduces whole-column masking value-for-value -- the chunk invariant. Four
things this module enforces so that holds in practice:

1. **Config-shape admission** (`code_set_conditional_failures`): only mask
   mode, without `chapter_preserve`. Gen mode selects by a whole-run
   `row_index` the per-chunk kernel does not carry; `chapter_preserve` raises
   per-value fail-closed errors, a quarantine shape the chunked route does not
   model.

2. **Runtime source-dtype gate** (the text_mask trio, renamed): the pandas
   chunked route converts each chunk Arrow->pandas and the handler calls
   `str(value)` (`_strategies/_code_set.py`), so it is exposed to the same
   int-with-nulls -> float widening that makes `str(1)` become `"1.0"` in one
   chunk and `"1"` in another. Only a chunk-stable string / large_string
   source is admitted.

3. **`when:` rejection** (`reject_code_set_when`): a `when:` predicate can
   carry a pandas-eval whole-column reduction (e.g. `a > a.mean()`) whose
   per-chunk evaluation selects different rows than the whole-frame
   evaluation; rejecting it also avoids a per-chunk `preflight()`
   re-resolving the corpus.

4. **FK-key exclusion, BOTH orientations** (`reject_code_set_fk_keys`):
   `code_set` is admitted as a CONDITIONAL strategy, never joining
   `CHUNK_SAFE_STRATEGIES`, so `_chunked_fk.gate_fk_child_edges`'s
   self-mask allowlist correctly excludes a `code_set` CHILD key edge. But
   that gate only inspects edges where the chunked table is the CHILD
   (`_chunked_fk.py`'s per-edge loop skips parent-only edges), so a
   `code_set` PARENT key column would otherwise slip through this module's
   own conditional admission unchallenged -- the parent would chunk (its
   code_set key masked independently per chunk) while the child cannot
   self-mask code_set at all, a mixed-route referential-integrity split this
   slice does not take on. This gate rejects `table` fail-closed whenever it
   participates, as parent or child, in any FK edge whose key column is
   `code_set`.

Corpus pinning (`resolve_pinned_code_set_records`, the build's central task):
the chunked route does NOT share one `StrategyContext` across chunks -- each
chunk's `adapter.run(...)` call builds a fresh one (`_pandas_adapter.py`), so
`ctx.code_set_records` resets per chunk unless seeded. The whole-column
handler resolves its corpus once and threads that SAME record to every value
precisely so a mid-run file swap cannot split output across values
(`_strategies/_code_set.py`'s module docstring); this function reproduces
that guarantee ACROSS chunks by resolving one `_CorpusRecord` per code_set
column up front and returning a `(table, column) -> record` mapping the
caller seeds into every chunk's context via the adapter's `code_set_records`
parameter. Resolving here (not lazily inside a chunk) also means resolution
-- and therefore a `corpus_source_version` mismatch -- fails closed before
any chunk streams, even for a zero-row source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

# code_set's domain is a corpus CODE: a STRING / LARGE_STRING source ingests
# to a chunk-stable pandas dtype. A non-string source does not, for the exact
# reason text_mask's does not (see `_chunked_text_mask.py`): the handler
# `str()`-converts every cell (`_strategies/_code_set.py`), so an
# int-with-nulls column widens to float64 on a null-bearing chunk but stays
# int64 on a null-free one, and the SAME value yields "1" vs "1.0" depending
# purely on which other rows share its chunk.
_SAFE_CODE_SET_SOURCE_CHECKS = (pa.types.is_string, pa.types.is_large_string)


def code_set_conditional_failures(col_entry: dict[str, Any]) -> list[str]:
    """Unmet chunked-admission conditions for a `code_set` column: mask mode,
    no `chapter_preserve`. Empty list means the column is admitted.
    """
    cfg = col_entry.get("provider_config") or {}
    failures: list[str] = []
    mode = str(cfg.get("mode", "mask"))
    if mode != "mask":
        failures.append(
            "requires mode 'mask' (gen mode selects by a whole-run row_index "
            "the per-chunk kernel does not carry)"
        )
    if cfg.get("chapter_preserve"):
        failures.append(
            "requires chapter_preserve absent or false (it raises per-value "
            "fail-closed errors, a quarantine shape the chunked route does "
            "not model)"
        )
    return failures


def _code_set_node_columns(ordered_work: list[WorkNode], table: str) -> list[str]:
    return [
        node.columns[0]
        for node in ordered_work
        if node.table == table and node.kind == "scalar" and node.strategy == "code_set"
    ]


def _unsafe_cols_in_schema(code_set_cols: list[str], schema: pa.Schema) -> list[str]:
    """Of `code_set_cols`, those NOT provably chunk-stable string in `schema`
    (a non-string type, or absent)."""
    offending: list[str] = []
    for name in code_set_cols:
        idx = schema.get_field_index(name)
        if idx < 0 or not any(
            check(schema.field(idx).type) for check in _SAFE_CODE_SET_SOURCE_CHECKS
        ):
            offending.append(name)
    return sorted(offending)


def unsafe_code_set_source_columns(
    ordered_work: list[WorkNode], source_schema: pa.Schema, *, table: str
) -> list[str]:
    """code_set column names on `table` whose SOURCE Arrow type is not a
    chunk-stable string type. Empty when every code_set column has a
    string / large_string source. The auto route's collector
    (`_planner._runtime_source_rejections`); the manual entry validates PER
    CHUNK instead (`code_set_source_columns` + `reject_unsafe_code_set_chunk_
    schema`), because it takes an arbitrary chunk iterable whose dtype could
    drift across chunks.
    """
    return _unsafe_cols_in_schema(_code_set_node_columns(ordered_work, table), source_schema)


def code_set_source_columns(
    plan: Plan, registry: ProviderRegistry, relationship_graph: RelationshipGraph, *, table: str
) -> list[str]:
    """The code_set column names on `table`, resolved once from the plan's
    work list, so the manual entry can validate every chunk's schema against
    them without rebuilding the work list per chunk."""
    from decoy_engine.execution._runner import build_work_list, order_work

    ordered_work = order_work(build_work_list(plan, registry), relationship_graph)
    return _code_set_node_columns(ordered_work, table)


def reject_unsafe_code_set_chunk_schema(
    schema: pa.Schema, code_set_cols: list[str], *, table: str
) -> None:
    """Reject if any of `code_set_cols` is not a chunk-stable string type in
    `schema`. Called on the FIRST chunk (admission) AND on EVERY subsequent
    chunk of the manual `run_mask_pipeline_chunked` iterable, since a caller
    can feed a string first chunk and a divergent (e.g. int) later chunk.

    Raises:
        PlanCompileError: ``code='chunked_code_set_source_dtype_unsupported'``.
    """
    offending = _unsafe_cols_in_schema(code_set_cols, schema)
    if not offending:
        return
    raise PlanCompileError(
        code="chunked_code_set_source_dtype_unsupported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(offending)} apply 'code_set' to a non-string "
            "source on the chunked route. code_set str()-converts every cell, so a "
            "non-string (e.g. integer-with-nulls) source diverges by chunk boundary "
            "(a null-free chunk stays int64 -> '1'; a null-bearing chunk widens to "
            "float64 -> '1.0'). Use a string source, or run full-frame on the oracle."
        ),
    )


def reject_code_set_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a `code_set` column that also carries a `when:` predicate.

    Raises:
        PlanCompileError: ``code='chunked_code_set_when_not_supported'``.
    """
    when_cols = sorted(
        str(col_entry.get("name", "?"))
        for col_entry in table_cfg.get("columns") or []
        if isinstance(col_entry, dict)
        and col_entry.get("strategy") == "code_set"
        and col_entry.get("when")
    )
    if not when_cols:
        return
    raise PlanCompileError(
        code="chunked_code_set_when_not_supported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(when_cols)} combine 'code_set' with a "
            "'when:' predicate, which is not supported on the chunked route: "
            "a when-gated predicate can carry a pandas-eval whole-column "
            "reduction whose per-chunk evaluation selects different rows "
            "than the whole-frame evaluation, and rejecting it also avoids "
            "a per-chunk preflight() re-resolving the corpus."
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


def reject_code_set_fk_keys(config: dict[str, Any], *, table: str) -> None:
    """Reject `table` when it participates, as PARENT or CHILD, in any FK
    edge whose key column uses `code_set` (see module docstring point 4).

    Raises:
        PlanCompileError: ``code='chunked_code_set_fk_key_unsupported'``.
    """
    offending: set[str] = set()
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        parent_info = rel_entry.get("parent") or {}
        if isinstance(parent_info, dict) and parent_info.get("table") == table:
            for col in parent_info.get("columns") or []:
                if isinstance(col, str) and _column_strategy(config, table, col) == "code_set":
                    offending.add(col)
        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict) or child_info.get("table") != table:
                continue
            for col in child_info.get("columns") or []:
                if isinstance(col, str) and _column_strategy(config, table, col) == "code_set":
                    offending.add(col)
    if not offending:
        return
    raise PlanCompileError(
        code="chunked_code_set_fk_key_unsupported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(sorted(offending))} on table {table!r} use "
            "'code_set' as an FK key column (parent or child side). code_set "
            "is not in CHUNK_SAFE_STRATEGIES, and the FK self-mask gate only "
            "inspects child-side edges, so a code_set PARENT key would "
            "otherwise chunk independently while its child (which cannot "
            "self-mask code_set) falls to full-frame -- a mixed-route "
            "referential-integrity split. Use run_pipeline or run_sequential "
            "instead."
        ),
    )


def resolve_pinned_code_set_records(
    plan: Plan, registry: ProviderRegistry, relationship_graph: RelationshipGraph, *, table: str
) -> dict[tuple[str, str], Any]:
    """Resolve ONE corpus record per code_set column on `table`, before any
    chunk streams (module docstring's corpus-pinning contract). The caller
    seeds the returned mapping into every per-chunk `StrategyContext` via the
    adapter's `code_set_records` parameter, so `CodeSetHandler.run`'s own
    `ctx.code_set_records.get((table, column))` cache hit reuses this SAME
    record instead of re-resolving (and potentially picking up a mid-run file
    swap) on every chunk.

    Resolution failure (missing/invalid corpus, `corpus_source_version`
    mismatch) raises here -- before the caller's empty-input early return --
    so a zero-row job with a bad corpus fails closed exactly like the oracle,
    which resolves the same corpus on its first (only) pass over the column
    regardless of row count.
    """
    from decoy_engine.execution._runner import build_work_list, order_work
    from decoy_engine.transforms.code_set import CodeSetConfig, resolve_corpus_record

    ordered_work = order_work(build_work_list(plan, registry), relationship_graph)
    records: dict[tuple[str, str], Any] = {}
    for node in ordered_work:
        if node.table != table or node.kind != "scalar" or node.strategy != "code_set":
            continue
        plan_slice = node.plan_slice
        if not isinstance(plan_slice, ColumnSeed):
            continue
        code_cfg = CodeSetConfig.from_dict(provider_config_to_dict(plan_slice.provider_config))
        records[(table, node.columns[0])] = resolve_corpus_record(code_cfg)
    return records


def aggregate_chunk_code_set_corpora(chunk_results: list[Any]) -> dict[str, Any]:
    """Aggregate each chunk's `code_set_corpora` evidence into ONE entry per
    (table, column) -- `masked_any` semantics, matching the full-frame
    handler's once-per-column stamp (`StrategyContext.code_set_corpora_
    metrics`): a column that masked at least one value in ANY chunk
    contributes its evidence exactly once. Corpus pinning guarantees every
    chunk that DOES stamp a given column stamps the identical record, so the
    first occurrence is representative; `{}` when no chunk masked a code_set
    column.
    """
    seen: dict[tuple[Any, Any], dict[str, Any]] = {}
    for result in chunk_results:
        for entry in result.quality_metrics.get("code_set_corpora") or ():
            seen.setdefault((entry.get("table"), entry.get("column")), entry)
    if not seen:
        return {}
    return {"code_set_corpora": list(seen.values())}

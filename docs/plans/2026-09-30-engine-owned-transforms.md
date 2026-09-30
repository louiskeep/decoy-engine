# Engine-owned table transforms (Rust engine program A8)

Status: plan (revision 7: folds the Codex plan-gate NO-GO on revisions 1 to 6; Cam chose to continue after round 5)

Date: 2026-09-30. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase A, item A8 (Cam decision 2, 2026-09-30: transforms move into the engine's `run_pipeline` so the engine, CLI and platform behave the same). Branch `feat/engine-owned-transforms`, off engine main `8dc559e5`. Paired platform work: A8p-0 and A8p below, each its own platform branch.

## Problem

A mask table's `transforms` block (filter, sort, limit, dedupe, derive, drop_column; `config/_transforms.py`, field `TableConfig.transforms`, `config/_tables.py` ~279-328) passes engine validation but no engine code path applies it. The only production caller of `execution._transforms.apply_transforms` is the platform: `api/jobs/v2_runner.py::_apply_per_table_transforms` (~162-211), which `v2_sequential.py` (~124-135, and again for vault reload ~221-237) and `v2_preview.py` (~125-135) reuse.

So one valid config gives different output by caller. The platform filters, derives and drops; `decoy_engine.run_pipeline`, the CLI and `decoy.mask(config=...)` mask the untransformed table without a warning. The `_transforms.py` module docstring names a call site that does not exist.

## Ownership contract

- **`run_pipeline` owns transforms.** It applies each mask table's transforms exactly once, over the whole table, before any strategy reads it, or it rejects the job with a coded error. Callers hand it raw sources.
- **Plan-level APIs take prepared inputs.** `PandasExecutionAdapter.run`, `run_sequential` and `run_fk_out_of_core` receive a `Plan`, not the config, so they cannot see transforms. Their docstrings state that inputs must already be transformed; callers that use them directly apply the public helper themselves.
- **Config-taking chunked entry points reject.** `check_chunked_compatibility` (`_chunked.py` ~250) raises `PlanCompileError(code="per_table_transforms_present")` when the target table declares transforms. Keeping its existing exception type matters: `_planner._chunked_rejection` (~259, ~318) catches `PlanCompileError` and turns incompatibility into a full-frame fallback, and every direct `classify_job` consumer (physical-plan capture and compile, `explain_plan`) relies on the same contract. Callers that reach it and must reject: `run_mask_pipeline_chunked` (~358, public), the native-or-oracle dispatcher (`native/_dispatch.py` ~483-525), which must call the gate before `iter(chunks)` so a rejected job consumes no chunk (today it reads the first chunk before any gate), and all three config-taking physical adapters in `physical/drivers/_chunked.py` (mask ~50, native-or-oracle ~91, resident aggregator ~128). Zero applications, no silent drop.
- **Public helper.** `decoy_engine.apply_table_transforms(config, table_name, table: pa.Table) -> pa.Table`, exported in `__all__` and the compatibility contract. It returns the same object when the table has no transforms or is generate-kind, and validates ops through `TableConfig`.

## Routes inside `run_pipeline`

| Route | Behavior for a mask table with transforms |
|---|---|
| Full-frame (incl. generate+mask, isolated runs, which call `run_pipeline`) | Applied once in `_pipeline_sources.resolve_resident_sources` |
| Auto-chunk | Ineligible through the same gate: the planner sees `PlanCompileError(per_table_transforms_present)` and falls back to full-frame. Whole-table sort, limit and dedupe cannot be chunk-local, and a resident preprocess followed by chunking would not bound memory anyway. |
| Sequential | Applied once per table on load, by wrapping the loader in `_psrc.resolve_sequential_loader` |
| Out-of-core, `auto` | Declined; falls back by existing rules. `route_reason` keeps its current value; a new telemetry field `out_of_core_declined` carries `per_table_transforms_present` so the cause is visible |
| Out-of-core, explicit | A config-only preflight before `profile_source` raises `per_table_transforms_present`, so nothing is read |
| Unified slice | Declines, unchanged (`_unified_slice_admission.py` ~307-309) |

## Memory admission

Routing runs on the raw profile before transforms. Row counts stay conservative (filter, limit and dedupe only shrink; sort, drop and derive keep the count), but `derive` widens rows and the raw byte estimate (`_pipeline_routing_signals.py` ~236-312) does not see derived columns.

- Derived-column width is treated as unpriceable: pandas and numexpr can promote narrow inputs (an int8 expression can yield int64 or float64), so no raw-profile formula is a safe upper bound.
- Relationship-bearing pure-mask jobs (the only shape the static-fit and probe admission applies to, `_pipeline_routing.py` ~257-272, ~439): a transform-bearing job is never admitted by the raw static-fit shortcut. When the probe runs (resident sources, default flags, raw bytes not already over budget; `_pipeline_routing_signals.py` ~411-443) it measures the transformed execution; when it is skipped or inconclusive the job stays on sequential or out-of-core routing.
- Every other shape (no relationships): byte-estimate routing has no effect today, and transforms make auto-chunk unavailable, so a large transform-bearing job runs full-frame. This slice does not add a new rejection for that case; it is recorded as a known limit, pinned by a test, and left for a later memory-admission item.
- `TableConfig.transforms` gets a maximum length of 32 ops (pre-GA hard change) so width growth is bounded.

## Arrow/pandas conversion

Invariant: a table with transforms masks exactly as the same data would without them. The round-trip through pandas must not change the Arrow type of any column the transforms leave alone.

The platform's current bare `table.to_pandas()` breaks that for every nullable integer column, not only FK keys: any integer column with a null becomes float64, stays float64 after `pa.Table.from_pandas`, and loses exactness above 2^53. That also changes downstream behavior, because the strategies and `reject_null_bearing_int` (`_pandas_adapter.py` ~191) see a float column instead of a nullable integer one, and the adapter's own lossless sets (FK keys, `date_shift.group_by`, `top_code`, `group_key.group_by`, ~193-215) are defeated because the precision is already gone before the adapter runs.

Decision: row-lineage reconstruction. pandas decides which rows survive and in what order, and computes derived columns; it never supplies the values of source columns.

1. Convert exactly as the no-transform path does: `to_pandas_fk_safe(table, <every Arrow integer column that contains a null>)` on the table as given, metadata intact (the same metadata-aware conversion `_pandas_adapter.py` ~210 uses). Transform-visible columns therefore have the same pandas dtypes they have today, including metadata-backed nullable integers (`Int8` and friends), so filter and derive arithmetic is unchanged; null-free integers without such metadata stay numpy-typed. A stored pandas index (a non-Range index recorded in the `pandas` metadata) stays in the pandas index, hidden from expressions and dedupe, exactly as today. Then replace the frame's row labels with a fresh `pd.RangeIndex(0, table.num_rows)`: these labels are the source row ordinals, and the stored index is discarded, matching the no-transform and platform paths, which both emit with `preserve_index=False`.
2. Carry the ordinals through the ops as the index. `_apply_filter`, `_apply_sort`, `_apply_limit` and `_apply_dedupe` all stop calling `reset_index(drop=True)` (today every one of them resets). With unique ordinal labels, the filter's boolean mask aligns by label, `df.assign` in derive aligns by label, `sort_values(kind="stable")` keeps labels and tie order, `head` keeps labels, and `drop_duplicates` keeps the first occurrence in current row order. The ordinal is never a column, so a dedupe over all columns, a filter, or a derive expression cannot see or match it.
3. Track each output column's origin through the ordered ops, as a list of `(name, origin)` bindings that starts with every source DATA column bound to `source`: the columns of the pandas frame from step 1, which excludes physical fields the `pandas` metadata marks as index columns. `drop_column` removes the current binding for that name; `derive` adds a binding with origin `derived`, even when a source column of the same name existed earlier and was dropped. Rebuild the output from the final bindings in order: a `source` binding is `pyarrow.compute.take(<original source column>, <final index as ordinals>)` with the column's original `pa.Field` (name, type, nullability and field metadata); a `derived` binding is converted from pandas (`pa.array(..., from_pandas=True)`) with an inferred type. Values, validity, NaN versus null, decimal precision and scale, dictionary type and ordered flag, and timestamp unit and time zone of source columns come from the source, not from pandas. `take` guarantees the same Arrow type and the selected logical values; it does not promise identical physical buffers, chunking or dictionary layout, and the plan does not require them.
4. Table-level schema metadata: keep the source table's metadata minus the `pandas` key (stale after drop and derive), and add none.

Consequences stated plainly: filter predicates, sort keys and dedupe keys are evaluated on the pandas view, so pandas semantics apply to those decisions (NaN and null both count as missing in a predicate; dedupe treats them as equal). That matches what the platform does today and is documented on `apply_table_transforms`. The emitted values themselves are exact.

Compatibility delta against today's platform, for transform-bearing tables only: every nullable integer column (any width, signed or unsigned, including small values like `[1, null]` and all-null integer columns) keeps its integer type instead of becoming float64; decimal, dictionary and temporal columns keep their exact Arrow type and values instead of what pandas inference produces; a float NaN stays NaN rather than becoming null; values above 2^53 are also no longer rounded; and a hash, truncate or categorical strategy on a nullable integer column now hits the same `reject_null_bearing_int` rejection it already hits on tables without transforms. Tables without transforms are unaffected. This goes in the CHANGELOG and is flagged to Cam at merge.

## Platform work (decoy-platform, Cam's go to merge)

**Version skew.** The platform declares `decoy-engine>=0.5.0` with no lockfile, so an old platform can install the new engine and apply transforms twice. The four combinations:

| Platform | Engine | Result | Prevented by |
|---|---|---|---|
| old | old | platform applies, correct | |
| old | new | double application | A8p-0 upper bound |
| new | old | never applied | A8p minimum version |
| new | new | engine applies, correct | |

- **A8p-0 (ships first, before A8 releases):** cap `decoy-engine<0.7.0`. V = 0.7.0, the engine release containing A8 (engine main is 0.6.0); if the release number changes, A8p-0 is amended before it merges. No behavior change.
- **A8p (ships with 0.7.0):** raise the minimum to `>=0.7.0` and remove the cap, and change the call sites:
  - Main run (`v2_runner.py` ~283-300): stop preprocessing; pass raw sources to `run_pipeline`.
  - Sequential (`v2_sequential.py`), which calls `run_sequential` directly: keep calling a transform on load, now the public `apply_table_transforms`, in both the execution loader and the vault-reload loader, so vault rows stay aligned with filtered, sorted, deduped or limited output.
  - Preview (`v2_preview.py`): transform the full source with `apply_table_transforms`, slice, then pass `run_pipeline` a config copy with that table's `transforms` cleared.
  - Delete `_apply_per_table_transforms` and the platform's private import of `decoy_engine.execution._transforms`.
- **Rollback:** roll back the platform to A8p-0 and the engine to the release before V together. Rolling back one side alone is blocked by the version bounds (install fails rather than running wrong).

## Acceptance tests (written first; each behavioral test records its red-before output)

Engine (A8):

1. **Applied once, correct output.** For each op and a chained sequence, `run_pipeline` output equals an independent oracle: raw source, then the ops applied by a test-local type-preserving reference (pandas nullable dtypes built in the test, not the new helper), then `run_pipeline` with transforms cleared. Compare full Arrow schema and values, not row counts. Routes: resident full-frame, generate+mask (mask table), sequential with transforms on parent and child, isolated execution.
2. **Exactly once.** Spy on `apply_transforms`: one call per transform-bearing mask table on each route in test 1; zero calls on declined or rejected routes (test 4-6).
3. **Auto-chunk ineligible.** With `auto_chunk=True` above the threshold, a transform-bearing job runs full-frame; `limit` N returns exactly N rows; a dedupe across what would have been a chunk boundary removes the duplicate.
4. **Out-of-core.** Under `auto`, not selected, with `out_of_core_declined="per_table_transforms_present"`; explicit `out_of_core` raises before `profile_source` is called (poisoned reader and profiler prove no read).
5. **Direct chunked entry points reject.** `run_mask_pipeline_chunked`, the native-or-oracle dispatcher and the mask and native-or-oracle physical adapters raise `PlanCompileError(per_table_transforms_present)` for a transform-bearing table, given a poisoned chunk iterable that fails if consumed. The resident aggregator adapter, which takes a resident `pa.Table`, raises the same error before its slicing boundary runs (spy on the slicer and assert it was never called). Without transforms all of them run unchanged.
5b. **Planner fallback, not an exception.** `classify_job`, `capture_physical_plan_inputs` followed by `compile_physical_plan`, `run_pipeline(auto_chunk=True)` and `run_pipeline(explain_plan=True)` on a transform-bearing job each fall back to full-frame with the coded reason, and none raises.
6. **Unified slice and identity.** Unified slice still declines. Called directly, `apply_table_transforms` returns the identical Arrow object for a no-transform table and for a generate-kind table; the unified-slice suites pass unmodified.
7. **Admission.** For a relationship-bearing pure-mask job with transforms, including a `derive` that promotes int8 to int64 and float64: never admitted by static fit; with resident sources and default flags the probe runs on the transformed execution; with the probe skipped (lazy sources, flags off, raw bytes over budget) it stays on sequential or out-of-core routing. A large single-table (non-FK) transform-bearing job is routed full-frame (pinning the known limit). A 33-op transforms list fails validation.
8. **Exact preservation.** Fixtures: int8 through int64 and uint8 through uint64 (with and without nulls, small values, an all-null column, `2**63 + 1`); float columns holding both NaN and null, and a non-nullable float field holding NaN; string; bool; date32 and date64; timestamps in each unit, naive and with time zones, including values around a DST fold and pandas `NaT`-sentinel edge values; decimal128 with non-default precision and scale; dictionary columns with int8 and int32 indices, ordered and unordered, and a chunked column whose chunks carry different dictionaries; field metadata on several columns and table metadata including a `pandas` key. Also a source table written from a pandas DataFrame with a named, non-default and duplicate-label index and valid pandas metadata (not an arbitrary metadata value): an outcome-neutral transform (`limit` to the full row count) must give the same output column set, strategy-visible columns, schema and values as the same job with no transforms, the stored index field must not appear in the output or be visible to filter, derive or dedupe, and dedupe over all columns must match the no-transform frame's dedupe. Also null-free metadata-backed `Int8` and `UInt8` columns and wider nullable integers at their arithmetic boundaries (e.g. `x + 1` at the type maximum) through filter and derive: results equal today's platform path. For each op alone, every adjacent pair of ops, and a longer chain (`filter`, stable `sort` with duplicate keys, `limit`, `dedupe` on all columns and on a subset including dictionary and time zone columns, `drop_column`, `derive`): after every op the frame's index equals the expected source ordinals, the selected source ordinals equal an independent reference computed in the test, every surviving column equals `take(source_column, ordinals)` exactly (values, validity, NaN bits), and fields compare equal with metadata checking enabled (`Field.equals(..., check_metadata=True)`); table metadata follows rule 4; derived columns carry inferred types. Name reuse: `drop_column x` then `derive x` emits the derived values (not the dropped source values); `derive y` then `drop_column y` emits no `y`; a repeated drop/derive chain on one name emits only the final binding; column order follows the final bindings. Null-free integer columns stay numpy-typed during evaluation (spy on the dtypes the ops receive).
9. **Invalid transforms** (derive onto an existing column, drop of a missing column, sort on a missing column) raise the existing `TransformError` codes. Full-frame writes no output, quarantine, vault or manifest. Sequential commits and publishes nothing; a plain callable sink may already hold earlier tables (documented limitation of non-transactional sinks, `_sequential.py` ~229-241).
10. **Public surface.** `from decoy_engine import apply_table_transforms` works and is in `__all__` and the compatibility contract; Plan-level API docstrings state the prepared-input contract.
11. **Derived-column masking** keeps today's outcome (characterization only, not red-before).

Platform (A8p-0): a packaging test resolves each of the four platform/engine combinations and shows the two bad ones fail to install; the rollback pairing is exercised the same way. Platform (A8p): main run, sequential (including a vault-bearing job with filter and sort, checking vault alignment), pipeline preview, node preview and bounded-child preview each apply transforms exactly once and match the pre-change platform output except for the recorded type-preservation correction. A8p-0 otherwise passes the platform suite unchanged.

## Out of scope

Generate-side transforms; running transforms on the Rust lane or the compiled chunked dispatcher (a later Phase B/C item may admit row-local ops there); transform timing in `ExecutionResult`.

## Gates

Codex plan-gate (re-gate of this revision), Sonnet build, dennis, Codex final, for A8 and for each platform branch. Merge order: A8p-0, then A8 plus the engine release, then A8p. A8 merges under the standing Rust rule only after A8p-0 is merged; A8p-0 and A8p need Cam's go.

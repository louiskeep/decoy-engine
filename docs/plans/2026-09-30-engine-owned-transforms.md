# Engine-owned table transforms (Rust engine program A8)

Status: plan (revision 4: folds the Codex plan-gate NO-GO on revisions 1 to 3)

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

- Derived-column width is treated as unpriceable: pandas and numexpr can promote narrow inputs (an int8 expression can yield int64 or float64), so no raw-profile formula is a safe upper bound. A transform-bearing job is never admitted by the raw static-fit shortcut. When the measured probe runs (resident sources, default flags, raw bytes not already over budget; `_pipeline_routing_signals.py` ~411-443), it measures the transformed execution. When the probe is skipped or inconclusive, the job stays on bounded routing, the same as any job the probe cannot confirm.
- `TableConfig.transforms` gets a maximum length of 32 ops (pre-GA hard change) so width growth is bounded.

## Arrow/pandas conversion

Invariant: a table with transforms masks exactly as the same data would without them. The round-trip through pandas must not change the Arrow type of any column the transforms leave alone.

The platform's current bare `table.to_pandas()` breaks that for every nullable integer column, not only FK keys: any integer column with a null becomes float64, stays float64 after `pa.Table.from_pandas`, and loses exactness above 2^53. That also changes downstream behavior, because the strategies and `reject_null_bearing_int` (`_pandas_adapter.py` ~191) see a float column instead of a nullable integer one, and the adapter's own lossless sets (FK keys, `date_shift.group_by`, `top_code`, `group_key.group_by`, ~193-215) are defeated because the precision is already gone before the adapter runs.

Decision: `apply_table_transforms` does three things.

1. Snapshots the source table's Arrow schema.
2. Converts with `to_pandas_fk_safe(table, <every Arrow integer column that contains a null>)`, collected from the schema and each column's null count. Each protected column maps to its own same-width, same-signedness pandas nullable dtype. That set is a superset of the adapter's four protected sets whenever they can lose precision (a null-free integer column never widens), so none of them can be defeated upstream. Null-free integer columns keep plain numpy dtypes, so numexpr evaluation of filter and derive is unchanged for them (extension dtypes push `_eval_clamped` onto the Python engine, `_transforms.py` ~61).
3. After the ops, rebuilds the Arrow table with an explicit schema: every surviving original column gets its exact original Arrow field (type, including decimal precision and scale, dictionary index width and ordered flag, timestamp unit and time zone, date32 versus date64; and nullability), cast from the pandas result; only columns created by `derive` are inferred. filter, sort, limit and dedupe only select or reorder rows, and drop_column only removes columns, so casting surviving columns back to their original type is exact. A cast that would fail or lose data raises a `TransformError` rather than silently changing the type.

Compatibility delta against today's platform, for transform-bearing tables only: every nullable integer column (any width, signed or unsigned, including small values like `[1, null]` and all-null integer columns) keeps its integer type instead of becoming float64; decimal, dictionary and temporal columns keep their exact Arrow type instead of the type pandas inference picks; values above 2^53 are also no longer rounded; and a hash, truncate or categorical strategy on a nullable integer column now hits the same `reject_null_bearing_int` rejection it already hits on tables without transforms. Tables without transforms are unaffected. This goes in the CHANGELOG and is flagged to Cam at merge.

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
7. **Admission.** A transform-bearing job, including one whose `derive` promotes an int8 input to int64 and float64, is never admitted by static fit. With resident sources and default flags the probe runs on the transformed execution; with the probe skipped (lazy sources, flags off, raw bytes over budget) the job stays on bounded routing. A 33-op transforms list fails validation.
8. **Type preservation.** For a table covering int8 through int64, uint8 through uint64 (each with and without nulls, small values, an all-null column, `2**63 + 1`), float, string, bool, date32, date64, timestamps in each unit with and without a time zone, decimal128 with non-default precision and scale, and dictionary columns with int8 and int32 indices and ordered and unordered flags, a transform list that touches none of them (e.g. `limit`, then `sort`, then `drop_column` of one other column) leaves every surviving column's Arrow field (type and nullability) and values unchanged, and a `derive` column is added with an inferred type. Null-free integer columns stay numpy-typed during evaluation (spy on the dtypes `apply_transforms` receives). Same for FK parent and child keys (single and composite, under each orphan policy), `top_code`, `date_shift.group_by` and `group_key.group_by` columns carrying nullable large integers, each through `run_pipeline` with transforms. No pandas index leaks into the output schema.
9. **Invalid transforms** (derive onto an existing column, drop of a missing column, sort on a missing column) raise the existing `TransformError` codes. Full-frame writes no output, quarantine, vault or manifest. Sequential commits and publishes nothing; a plain callable sink may already hold earlier tables (documented limitation of non-transactional sinks, `_sequential.py` ~229-241).
10. **Public surface.** `from decoy_engine import apply_table_transforms` works and is in `__all__` and the compatibility contract; Plan-level API docstrings state the prepared-input contract.
11. **Derived-column masking** keeps today's outcome (characterization only, not red-before).

Platform (A8p-0): a packaging test resolves each of the four platform/engine combinations and shows the two bad ones fail to install; the rollback pairing is exercised the same way. Platform (A8p): main run, sequential (including a vault-bearing job with filter and sort, checking vault alignment), pipeline preview, node preview and bounded-child preview each apply transforms exactly once and match the pre-change platform output except for the recorded type-preservation correction. A8p-0 otherwise passes the platform suite unchanged.

## Out of scope

Generate-side transforms; running transforms on the Rust lane or the compiled chunked dispatcher (a later Phase B/C item may admit row-local ops there); transform timing in `ExecutionResult`.

## Gates

Codex plan-gate (re-gate of this revision), Sonnet build, dennis, Codex final, for A8 and for each platform branch. Merge order: A8p-0, then A8 plus the engine release, then A8p. A8 merges under the standing Rust rule only after A8p-0 is merged; A8p-0 and A8p need Cam's go.

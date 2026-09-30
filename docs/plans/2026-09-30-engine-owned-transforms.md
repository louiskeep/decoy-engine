# Engine-owned table transforms (Rust engine program A8)

Status: plan (revision 2: folds the Codex plan-gate NO-GO on revision 1)

Date: 2026-09-30. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase A, item A8 (Cam decision 2, 2026-09-30: transforms move into the engine's `run_pipeline` so the engine, CLI and platform behave the same). Branch `feat/engine-owned-transforms`, off engine main `8dc559e5`. Paired platform work: A8p-0 and A8p below, each its own platform branch.

## Problem

A mask table's `transforms` block (filter, sort, limit, dedupe, derive, drop_column; `config/_transforms.py`, field `TableConfig.transforms`, `config/_tables.py` ~279-328) passes engine validation but no engine code path applies it. The only production caller of `execution._transforms.apply_transforms` is the platform: `api/jobs/v2_runner.py::_apply_per_table_transforms` (~162-211), which `v2_sequential.py` (~124-135, and again for vault reload ~221-237) and `v2_preview.py` (~125-135) reuse.

So one valid config gives different output by caller. The platform filters, derives and drops; `decoy_engine.run_pipeline`, the CLI and `decoy.mask(config=...)` mask the untransformed table without a warning. The `_transforms.py` module docstring names a call site that does not exist.

## Ownership contract

- **`run_pipeline` owns transforms.** It applies each mask table's transforms exactly once, over the whole table, before any strategy reads it, or it rejects the job with a coded error. Callers hand it raw sources.
- **Plan-level APIs take prepared inputs.** `PandasExecutionAdapter.run`, `run_sequential` and `run_fk_out_of_core` receive a `Plan`, not the config, so they cannot see transforms. Their docstrings state that inputs must already be transformed; callers that use them directly apply the public helper themselves.
- **Config-taking entry points outside `run_pipeline` reject.** `run_mask_pipeline_chunked` (`_chunked.py` ~358, public from `decoy_engine.execution` and top level) and everything that forwards the config to it (`physical/drivers/_chunked.py` ~58-88, `native/_dispatch.py` ~483-525) fail with `TransformError(code="per_table_transforms_present")` from `check_chunked_compatibility` (~250) when the target table declares transforms. Zero applications, no silent drop.
- **Public helper.** `decoy_engine.apply_table_transforms(config, table_name, table: pa.Table) -> pa.Table`, exported in `__all__` and the compatibility contract. It returns the same object when the table has no transforms or is generate-kind, and validates ops through `TableConfig`.

## Routes inside `run_pipeline`

| Route | Behavior for a mask table with transforms |
|---|---|
| Full-frame (incl. generate+mask, isolated runs, which call `run_pipeline`) | Applied once in `_pipeline_sources.resolve_resident_sources` |
| Auto-chunk | Ineligible: `decide_chunk_route` does not chunk a job with any transform-bearing mask table; it runs full-frame. Whole-table sort, limit and dedupe cannot be chunk-local, and a resident preprocess followed by chunking would not bound memory anyway. |
| Sequential | Applied once per table on load, by wrapping the loader in `_psrc.resolve_sequential_loader` |
| Out-of-core, `auto` | Declined; falls back by existing rules. `route_reason` keeps its current value; a new telemetry field `out_of_core_declined` carries `per_table_transforms_present` so the cause is visible |
| Out-of-core, explicit | A config-only preflight before `profile_source` raises `per_table_transforms_present`, so nothing is read |
| Unified slice | Declines, unchanged (`_unified_slice_admission.py` ~307-309) |

## Memory admission

Routing runs on the raw profile before transforms. Row counts stay conservative (filter, limit and dedupe only shrink; sort, drop and derive keep the count), but `derive` widens rows and the raw byte estimate (`_pipeline_routing_signals.py` ~236-312) does not see derived columns.

- The byte estimate adds, per `derive` op, the per-row bytes of the widest raw column in that table.
- A transform-bearing job cannot be confirmed by the raw static-fit shortcut; it goes through the existing measured path.
- `TableConfig.transforms` gets a maximum length of 32 ops (pre-GA hard change) so width growth is bounded.

## Arrow/pandas conversion

The platform today converts with bare `table.to_pandas()` and back with `pa.Table.from_pandas(preserve_index=False)`. For a nullable unsigned 64-bit FK column holding values above 2^53, that route turns the column into float64 and rounds the keys (Codex reproduced it with `2**63 + 1`), which breaks joinability.

Decision: the engine helper converts with the engine's FK-safe path (`_fk_keys.to_pandas_fk_safe`, passing the table's FK columns from the relationship config) and keeps the platform's `preserve_index=False` on the way back. That is an intentional correction, not byte parity. It changes output only for nullable integer FK columns whose values pandas cannot hold exactly in float64. Every other column is byte-identical to the platform's current behavior. The correction is recorded in the CHANGELOG and flagged to Cam at merge.

## Platform work (decoy-platform, Cam's go to merge)

**Version skew.** The platform declares `decoy-engine>=0.5.0` with no lockfile, so an old platform can install the new engine and apply transforms twice. The four combinations:

| Platform | Engine | Result | Prevented by |
|---|---|---|---|
| old | old | platform applies, correct | |
| old | new | double application | A8p-0 upper bound |
| new | old | never applied | A8p minimum version |
| new | new | engine applies, correct | |

- **A8p-0 (ships first, before A8 releases):** cap `decoy-engine<V`, where V is the engine release containing A8. No behavior change.
- **A8p (ships with V):** raise the minimum to `>=V` and remove the cap, and change the call sites:
  - Main run (`v2_runner.py` ~283-300): stop preprocessing; pass raw sources to `run_pipeline`.
  - Sequential (`v2_sequential.py`), which calls `run_sequential` directly: keep calling a transform on load, now the public `apply_table_transforms`, in both the execution loader and the vault-reload loader, so vault rows stay aligned with filtered, sorted, deduped or limited output.
  - Preview (`v2_preview.py`): transform the full source with `apply_table_transforms`, slice, then pass `run_pipeline` a config copy with that table's `transforms` cleared.
  - Delete `_apply_per_table_transforms` and the platform's private import of `decoy_engine.execution._transforms`.
- **Rollback:** roll back the platform to A8p-0 and the engine to the release before V together. Rolling back one side alone is blocked by the version bounds (install fails rather than running wrong).

## Acceptance tests (written first; each behavioral test records its red-before output)

Engine (A8):

1. **Applied once, correct output.** For each op and a chained sequence, `run_pipeline` output equals an independent oracle: raw source, then `apply_transforms` via the platform's own conversion, then `run_pipeline` with transforms cleared. Compare full Arrow schema and values, not row counts. Routes: resident full-frame, generate+mask (mask table), sequential with transforms on parent and child, isolated execution.
2. **Exactly once.** Spy on `apply_transforms`: one call per transform-bearing mask table on each route in test 1; zero calls on declined or rejected routes (test 4-6).
3. **Auto-chunk ineligible.** With `auto_chunk=True` above the threshold, a transform-bearing job runs full-frame; `limit` N returns exactly N rows; a dedupe across what would have been a chunk boundary removes the duplicate.
4. **Out-of-core.** Under `auto`, not selected, with `out_of_core_declined="per_table_transforms_present"`; explicit `out_of_core` raises before `profile_source` is called (poisoned reader and profiler prove no read).
5. **Direct chunked entry points reject.** `run_mask_pipeline_chunked`, the native-or-oracle chunked dispatcher and the physical chunked driver each raise `per_table_transforms_present` for a transform-bearing table, and succeed unchanged without transforms.
6. **Unified slice.** Still declines; no-transform and generate-kind tables pass through as the identical Arrow object, and the unified-slice suites pass unmodified.
7. **Admission.** A table with several wide `derive` ops gets a higher byte estimate than its raw profile and is not confirmed by static fit; a 33-op transforms list fails validation.
8. **Conversion.** Nullable int64 and uint64 FK columns (including `2**63 + 1` and nulls, and a composite key) keep exact values and types; non-FK columns match the bare platform conversion byte for byte; no pandas index leaks into the output schema.
9. **Invalid transforms** (derive onto an existing column, drop of a missing column, sort on a missing column) raise the existing `TransformError` codes. Full-frame writes no output, quarantine, vault or manifest. Sequential commits and publishes nothing; a plain callable sink may already hold earlier tables (documented limitation of non-transactional sinks, `_sequential.py` ~229-241).
10. **Public surface.** `from decoy_engine import apply_table_transforms` works and is in `__all__` and the compatibility contract; Plan-level API docstrings state the prepared-input contract.
11. **Derived-column masking** keeps today's outcome (characterization only, not red-before).

Platform (A8p): main run, sequential (including a vault-bearing job with filter and sort, checking vault alignment), pipeline preview, node preview and bounded-child preview each apply transforms exactly once and match the pre-change platform output except for the recorded FK conversion correction. A8p-0: the cap is present and the suite passes unchanged.

## Out of scope

Generate-side transforms; running transforms on the Rust lane or the compiled chunked dispatcher (a later Phase B/C item may admit row-local ops there); transform timing in `ExecutionResult`.

## Gates

Codex plan-gate (re-gate of this revision), Sonnet build, dennis, Codex final, for A8 and for each platform branch. Merge order: A8p-0, then A8 plus the engine release, then A8p. A8 merges under the standing Rust rule only after A8p-0 is merged; A8p-0 and A8p need Cam's go.

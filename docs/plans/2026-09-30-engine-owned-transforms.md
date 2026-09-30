# Engine-owned table transforms (Rust engine program A8)

Status: plan (revision 1)

Date: 2026-09-30. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase A, item A8 (Cam decision 2, 2026-09-30: transforms move into the engine's `run_pipeline` so the engine, CLI and platform behave the same). Branch `feat/engine-owned-transforms`, off engine main `8dc559e5`. A paired platform change (A8p) follows in decoy-platform.

## Problem

A mask table's `transforms` block (filter, sort, limit, dedupe, derive, drop_column; `config/_transforms.py`, validated on `TableConfig.transforms`, `config/_tables.py` ~282-328) is accepted by the engine schema but never executed by the engine. The only caller of `execution._transforms.apply_transforms` is the platform (`api/jobs/v2_runner.py::_apply_per_table_transforms`, reused by `v2_sequential.py` ~127 and `v2_preview.py` ~133), which converts each mask table Arrow to pandas, applies the ops, and converts back before calling `run_pipeline`.

So the same valid config gives different output depending on the caller: the platform filters, derives and drops columns; `decoy_engine.run_pipeline`, the CLI (`decoy run`) and `decoy.mask(config=...)` silently mask the untransformed table. The `_transforms.py` module docstring ("called by the mask path's PandasExecutionAdapter") describes a call site that does not exist.

The platform also owns the route rules that make transforms safe: Phase 1 streaming rejects them (`_phase1_eligibility.py` ~154), out-of-core rejects them (`v2_out_of_core.py` ~184, `admission_fk.py` ~390), and unified-slice admission declines them (engine `_unified_slice_admission.py` ~308). Inside the engine only the last one exists; the engine's own out-of-core route selection (`_pipeline_routing.resolve_execution_route`) has no transforms check, so an engine-direct FK job with transforms can take out-of-core and drop them.

## Design

One engine function owns transform application, and every engine route either applies it exactly once over the whole table before any strategy sees the data, or is ineligible for a table that declares transforms.

1. `execution/_transforms.py` gains `apply_table_transforms(config, table_name, table: pa.Table) -> pa.Table`: looks up the table config, returns the input unchanged (same object) when the table has no transforms or is generate-kind, otherwise converts with the existing FK-safe Arrow-to-pandas helper used by the mask path, runs `apply_transforms`, and converts back with `preserve_index=False`. The Arrow/pandas round-trip matches the platform's today, so outputs stay byte-identical to current platform behavior. Ops are validated through `TableConfig`, not re-parsed from raw dicts.
2. **Full-frame and auto-chunk.** `_pipeline_sources.resolve_resident_sources` applies it to each materialized mask table. Auto-chunk slices after that point, so sort, limit and dedupe see the whole table.
3. **Sequential.** The sequential route loads each table whole through its loader; wrap the loader so each mask table is transformed once on load (`_psrc.resolve_sequential_loader`).
4. **Out-of-core.** Ineligible for any job with a mask table that declares transforms: add a coded reason (`per_table_transforms_present`, the platform's existing string) to the engine's out-of-core eligibility in `_pipeline_routing`. With `execution_mode=auto` the job falls back to sequential or full-frame by the existing rules; an explicit `out_of_core` request fails before any read, with that reason.
5. **Unified slice.** Admission keeps declining tables with transforms (unchanged). Widening it is a later Phase B/C item, since a filter or derive before the Rust lane is cheap but sort/dedupe/limit are whole-table.
6. **Other entry points.** Any engine path that masks caller data outside `run_pipeline` (search for direct uses of the mask adapter on caller sources, for example preview helpers) either routes through the same function or is listed in the build report as not taking transforms, with a test.
7. **Public surface.** Export `apply_table_transforms` from `decoy_engine` (and the compatibility contract's public list) so the platform preview (`v2_preview.py`, which slices sources for a sample and never calls `run_pipeline` on the transformed full table) uses the engine function instead of the private module.
8. Fix the stale `_transforms.py` docstring to name the real call sites.

Profiling is unchanged: `profile_source` profiles the declared file sources before transforms, exactly as the platform path does today (the platform transforms caller tables after the engine has the config, and the engine still profiles the raw files). A config whose mask strategy targets a derived column behaves the same as today on the platform; that case is not widened here and gets a pinning test either way.

### Platform pairing (A8p, separate plan, decoy-platform)

Removing the platform's own application is required, not optional: after this engine change a platform that still transforms before calling `run_pipeline` would apply every op twice (a second derive fails on "column already present", a second drop_column fails on "not in table", a second limit or filter silently narrows again). A8p deletes `_apply_per_table_transforms` from the run and sequential paths, switches preview to the public engine function, and raises the platform's minimum engine version to the release that contains A8. A8 and A8p ship in the same Phase A paired release; A8 must not reach a platform environment without A8p.

## Acceptance tests (written first)

1. Engine-direct `run_pipeline` with each op type (filter, sort, limit, dedupe, derive, drop_column, and a chained sequence) on a mask table produces the same output as the platform's current pre-transform path (reference: apply `apply_transforms` to the source, then run `run_pipeline` on the transformed table with no transforms in config). Fails before the change.
2. The same through the sequential route (FK job, `execution_mode=sequential` and `auto`), with transforms on both parent and child tables. Fails before the change.
3. Auto-chunk (`auto_chunk=True`, table above the threshold, small chunk size): sort, limit and dedupe act on the whole table, not per chunk (a limit of N returns exactly N rows; a dedupe across a chunk boundary removes the duplicate).
4. Out-of-core: a transforms-bearing FK job is not selected for out-of-core under `auto` (route_reason names the coded reason), and explicit `out_of_core` fails before any source read with `per_table_transforms_present`.
5. Transforms are applied exactly once per table per run (spy on `apply_transforms`), on every route above.
6. Generate-kind tables and tables with no transforms pass through as the same Arrow object (identity, not equality), so unified-slice admission and its identity guard are unaffected; the unified-slice suites pass unmodified.
7. Invalid transforms (derive onto an existing column, drop_column of a missing column, sort on a missing column) raise the existing `TransformError` codes from `run_pipeline`, before any output table, quarantine, vault or manifest is written.
8. `from decoy_engine import apply_table_transforms` works and is listed in `__all__` and the compatibility contract.
9. A derived-column masking config behaves as today (pinning test of the current outcome, pass or coded failure).

Tests that need the companion carry `@_NEEDS_COMPANION`.

## Out of scope

Generate-side transforms; transforms on the Rust lane or the compiled chunked dispatcher (Phase B/C); transform timing in `ExecutionResult` (the platform's `phase_timing` transform bucket moves to A8p to decide).

## Gates

Codex plan-gate, Sonnet build, dennis, Codex final. Engine slice: merges under the standing Rust rule once CI is back, and only together with A8p (A8p's merge needs Cam's go).

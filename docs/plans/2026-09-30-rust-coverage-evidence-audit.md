# Rust coverage evidence audit

Status: plan

Date: 2026-09-30. Owner: consolidation loop, phase 3. Roadmap: decoy-platform `docs/ROADMAP.md`, "Order of work" step 1.

## Goal

Establish, by running real jobs, which job shapes and sizes execute on the Rust path today, which run on pandas or Python kernels, and which are not built. The output is a capability map and a gap list ranked by one question: does closing this gap move a real job size or shape onto Rust? The Rust engine program plan (phase 4) is built from that list, so every cell must rest on a recorded run or a cited line of code, not on docs or memory.

This is not §AUDIT (the pre-release security and code audit under `~/dev-rules/pre-release-audit.md`). It does not edit production code.

## The map

Rows are job shapes, columns are size tiers, and each cell records the route taken and what did the math.

Job shapes:
1. single-table mask
2. single-table generate
3. single-table mask + generate
4. multi-table mask, no relationships
5. FK mask (pure mask)
6. FK mask with validators / vault / fidelity / post-validation
7. FK with generate tables (generate-only and mixed mask + generate)

Size tiers: under 100k; 100k to 5M; 5M to 100M+ (as the routing sees it).

Axes recorded per cell:
- input format: Parquet, CSV, fixed-width
- output format: Parquet, CSV, fixed-width
- entry point: engine direct (`decoy_engine.run_pipeline`) and platform (`api.jobs.v2_runner.run_v2_pipeline_from_config`, which applies `admission_fk.py`'s own FK routing)
- masking strategy families: the 8 native operators, and at least one non-native strategy (Faker, FPE) to show where a single column forces the whole job off Rust

Cell values:
- **Rust**: `unified_slice_activation` present, every node `executed`, and hash nodes `compiled_kernel_executed=True`.
- **Rust (partial)**: some operators native, the rest Python.
- **pandas** / **Python-Arrow** (out-of-core per-batch kernels) / **DuckDB+Python** (OOC FK).
- **shadow only**: a Rust implementation exists but only runs in shadow parity tests.
- **not built** / **rejected** (the job refuses at this size).

## Method

### Evidence recorded per run
- the route: `quality_metrics["execution"]["execution_mode"]` and `route_reason`, `quality_metrics["auto_chunk"]` when present, `unified_slice_activation` when present, and the out-of-core / sequential markers;
- output byte-parity against the pandas oracle (`unified_slice_enabled=False`) wherever the Rust lane admitted the job;
- wall time and peak RSS (`resource.getrusage`) for each run, and the input row count;
- the exact commit (engine main, platform main) and the command.

### Scale on a 12 GB devbox
Real row counts are used up to ~1M rows (one heavy process at a time, per the devbox memory rule). Routing above that is exercised by lowering the routing knobs so small data takes the large-tier route: `auto_chunk_threshold_rows`, `out_of_core_threshold_rows`, `full_frame_reject_rows`, and the platform's `OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS`. Each such cell is labelled "routed by knob" and paired with a code citation showing that the default threshold routes a real job of that size the same way. Byte-estimate routing (the engine default) is exercised by passing a controlled memory budget, not by allocating the real data.

The 100M tier is established by code reading plus the knob runs; it is not run on the devbox. Absolute peak memory at 100M is left to the phase-4 GCP proof.

### Platform-route coverage
FK jobs without generation are routed by the platform (`api/jobs/admission_fk.py`), not by the engine's `decide_execution_route`. Every FK cell is therefore run through both entry points, and a cell is only marked by what the platform path actually does.

### Specific checks carried in from phase 1 and 2
1. **Cloud descriptor keys.** Confirm or refute that S3/GCS job descriptors carrying `connection_id` / `connection_name` (and `region` for GCS) fail engine validation. Run a real job through `run_v2_pipeline_from_config` against a local fake S3 (moto server in-process) and a stored snapshot built by `resolve_binding`. Record pass/fail and the exact error.
2. **Rust timings gap.** Confirm the Rust lane's `timings=()` shows as 0 ms in the platform job record.
3. **Output formats.** For each Rust-admitted cell, write CSV and Parquet output through the platform output layer and confirm byte-equivalence with the pandas route's output; record that fixed-width output is unsupported.
4. **Non-native strategy fallout.** One Faker or FPE column in an otherwise native job: record whether the whole table leaves the Rust lane.
5. **Upload ownership.** Re-verify the cross-owner binding finding by reading the code path only (no exploit run).

### Independent cross-check
Codex runs a separate, code-reading audit of the same map in parallel (it does not see the probe results first). Disagreements between the two are resolved by running the disputed cell.

## Deliverables

1. `docs/records/2026-09-30-rust-coverage-evidence-audit.md` (Status: record) in decoy-engine: the map, one row per shape, each cell citing its run id or code line.
2. The raw run log (JSON lines: shape, tier, formats, entry point, knobs, route evidence, parity, wall, peak RSS, commits) committed beside it.
3. The probe script, committed under `scripts/audit/` so the map can be regenerated after each engine slice.
4. A ranked gap list: each gap with the shapes/sizes it unlocks, the evidence, whether a shadow implementation exists, and rough size (port, wire-up, or new build).
5. A plain-language summary for Cam (Slack) and a roadmap update.

## Acceptance criteria

- Every cell of the map has a value and a citation (run id or file:line). No cell rests on docs or memory alone.
- Every "Rust" cell is backed by a byte-parity check against the pandas oracle and by kernel-execution evidence.
- Every FK cell reflects the platform route, not only the engine route.
- Every "routed by knob" cell has a code citation tying it to the default-threshold behavior.
- The cloud descriptor question is answered yes or no with the exact error or a passing run.
- The Codex cross-check is done and every disagreement is resolved by a run.
- dennis reviews the record for unsupported claims before it feeds the phase-4 plan.

## Failure modes to guard against

- Reading "admitted" from docs or from a flag instead of from `unified_slice_activation` in the result.
- Testing only the engine entry point and missing the platform's FK override.
- Treating a knob-routed small run as proof of 100M behavior without the code citation.
- A probe that silently falls back to pandas and is recorded as success because output matched (parity alone does not prove the route).
- Running several heavy probes at once on the devbox.

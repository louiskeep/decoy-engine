# Rust coverage evidence audit

Status: plan (revision 3: folds the Codex plan-gate NO-GO of revision 1 and GO-with-revisions of revision 2)

Date: 2026-09-30. Owner: consolidation loop, phase 3. Roadmap: decoy-platform `docs/ROADMAP.md`, "Order of work" step 1.

## Goal

Establish, by running real jobs through the real entry points, what each job actually executes on today: the Rust companion, Arrow/Python "native" kernels, or pandas. The output is a capability map and a gap list ranked by one question: does closing this gap move a real job onto Rust? The phase-4 Rust engine program is planned from that list, so every cell must rest on a recorded run or a cited line of code, never on docs or memory.

This is not §AUDIT (the pre-release security and code audit under `~/dev-rules/pre-release-audit.md`). It does not edit production code.

## Pinned commits

- decoy-engine `8dc559e5` (main after #180).
- decoy-platform `origin/main` at probe time, in a fresh detached worktree (the main checkout lags origin). The commit is recorded in every run.
- decoy-cli: one commit, in a detached worktree; its SHA is recorded.
- The native companion is built from the pinned engine tree; every run records the wheel/module SHA-256, module path, version and ABI (`native_companion_status()`), not only the engine commit.

## Environment preflight

Before any run, check and record: Postgres reachable (a local `postgres:15` container is available), Docker, the cgroup job supervisor socket, and cgroup delegation. Cells whose entry point needs a missing prerequisite (the adaptive-scheduler worker needs the supervisor and cgroups) are assigned to an integration host. That is a stop-and-ask to Cam if it needs new spend; it is never recorded as "untested".

## Dimensions

| Dimension | Values |
|---|---|
| Operation | mask, generate, mask + generate, subset-only, subset then mask |
| Strategy / generator family | the native mask operators (hash, categorical, bucket_perturb, group_key, date_shift, redact, truncate, passthrough); Faker (pooled and non-pooled); FPE (FF1); text_mask / text_redact; each generator family the registry exposes; at least one custom/unsupported strategy |
| Relationship topology | none; multi-table no relationships; FK tree; FK diamond; self-referential FK; cross-table FK cycle |
| Gates | validators, quarantine, vault writer, fidelity / post-validation, STORM, transforms, column `when` gate absent/present |
| Size tier | under 100k; 100k to 5M; 5M to 100M+ (as each routing layer sees it) |
| Input | resident Arrow vs lazy / loader-backed; Parquet, CSV, fixed-width |
| Transport | local file, S3, GCS (source and target) |
| Output format | Parquet, CSV. Fixed-width output is rejected by the target config schema (`config/_targets.py`); recorded once as a known gap, not probed per cell |
| Entry point | (1) engine direct `decoy_engine.run_pipeline`; (2) platform full-frame wrapper; (3) the real claim -> worker path, including `phase1_streaming_tables` set at claim; (4) the adaptive-scheduler worker consuming an immutable `DispatchPlan` (flag on); (5) the CLI in its default, `--native`, `--no-native` and `--chunked` modes where valid; (6) subset. Each of the six gets at least one real completed run. |

The full cross-product is not run. Each dimension is varied against a baseline cell (single-table hash mask, Parquet, local, under 100k, engine direct), plus the combinations the routing code actually branches on.

**Branch-witness ledger (written before any run).** A list of every routing and admission predicate outcome in the engine and platform code, each with file:line. Every outcome needs an executed witness run or an explicit code-inferred cell with its reason. At minimum: unified-lane cheap and resident-contract gates; auto-chunk and chunked admission; byte-estimate and micro-probe outcomes; sequential vs out-of-core vs full-frame vs reject; out-of-core `batch_join` vs `reorder` (sink, memory and disk budgets, deduplicated parent-key count, fan-in, row width); self-FK vs cross-table cycle; `when` present; categorical deterministic vs not; date_shift format and prepass; group_key sibling masking and type; the platform FK admission, runtime out-of-core, claim-time streaming and DispatchPlan decisions; the CLI mode switches.

## What a cell records

Per execution node, the backend that did the work:
- **Rust companion**: hash, categorical, bucket_perturb, group_key and date_shift nodes with per-node `compiled_kernel_executed=True`; chunked Faker with `pool_select_executed=True` and `pool_select_calls > 0`.
- **Arrow/Python native**: passthrough, redact and truncate on the unified lane run as Arrow/Python kernels (`native/_kernels_scalar.py`), not Rust.
- **pandas**: the legacy adapter.
- **mixed**: a job whose nodes span backends.

Faker pool selection is recorded separately, distinguishing a production-connected compiled path from shadow-only or low-level-only execution.

Each job is recorded by stage, because stages can run on different backends: subset (Polars preprocessing), generation (Python/NumPy/Faker), mask nodes (as above), relational and out-of-core work (DuckDB + Python), output transport, and rejection (no execution, with the rejecting gate).

Per cell, the route as each layer decided it: the engine's `quality_metrics["execution"]` (`execution_mode`, `route_reason`), `auto_chunk`, `unified_slice_activation`, the out-of-core / sequential markers; and on the platform the admission classification, the claim-time `phase1_streaming_tables` plan, the `DispatchPlan` route, and the runtime out-of-core decision, all read back from the persisted job, not from the knobs set.

Per run: output byte-parity against the pandas oracle where a non-pandas backend ran; wall time; peak memory; row count; commits; command. Peak memory is the worker's own `/proc/self/status` `VmHWM` (as the engine's isolated worker already reads it) or the job cgroup's `memory.peak`. `ru_maxrss` is not used: it survives `execve`, and the platform's persisted `peak_memory_mb` is process-wide, so it is labelled contaminated unless the worker lifecycle proves isolation.

## Scale on a 12 GB devbox

Real row counts up to ~1M, one heavy process at a time.

Large tiers are split into two separate claims:
- **route-equivalent**: the run took the same code path a real large job would. Reached by overriding the knobs each layer actually reads: engine `auto_chunk_threshold_rows`, `out_of_core_threshold_rows`, `full_frame_reject_rows`, the byte budget and memory detection used by byte-estimate routing (with probe on and off); platform `v2_out_of_core._OUT_OF_CORE_THRESHOLD_ROWS` (runtime) and `admission_fk.OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS` (admission pricing), overridden together with an assertion that admission and runtime agree; `streaming_min_input_mb` with a real claim so the streaming plan is created and persisted. The record lists every route predicate and its inputs: width estimates, budget source, probe result, Parquet footer counts, host/cgroup limit.
- **capacity-proven**: only for sizes actually run. 5M to 100M+ cells are labelled "route inferred; capacity unproven" until the phase-4 GCP run. Small fixtures do not exercise spill, cardinality, Arrow offset limits, row groups or disk admission, and the record says so.

## Specific checks

1. **Cloud descriptor keys**, as two separate tests:
   (a) a descriptor produced by `resolve_binding` for S3 and for GCS, stored and passed to `run_v2_pipeline_from_config`: assert whether validation rejects it, and that no cloud client or network call happens first;
   (b) a schema-valid descriptor with the platform-only keys stripped, run against an in-process moto S3 server end to end.
   (a) is not described as a moto test.
2. **Rust timings gap**: confirm the Rust lane's empty `timings` shows as 0 ms in the platform job record.
3. **Output**: for each non-pandas cell, CSV and Parquet output through the platform output layer, compared with the pandas route's output.
4. **Non-native fallout**: one Faker, FPE or text column in an otherwise native job; record whether the whole table leaves the Rust lane and on which gate.
5. **Platform streaming**: which engine code the claim-time `phase1_streaming_tables` path runs after engine #166 deleted the native streaming lane, and on what backend.
6. **Upload ownership**: re-verify the cross-owner binding finding by reading the code path only.

## Independent cross-check

Codex runs a separate, code-reading audit of the same dimensions in parallel, without seeing the probe results. Every disagreement is resolved by running the disputed cell.

## Deliverables

1. `docs/records/2026-09-30-rust-coverage-evidence-audit.md` (Status: record) in decoy-engine: the map, each cell citing its run id or file:line, with route-equivalent and capacity-proven kept separate.
2. The raw run log (JSON lines) committed beside it.
3. The probe harness under `scripts/audit/`, so the map can be regenerated after each engine slice.
4. A ranked gap list: each gap with the shapes and sizes it unlocks, the evidence, whether a shadow or historical (engine #166) implementation exists, and rough size (port, wire-up, or new build).
5. A plain-language summary for Cam and a roadmap update.

## Acceptance criteria

- Every cell has a value and a citation. No cell rests on docs or memory alone.
- "Rust companion" is claimed only for nodes with `compiled_kernel_executed=True`; Arrow/Python native is never reported as Rust.
- Every platform cell reflects the persisted claim/dispatch route. Each of the six entry points has at least one real completed run, locally or on an integration host.
- Every branch in the witness ledger has an executed witness or a cited code inference.
- Large-tier cells separate route-equivalent from capacity-proven, and 100M cells say "capacity unproven".
- The cloud question is answered by test (a), and test (b) shows whether a clean descriptor works.
- The Codex cross-check is done and every disagreement is resolved by a run.
- dennis reviews the record for unsupported claims before it feeds the phase-4 plan.

## Failure modes to guard against

- Reading "admitted" or "Rust" from docs, flags or knob settings instead of the persisted result.
- Counting Arrow/Python native kernels as Rust.
- Calling platform functions directly and missing claim-time or dispatch-time route decisions.
- Treating a knob-routed small run as proof of 100M capacity.
- Parity alone taken as proof of the route (a silent pandas fallback also matches).
- Several heavy probes at once on the devbox, or peak memory read from `ru_maxrss` or a long-lived process.
- A routing branch with neither a witness run nor a cited code inference.

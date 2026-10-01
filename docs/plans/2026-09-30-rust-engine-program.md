# Rust engine program

Status: plan (revision 3: folds the Codex plan-gate NO-GO of revision 1 and GO-with-revisions of revision 2; for Cam's review)

Date: 2026-09-30. Input: `docs/records/2026-09-30-rust-coverage-evidence-audit.md` (the evidence record; every "today" statement below cites it) and `docs/records/audit-2026-09-30/codex-independent-audit.md`. Roadmap: decoy-platform `docs/ROADMAP.md`, TOP PRIORITY and Order of work step 2.

## Goal and end state

A fast mask and generate engine at every size up to 100M+ rows, with **no pandas on admitted production routes**: masking runs on Rust companion kernels or on named Arrow kernels (redact, truncate, passthrough), and declared Faker-provider construction stays Python. Pandas remains the fallback for CLI users without the companion and the parity oracle for tests.

| Route | Data layer | Math today | Math at end state |
|---|---|---|---|
| Unified slice (full-frame, single table) | in memory | Rust for hash, categorical (deterministic), bucket_perturb, group_key, date_shift; Arrow/Py for redact, truncate, passthrough | plus Faker, FPE, text strategies, non-deterministic categorical, `when` |
| Chunked (engine auto-chunk, platform Phase 1, CLI `--chunked`) | platform: chunk-bounded; engine: resident input and output | pandas (deterministic Faker selection compiled) | Rust dispatcher; bounded input and output on every entry point |
| Engine independent multi-table | full-frame per table | pandas | Rust dispatch per table |
| Out-of-core FK | DuckDB scan, join, reorder, spill | Python kernels | Rust kernels; the final external-reorder design implemented |
| Sequential FK | table by table | pandas | Rust per table |
| Generation and mixed jobs | full-frame | Python/NumPy/Faker; masking in mixed jobs on pandas | bounded generation with an incremental sink; masking in mixed jobs on Rust |

## Rules for every slice

**Evidence.** Every positive fixture asserts, per column: `planned_backend`, `executed_backend`, call count, elapsed time, and zero unintended oracle fallbacks. Backend values distinguish `rust_companion`, `rust_pool_select`, `arrow_python` and `pandas_oracle`. CI fails if a column admitted to a Rust backend records zero compiled calls. Negative fixtures assert exactly one whole-table oracle route and its typed reason.

**Parity.** Deterministic slices require byte parity with the pandas oracle and thread-count invariance, covering values, Arrow schema and metadata, row and column order, warnings, errors, quality evidence, vault side effects, empty / all-null / ragged chunks, every supported dtype and several chunk sizes. Non-deterministic slices (C1b, C5b) cannot match an unseeded oracle byte for byte (`_strategies/_categorical.py:214`, `tests/parity/SEMANTIC_DIFFERENCES.md:42`); they require the same schema, null, order, error, evidence and side-effect invariants, plus a seeded test hook and a distributional test with its sample size, metric and tolerance declared before the build. No fallback may happen after output is published; a runtime failure aborts the transactional sink.

**Performance and memory.** Benchmarks run the candidate alone in an isolated process, on a frozen workload and thread budget, with warmup and repeated trials reported as p50 and p95. Each slice declares its absolute memory ceiling, with host headroom, before it is measured. The D9 ratio bar is dropped.

**Operations.** Platform activation requires backend, fallback reason, per-column timing and thread budget to cross the engine-to-platform boundary and appear in the job-detail and evidence APIs, under contract tests. Every activation and release declares: owner, exact artifact, exposure bound, health signals, observation window, numeric abort criteria, the disable / rollback command, and a post-disable verification.

**Release chain.** Every platform activation, in any phase, follows: engine and companion slice, exact artifacts, paired release, platform dependency pin, contract tests, bounded rollout.

**Docs.** Every slice updates the roadmap, the shipped log, the compatibility / support matrix and the affected public docs.

**Gates.** Opus plan, Codex plan-gate, Sonnet build, dennis, Codex final. A Rust slice merges under Cam's standing rule (dennis GO, Codex final GO, CI green, passes the applicable parity contract, faster, within its declared memory ceiling); anything short of that stops for Cam. Platform and CLI slices merge only with Cam's go. One writer per worktree; one heavy test process at a time on the devbox.

## Phase A: prerequisites (correctness and security)

| Slice | Content | Size | Notes |
|---|---|---|---|
| A1 | **Owner scope on upload bindings** (record P2): `resolve_binding` takes the authenticated user; a local upload must belong to that user or the user must be an admin, matching `api/files/router.py:781`; 404 otherwise. Connections stay org-wide (by design: `CloudAccount` is org-level and project access control is dormant); a test pins that. | S | HIGH security. Lands first. Built on the #74 branch, which restructures the same file. |
| A2 | **Cloud descriptors** (P1): remove `connection_id`, `connection_name` and GCS `region` from the executable `Job.yaml_snapshot`; keep them only in a separate binding-provenance / evidence record. Tests: the stored snapshot runs through `run_v2_pipeline_from_config` for S3 and GCS, source and target, against moto / fake-gcs-server. | S | After A1 for tidiness; no security dependency. |
| A3 | **Platform FK cycle routing** (P3): cycle check in `v2_sequential._should_use_sequential_relationship_path` and admission pricing. | S-M | Regression test from R063. Needed only before platform sequential activation (D2c). |
| A4 | **Phase 1 fixed-width** (P4): reject fixed-width in Phase 1 eligibility until a streaming reader exists. | S | |
| A5a | **Public fixed-width reader** (engine): export `read_fixed_width` through `decoy_engine.__all__` and the compatibility contract, so the CLI does not import private engine code. | S | Engine; ships in the Phase A paired release. |
| A5b | **CLI reads sources by declared format** (P5): one shared reader for run, demo and the Python API, a format-dispatching chunked iterator (fixed-width rejected under `--chunked`), and the CLI native gate widened to CSV and fixed-width (it hard-codes Parquet, while engine #180 admits all three). | S-M | decoy-cli; after the A5a release. |
| A6 | **Rust lane timings** (P6): the unified slice returns real per-column `timings` and `boundary_conversion_ms`; removes the engine #180 strict xfail. | S | |
| A7 | **Retire the D9 ratio check** (R14); keep an absolute-peak check. | S | Before B3. |
| A8 | **Transforms ownership** (P7): transforms move into the engine `run_pipeline` (Cam decision 2); engine, CLI and platform agree. Plan `docs/plans/2026-09-30-engine-owned-transforms.md`. | M | Ships with A8p-0 (platform engine-version cap, merges first) and A8p (platform call sites). |
| A9 | **Platform uses the public fixed-width reader**: switch the platform's three imports of `decoy_engine.profile._fixed_width_reader` (`api/fixed_width_layouts/router.py`, `api/jobs/preview_sample.py`, `api/jobs/v2_cloud_staging.py`) to `decoy_engine.read_fixed_width`. | S | Platform; after the A5a release. |
| A10 | **Fixed-width byte offsets**: the reader slices by character while `FixedWidthLayout` defines byte ranges, so a multibyte character shifts later columns. Switch to byte slicing (slice each line's bytes, decode per field, reject a split character), per Cam decision 8. (A5a already reads one binary line at a time, so exact decode-error lines and capped reads are done; A10 changes only the within-line slicing.) | S-M | Engine; before GA. Replaces the characterization test A5a added. |
| A11 | **Rename the masking-strategy package** `decoy_engine/transforms/` to `decoy_engine/strategies/`, so "transforms" means only table reshaping and code uses the config's word `strategy`. Pre-GA hard rename across engine, platform and CLI; compatibility contract updated. | M (mechanical) | Right after Phase A merges, before Phase B starts, so no open worktree conflicts. |

## Phase B: the chunked dispatcher in production (record R1)

Scope: large single-table and independent multi-table mask jobs on the compiled dispatcher (`run_native_or_oracle_chunked`) instead of pandas. On the tested 1M-row fixture: 5.9x to 8.4x faster, byte-identical (R074, R075). Platform Phase 1 covers hash, redact, truncate and passthrough. Deterministic-REUSE Faker is additionally available only to engine and CLI callers that satisfy its gates; platform Faker waits for its C admission slice.

| Slice | Content | Size |
|---|---|---|
| B1 | **Dispatcher production contract** (engine): accept what production callers pass (`registry`, `adapter`, `vault_writer`, `chunk_result_sink`, `base_row_offset`); reconcile output schema with the oracle route in the degenerate cases the parity gate currently allowlists; emit `ExecutionResult` timings and the per-column evidence above; map `NativeChunkSchemaDriftError` to a typed engine error; thread budget as a parameter (library default 1; production callers pass the admitted reservation). One public engine entry point. | M |
| B2 | **Engine auto-chunk on the dispatcher**, with the per-table oracle fallback. | S-M |
| B7 | **Engine `run_pipeline` independent multi-table dispatch**: each independent table goes to the dispatcher (today only platform Phase 1 handles several tables). | M |
| B8 | **Native admission for unconfigured passthrough columns** (engine): B1's dispatcher admits a table whose unconfigured columns are kept under the passthrough policy, carrying them as Arrow passthrough, instead of vetoing it to the oracle route (`uncovered_columns`). Partial-mask configs are common, and without this they run wholly on pandas. Before B3 and B4 activation, so platform and CLI jobs get the native route. | S-M |
| B5 | **Platform admission pricing and reservation** for native chunk cost and the thread budget. | S-M |
| B3 | **Platform Phase 1 on the dispatcher**, against an exact engine and companion artifact; the platform maps the new error and surfaces backend, reason, timing and threads on the job. | S-M |
| B4 | **CLI**: dependency and native-extra update to the released engine and companion; `--chunked` uses B1; `--native` stops rejecting chunked jobs (record R10) and reads per-column evidence; CLI release and clean-install smoke test. | S-M |
| B6a | **Engine auto-chunk incremental output sink**; input stays resident. | M |
| B6b | **`LazySource` batch input plus the incremental sink**, proving bounded input and output. | M |
| B6c | **Platform drops the sub-256-MiB eager route** for eligible jobs (record R7). Depends on A4, B3, B5. | S-M |

Phase B total: L.

## Phase C: operator coverage (record R2, R3, R6)

Each C slice depends only on B1's frozen contract. Each platform admission step (C9) depends on B3, B5 and the matching C slice.

| Slice | Content | Size |
|---|---|---|
| C1 | Chunked dispatcher: categorical (deterministic). | M |
| C1b | Non-deterministic categorical: define its semantics under chunking, then Rust execution. | M |
| C2 | Chunked dispatcher: bucket_perturb. | M |
| C3 | Chunked dispatcher: group_key (cross-chunk sibling state). | M |
| C4 | Chunked dispatcher: date_shift (prepass). | M |
| C5a | Pooled Faker on the unified slice. | M |
| C5b | Default (non-deterministic) Faker selection on the Rust paths. | M |
| C5c | Non-string Faker sources. | S-M |
| C6a | FPE (FF1) on the Rust paths. | M |
| C6b | text_mask on the Rust paths. | M |
| C6c | text_redact on the Rust paths. | M |
| C8 | `when` predicates without a whole-table pandas fallback (and expressible through `PipelineConfig`). | M |
| C9 | Platform Phase 1 admission, one slice per operator, driven by an engine-owned compatibility decision rather than a hardcoded platform list (record R6). | S each |

## Phase D: FK routes (record R4, R5, R9)

D1 and D2 engine work does not depend on A3; only platform sequential activation (D2c) does.

| Slice | Content | Size |
|---|---|---|
| D1 | Rust kernels for out-of-core FK payload masking; DuckDB keeps scan, join, reorder. Starts with hash, categorical, bucket_perturb. | L, ~1,000 to 2,000 LOC |
| D1b | Rust kernels for every remaining out-of-core-admitted operator. | M |
| D5 | Platform out-of-core activation of the Rust FK kernels: admission, exact artifact pin, contract tests, rollout. | S-M |
| D2a | Sequential FK executor contract, with hash on Rust. | L |
| D2b | Sequential FK: activation across FK shapes (diamond, self-FK, out-of-core-incompatible strategies). | M-L |
| D2c | Sequential FK: remaining operator families; platform sequential activation (after A3). | L |
| D3a | Platform out-of-core with validators keeps outputs bounded. | M |
| D3b | Platform out-of-core with the vault keeps outputs bounded. | M |
| D4a | Revalidate the existing final external-reorder design (`docs/plans/2026-07-22-ooc-b-external-reorder-implementation.md`) against current main; record only invalidating changes. No new survey. | S |
| D4b | Implement it in separately reviewed sorter, join, budgeting and route-wiring slices. | L |
| D4c | 100M and 200M parent-growth and child-growth proof with spill and peak-memory evidence. | M |

## Phase E: mask and FK 100M milestone

A GCP run at 1M, 10M and 100M rows through the platform worker (real claim, streaming plan), asserting per-column Rust evidence, byte parity at 1M, p50/p95 wall and the declared absolute peak. Uses the existing 50-run GCP budget; Slack before running.
- Single-table: depends on B1 to B7 and the C slices for the operators in the workload.
- FK tree: depends on D1, D4a then D4b then D4c, D5, and the exact engine artifact. D3a / D3b only if the milestone workload includes validators or the vault.

## Phase F: generation and mixed jobs

| Slice | Content | Size |
|---|---|---|
| F1 | Bounded generation with an incremental output sink. | L |
| F2 | Mixed mask + generate and FK + generate, with the masking routed through Rust. | L |
| F3 | Rust generation selection for pooled and deterministic generators (record R12). | M |
| F4 | Capacity gate for generation and mixed jobs at 1M, 10M and 100M. | M |

Then prodsim (roadmap Order of work step 4). Until the end state is reached, prodsim treats an unexpected fallback within the currently admitted matrix as a defect; expected declines stay tracked as coverage gaps.

## Out of this program: breadth backlog

Not part of this program's end state; each becomes its own planned and sized item on the roadmap later: fixed-width output (R13); streaming subset materialization (R11, L-XL); enforced post-validation on streaming, sequential and out-of-core routes (R8, L-XL); the adaptive-scheduler entry point on an integration host (scheduler S13 VERIFY).

## Order

1. Phase A: A1, then A2; A3, A4, A5, A6, A7 in parallel worktrees (one writer each). A8 with A8p-0 first and A8p after the release; A9 after the A5a release; A10 any time before GA. A11 once every Phase A branch has merged.
2. B1, then B2 and B7, then B8 (before B3 and B4 activation); merge and identify the exact engine and companion artifact; B5; B3 against that artifact; a paired engine and companion release; B4 (CLI dependency and release, clean-install smoke). B6a after B2, then B6b; B6c after A4, B3, B5.
3. C slices start as soon as B1's contract is frozen, in parallel; each C9 platform step after B3, B5 and its C slice.
4. D1 then D1b, and D4a then D4b then D4c, in parallel with C; D2 after D1's operator work; D5 after D1 and D4b; E after the dependencies listed there.
5. F after E: F1 then F2; F3 alongside; F4 after F1, F2, F3 and their released artifacts. The breadth backlog after F.

Rough scale: Phase A about a week; Phase B two to three weeks; Phase C three to five weeks (parallel); Phase D several weeks, with D2 and D4b the largest; F two to three weeks. Estimates from the record's sizes, not commitments.

## Decisions (Cam, 2026-09-30)

1. Order after Phase B: C, then D, then E, then F.
2. A8: transforms move into the engine `run_pipeline`, so engine, CLI and platform behave the same.
3. Cam's standing Rust merge rule covers every engine slice in Phases B to F. Platform and CLI slices, including the Phase A platform fixes, need Cam's go.
4. Phase E runs under the existing 50-run GCP budget, with a Slack message before each run.
5. The engine and native companion release together at each phase boundary, and the companion wheels are published so CLI users get the Rust path. Each publish is announced on Slack first.
6. A2b (added): the adaptive-scheduler claim path (`scheduler_claim_loop.claim_one_flag_on`) gets the same legacy cloud-job cancellation as the standard claim path.
7. A1/A2 claim loop: after two gate failures in the same code, the plan is remediated first (a re-select loop that excludes finished jobs, as the adaptive claimer does), gated by dennis and Codex, then rebuilt.
8. Masking-package rename (A11) goes on the roadmap for right after Phase A; A8's transform round-trip keeps every integer column's exact type (the output change is recorded in the CHANGELOG); the fixed-width reader moves to byte offsets before GA (A10).

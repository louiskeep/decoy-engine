# Rust engine program

Status: plan (draft for Cam's review; Codex plan-gate pending)

Date: 2026-09-30. Input: `docs/records/2026-09-30-rust-coverage-evidence-audit.md` (the evidence record; every "today" statement below cites it). Roadmap: decoy-platform `docs/ROADMAP.md`, TOP PRIORITY and Order of work step 2.

## Goal and end state

A fast mask and generate engine, with the Rust companion doing the per-value work on every route and at every size up to 100M+ rows. Pandas remains the fallback for CLI users without the companion and the byte-parity oracle for tests; it is not a production route for jobs the companion can run.

End state, per route:

| Route | Data layer | Math today | Math at end state |
|---|---|---|---|
| Unified slice (full-frame, single table) | in memory | Rust for hash, categorical, bucket_perturb, group_key, date_shift; Arrow/Py for redact, truncate, passthrough | same, plus Faker, FPE, text strategies |
| Chunked (engine auto-chunk, platform Phase 1, CLI `--chunked`) | chunk-bounded (platform); resident (engine auto-chunk) | pandas (deterministic Faker selection compiled) | Rust dispatcher, streaming on every entry point |
| Out-of-core FK | DuckDB scan, join, reorder, spill | Python kernels | Rust kernels, DuckDB unchanged |
| Sequential FK | table by table | pandas | Rust per table |
| Generation | full-frame | Python/NumPy/Faker | Rust selection for pooled and deterministic generators |

## Rules for every slice

- **Byte parity** with the pandas oracle on every admitted case, including the edge values the audit used (nulls, empty strings, leading zeros, Unicode, padded fixed-width).
- **Fail closed per table:** anything the Rust path cannot prove it handles goes to the oracle with a recorded reason, never to a silent partial result.
- **Evidence in the result:** per-node backend (`compiled_kernel_executed` or `pool_select_executed`), the route and its reason, and real per-column timings (so the platform stops reporting 0 ms).
- **Faster than the oracle** on the slice's benchmark, and **absolute peak memory** within the 32 GB box at the slice's largest tested size. The D9 ratio bar is dropped.
- **Gates:** Opus plan, Codex plan-gate, Sonnet build, dennis, Codex final. A Rust slice merges under Cam's standing rule (dennis GO, Codex final GO, CI green, byte-identical, faster, peak within 32 GB); anything short of that stops for Cam. Platform slices merge only with Cam's go.
- One writer per worktree. One heavy test process at a time on the devbox.

## Phase A: prerequisites (correctness and security)

These fix real breakage and do not wait for the Rust work. Sizes are the record's (Codex estimates where larger).

| Slice | Content | Size | Notes |
|---|---|---|---|
| A1 | **Owner scope on upload bindings** (record P2): `resolve_binding` takes the authenticated user; a local upload must belong to that user or the user must be an admin, matching `api/files/router.py:781`; 404 otherwise. Connections stay org-wide (by design, `CloudAccount` is org-level); a test pins that. | S | HIGH security. Lands first. |
| A2 | **Cloud descriptors** (P1): strip `connection_id`, `connection_name` and GCS `region` from the engine copy before validation; keep them in the snapshot for evidence. Tests: the stored snapshot runs through `run_v2_pipeline_from_config` for S3 and GCS, source and target, against moto / fake-gcs-server. | S | After A1 for tidiness; no security dependency (connections are org-wide by design). |
| A3 | **Platform FK cycle routing** (P3): cycle check in `v2_sequential._should_use_sequential_relationship_path` and admission pricing, so cross-table cycles take the full-frame path that works. | S-M | Regression test from R063. |
| A4 | **Phase 1 fixed-width** (P4): reject fixed-width in Phase 1 eligibility until a streaming reader exists. | S | |
| A5 | **CLI fixed-width** (P5): read fixed-width with the engine reader, not as CSV. | S | decoy-cli repo. |
| A6 | **Rust lane timings** (P6): the unified slice returns real per-column `timings` and `boundary_conversion_ms`; removes the engine #180 strict xfail. | S | Engine. |
| A7 | **Retire the D9 ratio check** (R14) in `scripts/bench-unified-slice/bench_compare.py`; keep an absolute-peak check. | S | Needed before B2 activation. |
| A8 | **Transforms parity** (P7): decide where transforms live (engine `run_pipeline` or explicitly platform-only) and make engine, CLI and platform agree. | M | Needs a Cam decision on ownership. |

## Phase B: the chunked dispatcher in production (record R1)

Goal: large single-table and independent multi-table mask jobs run the compiled dispatcher (`run_native_or_oracle_chunked`) instead of pandas, on every chunked entry point. Measured on the tested fixture: 5.9x to 8.4x faster at 1M rows, byte-identical (R074, R075). Covers tables whose columns are all hash, redact, truncate, passthrough, or deterministic-REUSE Faker; everything else keeps going to the oracle until Phase C.

| Slice | Content | Size |
|---|---|---|
| B1 | **Dispatcher production contract** (engine): accept what production callers pass (`registry`, `adapter`, `vault_writer`, `chunk_result_sink`, `base_row_offset`); reconcile its output schema with the oracle route's in the degenerate cases the parity gate currently allowlists (all-null column, zero-row batch); emit `ExecutionResult` timings and route evidence; map `NativeChunkSchemaDriftError` to a typed engine error; a thread-budget parameter (default derived from the job's reservation, not a fixed 1). Exposed as one public engine entry point. | M |
| B2 | **Engine auto-chunk on the dispatcher**: `run_pipeline`'s chunked route calls B1's entry point, with the per-table oracle fallback. Parity suite across the audit's single-table cells at 150k and 1M. | S-M |
| B3 | **Platform Phase 1 on the dispatcher**: `_run_v2_pipeline_streaming` calls B1's entry point; the platform maps the new error and records per-node backend and timings on the job. | S-M |
| B4 | **CLI `--chunked` and `--native`**: `--chunked` uses B1; `--native` stops rejecting jobs that route to chunking (record R10) and reads per-node evidence instead of the "any node compiled" label. | S |
| B5 | **Admission pricing**: price native chunk cost and the thread budget in the platform's admission. | S-M |
| B6 | **Engine auto-chunk streaming**: auto-chunk currently keeps every masked chunk resident; stream chunks to the sink so the engine route is memory-bounded like platform Phase 1. Also remove the platform's eager full read below 256 MiB (record R7). | M |

Phase B total: M-L (the record's 500 to 1,000 lines for B1 to B5, plus B6).

## Phase C: operator coverage (record R2, R3, R6)

Goal: one column no longer sends a large table to pandas.

| Slice | Content | Size |
|---|---|---|
| C1 to C4 | Lift the chunked-dispatcher vetoes one operator at a time: categorical (deterministic), bucket_perturb, group_key, date_shift. Reuse the unified-slice kernels; each needs its cross-chunk semantics proven (group_key sibling state, date_shift prepass). | M each |
| C5 | Faker on the unified slice and the dispatcher for default (non-deterministic) columns and non-string sources, or a documented reason it stays Python. | M |
| C6 | FPE (FF1) and text_mask / text_redact on the Rust paths. | M each |
| C7 | Widen platform Phase 1's strategy allowlist to everything C1 to C6 admit (record R6). | M |

## Phase D: FK routes (record R4, R5, R9, A3 first)

| Slice | Content | Size |
|---|---|---|
| D1 | Rust kernels for out-of-core FK payload masking; DuckDB keeps scan, join and reorder. Start with hash, categorical, bucket_perturb (already out-of-core compatible). | L, ~1,000 to 2,000 LOC |
| D2 | Rust per-table masking on the sequential FK route (diamonds, self-FKs, out-of-core-incompatible strategies). | XL, ~2,000 to 4,000 LOC |
| D3 | Platform out-of-core with validators or vault keeps outputs bounded (record R9). | M |
| D4 | FK out-of-core memory at 100M+ (the July OOC-B revert: DuckDB global sort >21 GB at 200M). Separate design slice; survey external-sort approaches first. | L |

## Phase E: 100M proof

A GCP run at 1M, 10M and 100M rows through the platform worker (real claim, streaming plan), for single-table mask and an FK tree, asserting per-node Rust evidence, byte parity at 1M, wall time, and absolute peak within 32 GB. Uses the existing 50-run GCP budget; Slack before running. This is the capacity proof the audit could not give.

Then the prodsim rebuild (roadmap Order of work step 4) grades every job's per-node backend and treats any pandas fallback as a defect.

## Phase F: breadth (after the 100M proof, order to be set then)

Rust generation selection for pooled and deterministic generators (R12); fixed-width output (R13); streaming subset materialization (R11, L-XL); enforced post-validation on streaming, sequential and out-of-core routes (R8, L-XL); the adaptive-scheduler entry point exercised on an integration host (scheduler S13 VERIFY).

## Order and first steps

1. A1 with A2 (one platform PR or two back to back, A1 first), A3, A4, A6, A7. Small and independent; they can run in parallel worktrees, one writer each.
2. B1, then B2 to B5 (B3 and B4 can run in parallel after B1), then B6.
3. C1 to C4 in parallel after B2; C5 to C7 after.
4. E after B and C; D in parallel with C where people and memory allow.

Rough scale: Phase A about a week of slices; Phase B one to two weeks; Phase C two to three weeks; Phase D several weeks, D2 the largest. These are estimates from the record's sizes, not commitments.

## Decisions for Cam

1. Approve this order, or reorder (for example D before C if FK jobs matter more to early customers).
2. A8: where transforms should live.
3. Confirm the standing Rust-slice merge rule covers Phase B to D slices, and whether Phase A platform fixes may merge under the same rule once CI is back, or each needs your go.
4. Phase E spend: the 100M GCP runs under the existing budget.
5. B1 adds a public engine entry point; fine pre-GA, but it becomes part of the compatibility contract at GA.

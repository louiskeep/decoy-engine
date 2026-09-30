# Rust coverage evidence audit: record

Status: record

Date: 2026-09-30. Plan: `docs/plans/2026-09-30-rust-coverage-evidence-audit.md` (revision 3, Codex plan-gated). Evidence: `docs/records/audit-2026-09-30/`: environment and preflight, the branch-witness ledger (every row has a run id or a "code-inferred" reason), `runs.jsonl` (R001 to R084), stage summaries 2a to 2d, `provenance-2b.md`, and the Codex independent code-only audit.

Pinned: decoy-engine `8dc559e5` (main after #180). The runs recorded `7cadcde0`, which is `8dc559e5` plus docs and audit scripts only (no `src/` change; `provenance-2b.md`). decoy-platform `0701a954` (origin/main, detached worktree). decoy-cli `b8274b01`. Native companion built from the pinned engine tree: `present=True ok=True`, ABI `decoy-native-abi-2`, module SHA-256 `1a3b2d18...b061`. Each run is a fresh subprocess; peak memory is the worker's own `VmHWM`. Stage 2b runs R025 to R042 predate per-run commit recording; `provenance-2b.md` documents their environment.

## Headline

1. **Large masking jobs do not run on the Rust companion today, with one exception.** The unified slice (single-table, full-frame, under 100k rows by default, every column on an allowlisted operator, no `when` gate) is the only route whose mask operators run compiled. The exception: Faker pool selection calls the compiled `derive_index_batch` kernel on every masking route when the companion is present, including chunked and sequential (R069 to R071, R073; `generation/pool/_sampler.py:48-63,129-152`). Generation does not (R072).
2. **A compiled chunked executor exists, is correct at 1M rows, and has no production caller.** `execution.native._dispatch.run_native_or_oracle_chunked` ran a 1M-row hash + redact + truncate + passthrough job in 1.92s at 1 thread and 1.27s at 4 threads, against 11.23s / 10.69s for the pandas oracle, byte-identical (R074, R075). In it, hash and REUSE-mode Faker selection run compiled; redact, truncate and passthrough run Arrow/Python kernels. Categorical, bucket_perturb, group_key and date_shift are vetoed there, and one such column sends the whole table to the oracle (R076, R077; `native/_requirements.py:150-152`, `_dispatch.py:222-230`). No engine, platform or CLI production code calls it (grep of all three trees).
3. **Putting it into production is real work, not a wire-up** (see gap R1). Its production callers pass parameters it does not accept, and its output schema needs caller reconciliation in edge cases.
4. **Platform-created cloud jobs fail.** S3/GCS connection descriptors built by `binding_resolve` are rejected by `run_v2_pipeline_from_config` with `extra_forbidden` before any cloud client exists (R025, R083). With the platform-only keys stripped they validate (R026), and S3 (moto) and GCS (fake-gcs-server) both succeed end to end (R027, R028).
5. **The runs and the Codex code-only audit agree on the facts they both checked.** They differed on the approach for the largest gap (Codex: build a compiled executor behind `run_mask_pipeline_chunked`; this audit: reuse the existing dispatcher). The dispatcher runs above settle it: the compiled executor Codex describes already exists as `run_native_or_oracle_chunked`, so gap R1 is to put it behind the chunked entry points and close its integration gaps, not to write a new one. Codex also missed the Faker compiled selection in headline 1.

## About the historical 100M benchmark

The roadmap's "100M rows in ~430s, ~450 MB" comes from the native-throughput program (engine PR #129, merge `8d833c30`; plan note at `4e00bbac`, lines 1223-1230). Its worker, `scripts/native-baseline/bench_worker_native.py:35,137`, imports and calls `run_native_or_oracle_chunked` directly. The raw log (`native-threadsweep.log`, decoy-platform `docs/product/release-1-validation-runs/2026-09-10-tb6-50m/`) is gitignored, so it is not committed. It shows 8 threads, median 429.85s, 446.9 MB; hash 87.4 to 103.9s, redact 127.8 to 145.4s, truncate 159.3 to 177.7s.
- The "13.5x" is hash against the Phase 0 **native single-thread** baseline (~1,280s, `8d833c30:docs/plans/native-throughput-phase0-baseline.md` lines 28-56), not against pandas. Against the pandas oracle's measured hash throughput (80.6k rows/s/col, extrapolated to 3 columns x 100M) it is roughly 39x.
- A later run of the same dispatcher (`0731e58b`, `scripts/native-baseline/results_native_100m.json`, run at `e7100689`, `native_threads=1`) shows redact and truncate at ~3 to 5s after they were vectorized. Those are Arrow/Python kernels, not Rust.
- The dispatcher has changed since #129 (27 files, ~3.2k lines in `execution/native`, including the chunked vetoes), so the benchmark is evidence for the design, not for today's exact code.

## Map

Backends: **Rust** (companion kernel, `compiled_kernel_executed=True` or `pool_select_executed=True`), **Arrow/Py** (redact, truncate, passthrough), **pandas**, **pandas + Rust Faker selection**, **DuckDB + Python kernels** (out-of-core). "Route-equivalent" means forced with the knob each layer reads, at small data; capacity at that size is unproven.

### Single-table mask, engine direct

| Case | Route | Backend | Runs |
|---|---|---|---|
| hash, categorical (deterministic), bucket_perturb, date_shift, under 100k | unified slice | Rust | R001 R002 R004 R006 R018 |
| group_key (with a passthrough sibling) | unified slice | Rust + Arrow/Py | R005 |
| redact, truncate, passthrough | unified slice | Arrow/Py | R007 R008 R009 |
| native mix (hash, redact, truncate, passthrough) in Parquet, CSV, fixed-width | unified slice | Rust + Arrow/Py, byte-identical to pandas | R013 R015 R016 |
| categorical non-deterministic; FPE; text_mask | declined | pandas | R003 R011 R012 |
| pooled Faker, alone or in the native mix | declined from the unified slice | pandas + Rust Faker selection (1 compiled call) | R069 R070 |
| native mix plus one FPE or text_mask column | whole table declined | pandas | R033 |
| native mix with a `when` gate | whole table declined; `when` is not expressible through `PipelineConfig` | pandas | R017 |
| hash, 150k and 1M, default routing | auto-chunk | pandas | R019 R022 |
| pooled Faker, 150k, default routing | auto-chunk | pandas + Rust Faker selection (1 call per chunk) | R071 |
| hash, 150k and 1M, `auto_chunk=False` | unified slice, full-frame | Rust | R020 R021 |

At 1M hash rows: default chunked pandas 10.79s / 457 MB; forced full-frame Rust 3.35s / 838 MB (R021, R022).

### Compiled chunked dispatcher, called directly (not reachable by any product entry point)

| Case, 1M rows | Result | Wall (dispatcher / oracle) | Peak | Runs |
|---|---|---|---|---|
| hash, redact, truncate, passthrough; 1 thread | admitted; hash compiled, others Arrow/Py; byte-identical | 1.92s / 11.23s | ~585 MB | R074 |
| same; 4 threads | same | 1.27s / 10.69s | ~580 MB | R075 |
| plus one categorical column; 1 and 4 threads | vetoed (`categorical_not_native_chunked_route`), whole table to oracle | | | R076 R077 |
| plus a deterministic pooled Faker column | admitted; `pool_select_executed=True`, 3 calls | | | R084 |

### Single-table generate and mixed

| Case | Route | Backend | Runs |
|---|---|---|---|
| generate only (Faker) | full-frame generation | Python/Faker (0 compiled calls) | R023 R072 |
| mask + generate | full-frame, declined from the unified slice | pandas | R024 |

### Platform, CLI and subset entry points

| Entry point | Result | Runs |
|---|---|---|
| Platform full-frame wrapper | Rust-admitted job ran Rust (`native_keyed_hash`, `compiled_kernel_executed=True`); the platform records `execute_ms = 0.0` (it sums the lane's empty `timings`, `v2_full_frame.py:83-91`); pandas control 49.7 ms | R029 R030 R082 |
| Real Postgres claim then worker | claim stamped `phase1_streaming_tables`, worker exported `execution_mode=chunked_stream`; the function per chunk is `run_mask_pipeline_chunked` on pandas | R068, R034 (function trace), `api/jobs/v2_runner.py:373-419` |
| Phase 1 with a fixed-width source | admitted (no format gate), then the stream reader raises `NotImplementedError` | R035 |
| CLI default, `--native`, `--no-native`, `--chunked` | route as the engine; engine-result node evidence confirms compiled hash for default and `--native`; `--chunked` runs pandas | R036 to R039, R080 R081 |
| Subset-only; subset then mask | Polars subset preprocessing, then the engine route | R040 to R042 |
| Adaptive-scheduler worker (`DispatchPlan`) | **not run**: no cgroup supervisor or delegation on this host (preflight). Open item, needs an integration host. Codex notes its adaptive full-frame route is the only platform path that runs a cross-table cycle, so it would change the cycle row. | preflight |

### Cloud and output

| Check | Result | Runs |
|---|---|---|
| S3 / GCS descriptors from `binding_resolve`, full `run_v2_pipeline_from_config` path | `PipelineConfigError` (`extra_forbidden`: S3 `connection_id`, `connection_name`; GCS also `region`), no cloud client constructed | R025 R083 |
| Same with platform-only keys stripped | they validate; S3 (moto) and GCS (fake-gcs-server) succeed end to end, Rust masking | R026 R027 R028 |
| CSV and Parquet output, Rust route vs pandas route | byte-identical | R031 R032 |
| Fixed-width output | rejected by the target schema (`FileTarget.format`) | R032 |

### FK (relationship) jobs

| Shape | Engine direct, default | Engine forced routes | Platform worker | Runs |
|---|---|---|---|---|
| FK tree | full-frame (byte estimate fits) | sequential works; out-of-core works for OOC-compatible payloads, rejects `group_key` | sequential; out-of-core when both platform thresholds are lowered (admission and runtime agree) | R043 to R046, R057 R058, R060, R066 R067 |
| FK diamond | full-frame | sequential works; out-of-core rejects (`multi_parent_child`) | sequential | R047 to R049, R061 |
| Self-referential FK | sequential (not a cycle) | out-of-core rejects (`self_referential_fk`) | sequential | R050 R051 R062 |
| Cross-table cycle | full-frame (`cross_table_cycle`), succeeds | sequential and out-of-core reject | **fails**: platform picks sequential with no cycle check, engine raises `relationship_cycle`, job lands failed | R052 to R054, R063 |
| FK + validators | full-frame (validators exclude sequential and OOC) | row-count reject at 7.5M, route-equivalent | sequential (platform does not exclude validators) | R055 R059 R064 |
| FK + generate parent | full-frame | n/a | full-frame | R056 R065 |

Backends on the FK routes, instrumented: sequential masks through `PandasExecutionAdapter._dispatch_mask_node` (10 calls, 0 calls to `.run`) (R078); out-of-core masks through `_runner.mask_batch` with the pure-Python `hash_array` kernel (`kernel/_scalar.py`) (R079). A Faker payload column on the sequential route still uses Rust Faker selection (R073). Out-of-core `batch_join` vs `reorder` was not forced (parent-key counts stayed under the 2M default); code-inferred in the ledger.

## Gaps

Two lists: prerequisites (correctness and security, fix regardless of Rust work), then Rust coverage. Size: **S** (small fix), **M** (hundreds of lines), **M-L** (500 to 1,000 lines), **L**.

### Prerequisites

| # | Gap | Evidence | Unlocks | Size |
|---|---|---|---|---|
| P1 | Strip platform-only keys (`connection_id`, `connection_name`, GCS `region`) before engine validation; keep them for evidence | R025 R083; stripped R026 R027 R028 | every platform-created S3/GCS job, source and target | S |
| P2 | Owner check in `resolve_binding` for local uploads and connections (`binding_resolve.py:93-94`, caller `_service.py:462-467`) | code (stage 2b) | closes a HIGH security gap | S |
| P3 | Platform FK cycle routing: add the cycle check to `v2_sequential._should_use_sequential_relationship_path` and admission pricing | R063 vs R052 | cross-table-cycle jobs on the platform | S |
| P4 | Phase 1 fixed-width: reject until a streaming reader exists | R035 | stops a crash on fixed-width large jobs | S |
| P5 | CLI reads fixed-width files as CSV (`decoy src/decoy/cli/run.py:1191-1224`, Codex gap 7) | code | correct fixed-width in the CLI | S |
| P6 | Rust per-column timings: the unified lane returns `timings=()`, platform shows 0 ms | R029 R030 R082; strict xfail in engine #180 | speed reporting for Rust jobs | S |
| P7 | Transforms apply only on the platform, not engine or CLI (Codex gap 7) | code | consistent results across entry points | M |

### Rust coverage

| # | Gap | Evidence | Unlocks | Existing code | Size |
|---|---|---|---|---|---|
| R1 | Put the compiled chunked dispatcher behind the chunked entry points (engine auto-chunk, platform Phase 1, CLI `--chunked`). Needs: the parameters production callers pass (`registry`, `adapter`, `vault_writer`, `chunk_result_sink`, `base_row_offset`); output-schema reconciliation with the oracle route; `ExecutionResult` timings; a thread-budget policy tied to the job's reservation (`native_threads` defaults to 1); mapping `NativeChunkSchemaDriftError`; a public engine entry point for the platform; the CLI call site and its `--native` evidence gate; admission pricing for native chunk costs. Auto-chunk currently lists every chunk resident (`_pipeline_route_exec.py:570-586`), so this gives speed, not streaming, until that changes. | R019 R022 R068 (today pandas); R074 R075 (dispatcher 5.8x to 8.4x faster at 1M, byte-identical) | Rust hash and Arrow/Py redact/truncate/passthrough for large single-table and independent multi-table jobs, 100k to 100M+ | `run_native_or_oracle_chunked`, its physical driver, its parity and certification tests (42 passing), the #129 benchmark harness | M-L |
| R2 | Lift the chunked-route vetoes: categorical, bucket_perturb, group_key, date_shift on the chunked dispatcher | R076 R077 | large jobs using those operators stay on Rust | the unified-slice kernels for the same operators | M per operator |
| R3 | Widen unified-slice and chunked operator coverage so one column does not send the table to pandas: pooled Faker on the unified slice (selection already compiled), non-deterministic categorical, FPE, text_mask | R003 R010 R011 R012 R033 R069 R070 | mixed-strategy jobs | Faker `pool_select` path, the FF1 implementation | M per operator |
| R4 | Rust kernels for out-of-core FK payloads (keep DuckDB for scan, join, reorder) | R079, R046 R067 | large FK trees | #145 OOC physical dispatch (shadow), the companion operators | M-L |
| R5 | Rust per-table masking on the sequential FK route (diamonds, self-FKs, OOC-incompatible strategies) | R078, R044 R048 R050 R060 to R062 | FK shapes out-of-core refuses | the unified-slice operators | M-L |
| R6 | Widen Phase 1 streaming's strategy allowlist (hash, redact, truncate, passthrough today) once R1 lands | Codex `_phase1_eligibility.py:67-74` | platform large-table streaming for more strategies | | S after R1 |
| R7 | Remove eager reads before engine chunking: below 256 MiB the platform loads a large table fully, then the engine chunks it (Codex Q3) | code | lower peak memory for mid-size jobs | | M |
| R8 | Enforced post-validation rejects streaming, sequential and out-of-core routes (Codex gap 9) | code | post-validation on large jobs | post-validation scanners | M-L |
| R9 | On the platform out-of-core route, validators and the vault make outputs resident (Codex shape 9) | code | memory-bounded large FK jobs with validators | | M |
| R10 | CLI `--native` rejects jobs that route to auto-chunk (Codex shape 1) | code, R036 to R039 | native CLI runs on large tables (after R1) | | S after R1 |
| R11 | Streaming subset materialization (Codex gap 10) | code, R040 to R042 | subset at 100M+ | Polars closure code | M-L |
| R12 | Rust generation selection for pooled and deterministic generators; arbitrary Faker providers stay Python | R072 | faster generation | pool sampler | M |
| R13 | Fixed-width output (schema change plus writer) | R032 | fixed-width round trips | | M |
| R14 | Retire or relax the D9 ratio check in `scripts/bench-unified-slice/bench_compare.py`; keep an absolute-peak check. Cam dropped the ratio bar on 2026-09-30 (decoy-platform `docs/ROADMAP.md`, TOP PRIORITY) | | unblocks activation gates | | S |

## Limits

- Capacity above ~1M rows is not proven here; large-tier cells are route-equivalent. The only 100M evidence is the #129 benchmark, which predates ~3.2k lines of dispatcher changes.
- The adaptive-scheduler entry point is open (needs an integration host).
- Of the ledger's 335 rows, rows without a run id are marked "code-inferred" with their reason; they are not witnessed. The ledger lists its open questions.
- Out-of-core `batch_join` vs `reorder`, the REMAP orphan-policy FK path, and several decline fixtures are code-inferred only.
- Peak memory comes from a 12 GB LXC and is indicative, not a sizing figure.

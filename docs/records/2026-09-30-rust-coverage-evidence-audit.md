# Rust coverage evidence audit: record

Status: record

Date: 2026-09-30. Plan: `docs/plans/2026-09-30-rust-coverage-evidence-audit.md` (revision 3, Codex plan-gated). Evidence: `docs/records/audit-2026-09-30/` (environment, preflight, the 278-entry branch-witness ledger, `runs.jsonl` with runs R001 to R068, the three stage summaries, and the Codex independent code-only audit).

Pinned: decoy-engine `8dc559e5` (main after #180), decoy-platform `0701a954` (origin/main, detached worktree), decoy-cli `b8274b01`. Native companion built from the pinned engine tree, `present=True ok=True`, ABI `decoy-native-abi-2`, module SHA-256 `1a3b2d18...b061`. Every run is a fresh subprocess; peak memory is the worker's own `VmHWM`.

## Headline

1. **No large job runs on the Rust companion today.** Rust is reached only by the unified slice: single-table, full-frame (under 100k rows by default), pure-mask, every column on an allowlisted operator, no `when` gate. Everything larger or wider runs on pandas, or on DuckDB plus Python/Arrow.
2. **The compiled chunked Rust executor already exists and is proven, but nothing in production calls it.** `execution.native._dispatch.run_native_or_oracle_chunked` (and its physical driver `NativeOrOracleChunkedAdapter`) has parity, certification and Faker-pool tests, and it is the exact code the native-throughput benchmark ran: 100M rows in ~430s on GCP, with hash at 94.8s (13.5x its pandas time). The remaining time was redact and truncate, since vectorized (a later rerun of the same code shows them at ~3 to 5s). Engine auto-chunk, platform Phase 1 streaming and CLI `--chunked` all call the pandas-only `_chunked.run_mask_pipeline_chunked` instead.
3. **Platform-created cloud jobs are broken.** Every S3/GCS connection binding is rejected by engine validation at worker time. Stripping the platform-only keys makes both work end to end.
4. The code-only Codex audit and the runs agree on every checked point.

## Map

Backends: **Rust** (companion kernel with `compiled_kernel_executed=True`), **Arrow/Py** (redact, truncate, passthrough on the unified lane), **pandas**, **DuckDB+Py** (out-of-core relational work with Python/Arrow mask kernels). "Route-equivalent" = forced by the knob each layer reads at small data; capacity at that size is unproven.

### Single-table mask, engine direct

| Case | Route | Backend | Runs |
|---|---|---|---|
| hash, categorical (deterministic), bucket_perturb, group_key, date_shift, under 100k | full-frame unified slice | Rust | R001 R002 R004 R005 R006 R018 |
| redact, truncate, passthrough, under 100k | unified slice | Arrow/Py | R007 R008 R009 |
| native mix (hash + redact + truncate + passthrough) in Parquet, CSV, fixed-width | unified slice | Rust + Arrow/Py, byte-identical to pandas | R013 R015 R016 |
| categorical non-deterministic; Faker (pooled); FPE; text_mask | declined to pandas | pandas | R003 R010 R011 R012 |
| native mix plus one Faker, FPE or text_mask column | whole table declined | pandas | R014, stage 2b |
| native mix with a `when` gate | whole table declined (and `when` is not expressible through `PipelineConfig`) | pandas | R017 |
| 150k and 1M, default routing | auto-chunk | pandas | R019 R022 |
| 150k and 1M, `auto_chunk=False` | unified slice, full-frame | Rust | R020 R021 |

At 1M hash rows: default chunked pandas 10.79s / 457 MB; forced full-frame Rust 3.35s / 838 MB (R021, R022).

### Single-table generate and mixed

| Case | Route | Backend | Runs |
|---|---|---|---|
| generate only (Faker) | full-frame generation | Python/Faker | R023 |
| mask + generate | full-frame, declined from the unified lane | pandas | R024 |

### Platform and CLI entry points

| Entry point | Result | Runs |
|---|---|---|
| Platform full-frame wrapper | Rust-admitted jobs run Rust; platform records `execute_ms = 0.0` for them (sums the lane's empty `timings`); pandas control 49.7 ms | stage 2b |
| Real Postgres claim then worker (Phase 1 streaming) | claim stamps `phase1_streaming_tables`, worker runs `run_mask_pipeline_chunked`, `execution_mode=chunked_stream`, pandas | R068 |
| Phase 1 with a fixed-width source | admitted by the classifier (no format gate), then the stream reader raises `NotImplementedError` | stage 2b |
| CLI default, `--native`, `--no-native`, `--chunked` | routes as the engine; `--chunked` runs pandas; the CLI's `native` label is set if any one node ran compiled | stage 2b, ledger B200 |
| Subset-only and subset then mask | Polars subset preprocessing, then the engine route for the mask | stage 2b |
| Adaptive-scheduler worker (`DispatchPlan`) | not run: no cgroup supervisor or delegation on this host; deferred to scheduler S13 VERIFY on an integration host. It selects among the same engine functions, so it does not change the backend map | preflight |

### Cloud and output

| Check | Result | Runs |
|---|---|---|
| S3 / GCS descriptors from `binding_resolve` | rejected by `validate_v2_config` before any cloud client is built (S3: 2 extra keys; GCS: 3 incl. `region`) | stage 2b |
| Same descriptors with platform-only keys stripped | succeed end to end: moto S3 and fake-gcs-server, Rust masking | stage 2b |
| CSV and Parquet output, Rust vs pandas route | byte-identical | stage 2b |
| Fixed-width output | rejected by the target schema (`FileTarget.format`) | stage 2b |

### FK (relationship) jobs

| Shape | Engine direct, default | Engine forced routes | Platform worker | Runs |
|---|---|---|---|---|
| FK tree | full-frame (byte estimate fits), pandas | sequential works; out-of-core works for OOC-compatible payloads, rejects `group_key` | sequential; out-of-core when both platform thresholds are lowered (admission and runtime agree) | R043-R046 R057 R058 R060 R066 R067 |
| FK diamond | full-frame | sequential works; out-of-core rejects (`multi_parent_child`) | sequential | R047-R049 R061 |
| Self-referential FK | sequential (not a cycle) | out-of-core rejects (`self_referential_fk`) | sequential | R050 R051 R062 |
| Cross-table cycle | full-frame (`cross_table_cycle`), succeeds | sequential and out-of-core both reject | **fails**: platform picks sequential with no cycle check, engine raises `relationship_cycle`, job lands failed | R052-R054 R063 |
| FK + validators | full-frame (validators exclude sequential/OOC) | row-count reject at 7.5M route-equivalent | sequential (platform does not exclude validators; ran fine) | R055 R059 R064 |
| FK + generate parent | full-frame, pandas | n/a | full-frame | R056 R065 |

All FK routes use pandas (full-frame, sequential) or DuckDB+Py (out-of-core). None reaches the Rust companion. Out-of-core `batch_join` vs `reorder` was not forced (all parent-key counts stayed under the 2M default): code-inferred only (ledger).

## Ranked gaps

Ranked by how much real work each moves onto Rust or unblocks. Size: **wire** (connect existing code), **port**, **new**.

1. **Wire the compiled chunked executor into production chunking.** Point `run_pipeline`'s auto-chunk, platform Phase 1 streaming and CLI `--chunked` at `run_native_or_oracle_chunked` (per-table fallback to the pandas oracle where an operator is not native, as the dispatcher already does). Unlocks Rust for every large single-table and independent multi-table mask job, 100k to 100M+. Evidence: R019/R022, R068, the #129 benchmark lineage. Existing code: the dispatcher, its driver, its parity/certification tests. Size: wire, plus admission and CI gates.
2. **Fix platform cloud descriptors.** Strip `connection_id` / `connection_name` (and GCS `region`) before engine validation, keeping them for evidence. Unlocks every platform-created S3/GCS job. Size: small platform fix.
3. **Fix cross-owner binding.** `resolve_binding` checks no owner for local uploads or connections (`binding_resolve.py:93-94`, caller `_service.py:462-467`). Size: small platform fix. HIGH security.
4. **Rust per-column timings.** The unified lane returns `timings=()`, so the platform shows 0 ms for Rust jobs (strict xfail in engine #180). Needed for job-detail speed reporting. Size: small engine change.
5. **Platform FK cycle routing.** Add the cycle check to `v2_sequential._should_use_sequential_relationship_path` (and admission pricing) so cross-table cycles take the path that works. Size: small platform fix.
6. **Widen Rust operator coverage** so one column does not push a whole table to pandas: pooled Faker selection on the unified lane, deterministic handling for categorical, then FPE and text strategies. Existing code: the chunked Faker `pool_select` path. Size: port per operator.
7. **Rust kernels for out-of-core FK payloads** (keep DuckDB for scan, join, reorder). Unlocks Rust for large FK trees. Existing code: #145 OOC physical dispatch (shadow), the same companion operators. Size: port.
8. **Rust per-table masking on the sequential FK route** (diamonds, self-FKs, OOC-incompatible strategies, CSV/fixed-width FK). Size: port.
9. **Phase 1 streaming correctness**: reject fixed-width until a streaming reader exists; widen the strategy allowlist once gap 1 lands.
10. **Rust generation selection** (pooled/deterministic generators). Arbitrary Faker providers stay Python. Size: port/new.
11. **Output**: streaming output for the chunked/streaming routes is exercised by gap 1; fixed-width output is a schema change plus a writer. Size: new.
12. **Retire or relax the D9 ratio check** in `scripts/bench-unified-slice/bench_compare.py` (Cam dropped the ratio bar); keep an absolute-peak check.

## Limits

- Capacity above ~1M rows is not proven here; large-tier cells are route-equivalent only. The #129 benchmark is the only 100M evidence, and it ran the dispatcher directly on GCP.
- The adaptive-scheduler entry point was not run.
- Out-of-core `batch_join` vs `reorder` and several decline fixtures (group_key order dependence, null-bearing int hash) are code-inferred in the ledger, not witnessed.
- Peak memory comes from a 12 GB LXC and is indicative, not a sizing figure.

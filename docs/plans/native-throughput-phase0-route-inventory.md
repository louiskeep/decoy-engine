Status: record

# Phase 0 / Task 0.3 - Route and Contract Ownership Inventory

Plan: `docs/plans/2026-09-09-execution-consolidation-and-native-throughput.md` (Task 0.3, §3.5, §7).
Scope: READ-ONLY archaeology. No code modified.

Repos / branches:
- Engine: `/home/cam/vscode/decoy-engine` @ `feat/native-throughput-consolidation`
- Platform: `/home/cam/vscode/decoy-platform` @ `test/test7-connector-io`
- CLI: `/home/cam/vscode/decoy` @ `fix/ooc-preflight-advisory-rendering`

All file:line citations are exact unless labelled INFERRED.

---

## 0. The six execution routes (§3.5 enumeration mapped to code)

| # | Route (plan name) | Engine entrypoint | Wired into `run_pipeline`? |
|---|---|---|---|
| 1 | pandas full-frame | `adapter.run(...)` where adapter = `PandasExecutionAdapter` | Yes - default terminal branch (`_pipeline.py:527`) |
| 2 | pandas chunked | `run_mask_pipeline_chunked` via `run_mask_chunked` | Yes - `auto_chunk` (default True) (`_pipeline.py:508-525`) |
| 3 | DuckDB out-of-core | `run_fk_out_of_core` via `run_out_of_core_route` | Yes - FK auto-route (`_pipeline.py:433-450`) |
| 4a | native lane (production seam) | `try_native_route` / `_run_native_streaming` (passthrough/redact/truncate) | Yes but OFF by default (`native_route_enabled=False`, `_pipeline.py:453`) |
| 4b | native columnar (Faker/pool) | `run_native_or_oracle_chunked` / `plan_native_route` | **NO production caller** - benchmark scripts + tests only |
| 5a | pandas execution adapter | `PandasExecutionAdapter` (default full-frame + sequential + chunked substrate) | Yes |
| 5b | Polars execution adapter | `PolarsExecutionAdapter` via `select_execution_adapter` | Yes - `substrate="polars"` is the S13 default of `resolve_substrate` |
| - | sequential (bounded FK) | `run_sequential` via `run_sequential_route` | Yes - FK auto-route (`_pipeline.py:410-429`) |

Note: the plan's "native columnar" and "pandas/Polars execution adapters" enumerate to the modules above. Sequential is a distinct fourth bounded route not separately named in §3.5 but central to FK routing; it is included throughout.

---

## 1. Engine responsibility map, per route (file:line)

### 1.1 Planning / route-eligibility decision

- **Single live router (all bounded/full-frame FK + non-FK decisions):**
  `decide_execution_route` in `src/decoy_engine/execution/_pipeline_routing.py:221-555`.
  Returns `(route, route_reason)` where route in `{out_of_core, sequential, full_frame}`, or raises fail-closed `ExecutionError` (`fk_full_frame_oom_risk_rejected` / `_estimated`).
  - Sequential eligibility predicate: `_sequential_eligible` `_pipeline_routing.py:172-218` (pure-mask FK; disqualified by generate table, validators, fidelity_report, vault_writer, non-pandas substrate).
  - Cross-table FK cycle check: `_has_cross_table_fk_cycle` `_pipeline_routing.py:143-169`.
  - Out-of-core admission (strict subset of sequential): `out_of_core_ready` computed `_pipeline_routing.py:363-370`, needs `check_out_of_core_compatibility` admit + largest mask table >= `out_of_core_threshold_rows`.
  - Called from `run_pipeline` via wrapper `resolve_execution_route` (`_pipeline_routing_signals`) at `_pipeline.py:364-385`.
- **Chunked (route 2) eligibility:** `classify_job` -> `_chunked_rejection` in `src/decoy_engine/execution/_planner.py:177-278` and `:325-415`; routed only when `run_pipeline(auto_chunk=True)` via `decide_chunk_route` (`_pipeline_chunk_route.py`), call site `_pipeline.py:397-408`. Threshold `AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT = 100_000` (`_planner.py:103`).
- **Polars-native (route 5b) classification (EXPLAIN only, does not route):** `_polars_native_rejection` `_planner.py:281-322`. Actual polars-vs-pandas selection is `resolve_substrate` default + `select_execution_adapter`, NOT the planner. `PLANNER_ROUTING_ENABLED = False` (`_planner.py:96`) - full planner routing is dormant.
- **Native production lane (route 4a) candidacy:** `static_candidacy` `src/decoy_engine/execution/_native_route.py:106-207` (no-I/O gate) then `peek_and_admit` `_native_route.py:210-255` (first-batch schema/type gate). Allowlist `ALLOWED_STRATEGIES = {passthrough, redact, truncate}` (`_native_route.py:57`).
- **Native columnar (route 4b) route decision:** `_static_route_decision` / `plan_native_route` `src/decoy_engine/execution/native/_dispatch.py:231-326`. NOT reached from `run_pipeline`.
- **Out-of-core sub-route (reorder vs `_batch_join`):** `decide_route` `src/decoy_engine/execution/out_of_core/_route_policy.py:94-158`. Threshold `REORDER_PARENT_KEY_THRESHOLD = 2_000_000` (`_route_policy.py:49`).
- **Substrate resolution:** `resolve_substrate` `src/decoy_engine/execution/_substrate.py:28-48`; default `_DEFAULT_SUBSTRATE = "polars"` (`_substrate.py:25`).

### 1.2 Admission / preflight (memory, capacity, fan-in)

- **Full-frame byte-estimate admission + OOM reject:** inside `decide_execution_route` (`_pipeline_routing.py:428-555`), driven by `byte_estimate_full_frame_fits` / `resolve_probe_recovery` (`_pipeline_routing_signals.py`, re-exported `_pipeline_routing.py:110-119`). Uses `_mem_estimate.fits` (INFERRED from docstring `_pipeline_routing.py:89-95`). `use_byte_estimate_routing`/`use_probe_routing` default True (`_pipeline.py:160-161`).
- **Out-of-core memory limit + fan-in:** `resolve_ooc_memory_limit` + `enforce_ooc_memory_preflight` called in `run_out_of_core_route` `_pipeline_route_exec.py:326-395`. Fan-in refusal code `out_of_core_fanin_exceeds_budget`; concurrency model `_max_concurrent_ooc_instances` `_pipeline_route_exec.py:465-492`; per-table build floor `_parent_table_row_counts` `_pipeline_route_exec.py:495-528`.
- **Out-of-core disk preflight (advisory):** `enforce_ooc_disk_preflight` wired via `resolve_execution_route` (`_pipeline_routing_signals`), runtime enforcer `check_temp_disk_budget` inside runner; temp-disk budget sized at `_pipeline_route_exec.py:349-362` (`_TEMP_DISK_SAFETY_FRACTION = 0.9`, `:60`).
- **Out-of-core compatibility gate:** `check_out_of_core_compatibility` `out_of_core/_compat.py` (exported `out_of_core/__init__.py:15`).
- **Knob validation (fail-closed at submit):** `require_bool`/`require_positive_int` `_substrate.py:51-73`; called in `run_pipeline` at `_pipeline.py:269-288` for substrate, auto_chunk, thresholds, `native_route_enabled`.
- **Native lane admission:** static (`static_candidacy`) + dynamic (`peek_and_admit`) as above; ledger invariants enforced pre-commit by `_validate_ledger` `_native_route_exec.py:186-266`.

### 1.3 Batch execution entrypoint (the runner that masks)

- **Full-frame:** `adapter.run(plan, merged_sources, ...)` `_pipeline.py:527-536`. Adapter = `PandasExecutionAdapter` (`_pandas_adapter.py`) or `PolarsExecutionAdapter` (`polars/_polars_adapter.py`), constructed by `select_execution_adapter` `_substrate.py:76-110`, call `_pipeline.py:270-275`.
- **Chunked:** `run_mask_chunked` `_pipeline_route_exec.py:531-598` -> `_chunked.run_mask_pipeline_chunked` (`_chunked.py`). Call site `_pipeline.py:513-525`.
- **Sequential:** `run_sequential_route` `_pipeline_route_exec.py:100-177` -> `run_sequential` (`_sequential.py`). Call site `_pipeline.py:412-429`.
- **Out-of-core:** `run_out_of_core_route` `_pipeline_route_exec.py:180-446` -> `run_fk_out_of_core` (`out_of_core/_runner.py`, exported `out_of_core/__init__.py:24`). Sub-route driver `_stream_driver.stream_table` vs `_runner._stream_table` selected by `decide_route`.
- **Native lane (4a):** `_run_native_streaming` `_native_route_exec.py:367-460`, kernels `native/_kernels_scalar.py`.
- **Native columnar (4b):** `run_native_or_oracle_chunked` `native/_dispatch.py:577+`; Faker via `_sample_faker_chunk` `_dispatch.py:344`, `PoolSampler` (`generation/pool/_sampler.py`). Not called in production.

### 1.4 Diagnostics / route-evidence emission

- **Shared execution telemetry (all routes):** `execution_telemetry` `_pipeline_route_exec.py:63-97`; stamped into `quality_metrics["execution"]` per route (sequential `:156`, ooc `:412`, full-frame `_pipeline.py:622-628`, native `_native_route_exec.py:_execution_envelope:328-342`).
- **Reproducibility / adapter identity stamp:** `_pipeline_finalize.stamp_execution_metrics` call `_pipeline.py:559-574`; native equivalent `_execution_adapter_stamp` `_native_route_exec.py:344-366` (`adapter_name="native"`).
- **EXPLAIN block (opt-in `explain_plan`):** `quality_metrics["execution_plan"]` stamped in every route (`_pipeline.py:601-606`, seq `_pipeline_route_exec.py:163-168`, ooc `:432-437`, native `_native_route_exec.py`).
- **Native lane evidence object:** `NativeRouteReport` / `NativeRouteLedger` `_native_route.py:258-308`; returned on `ExecutionResult.native_route` (`_pipeline.py:638`). Ledger counts native calls at actual boundaries (§6.6 observable-routing pattern).
- **Native columnar evidence (unwired):** `NativeRouteEvidence` / `NodeRouteRecord` `native/_dispatch.py:133-172`; per-invocation pool-warning isolation `RouteDiagnostics` `native/_route_diagnostics.py` (standalone, NOT wired - `_route_diagnostics.py:24-30`).

### 1.5 Staging + publication path (atomicity)

- **Sink protocol:** `TransactionalSink` `src/decoy_engine/execution/_transactional_sink.py:51-90` (write/write_batches/commit/abort). Reference impl `ParquetTransactionalSink` `_transactional_sink.py:124-298`: atomic publish via single `os.replace` directory rename (`commit()` `:258-286`), fail-closed on non-empty target, best-effort `abort()`.
- **Sink-based publication is used ONLY on sequential / out-of-core / native-lane routes** (sink passed at `_pipeline.py:418, 439, 461`). commit/abort for native at `_native_route_exec.py:428-441`; for sequential/ooc inside their runners.
- **Full-frame and chunked routes do NOT use the sink**: `run_pipeline` returns `ExecutionResult.outputs` as an in-memory `dict[str, pa.Table]` (`_pipeline.py:586-588, 630-637`). Publication of those outputs is the CALLER's responsibility (platform/CLI). This is an ownership split - see §3 duplications.

### 1.6 Public API surface each route depends on

- `run_pipeline` (`decoy_engine.execution._pipeline`, re-exported `decoy_engine/__init__.py:103`, `execution/__init__.py:176`).
- `select_execution_adapter`, `resolve_substrate`, `VALID_SUBSTRATES` (`_substrate.py`, exported `__init__.py:104`, `execution/__init__.py:173,178`).
- `ExecutionResult` (`_adapter.py`, exported `__init__.py:97`).
- `TransactionalSink`, `ParquetTransactionalSink` (`execution/__init__.py:152,161`).
- `run_fk_out_of_core`, `run_sequential`, `run_mask_pipeline_chunked`, `classify_job`, `run_pipeline_isolated` (`execution/__init__.py:166,175,177`).
- `generate_tables` (`generation.synthesize`, exported `__init__.py:135,396`).
- `compile_plan`, `PipelineConfig` (`__init__.py:381,340`).
- Engine `__version__ = "0.5.0"` (`__init__.py:274`); `SEED_PROTOCOL_VERSION = 6` (`determinism/_derive.py:118`, exported `__init__.py:72,285`).

---

## 2. Callers in decoy-platform and the CLI

### 2.1 Platform (`/home/cam/vscode/decoy-platform`, branch test/test7-connector-io)

Central fork: `run_v2_pipeline_job` in `api/jobs/v2_orchestrator.py`. Streaming classification at `v2_orchestrator.py:178`; if/elif/else route fork:
- streaming route -> `v2_orchestrator.py:192`
- sequential-relationship (contains out-of-core sub-choice) -> `v2_orchestrator.py:270`; out-of-core sub-branch `:303`, sequential else `:341`; OOC-decline falls back to sequential `:315-340`
- full-frame else -> `v2_orchestrator.py:375`

Critical fact: the platform makes its OWN route decision and BYPASSES the engine's `run_pipeline` for the FK routes, calling `run_sequential` / `run_fk_out_of_core` directly. It never passes `execution_mode`, `substrate`, `native_route_enabled`, or `auto_chunk`. Rationale documented at `v2_out_of_core.py:11-18`.

| Route | Platform caller (module:function) | Engine entrypoint + call site | Route condition (platform-owned) | Publication |
|---|---|---|---|---|
| Full-frame | `v2_runner.py:run_v2_pipeline` (called `v2_orchestrator.py:382`) | `run_pipeline` (`v2_runner.py:270` import, `:289-296` call) | else fallthrough (`v2_orchestrator.py:375`); only sets seed | `write_v2_outputs` `v2_orchestrator.py:437` -> `v2_cloud_materialize.py:531`; atomic `os.replace` `_materialize_file_output` `v2_cloud_materialize.py:53` |
| Chunked streaming | `v2_runner.py:_run_v2_pipeline_streaming:299` | `run_mask_pipeline_chunked` (`v2_runner.py:327` import, `:352-358` call) | `classify_streaming_eligibility` `streams.py:454-562`; gated by `settings.streaming_execution_enabled` (default OFF `streams.py:498`) | incremental sink `open_target_writer` `streams.py:420`; `writer.write_batch`/`commit` once `v2_runner.py:360-363`, `abort` on error `:364-366`; atomic tmp+`os.replace` `streams.py:253-257` |
| Sequential FK | `v2_sequential.py:run_sequential_relationship_job:463` -> `_run_v2_pipeline_sequential_to_stage` | `run_sequential` (`v2_sequential.py:156` import, `:191-199` call), constructs its own `PandasExecutionAdapter` | `_should_use_sequential_relationship_path` `v2_sequential.py:109-121` (has relationships AND no generate_columns) | engine writes temp `ParquetTransactionalSink(stage_dir)` `v2_sequential.py:198`; platform publishes per table `_materialize_staged_sequential_outputs` -> `write_v2_outputs` `v2_sequential.py:326`; validators/quarantine before publish `:240-278,411-419` |
| Out-of-core FK | `v2_out_of_core.py:run_out_of_core_relationship_job:363` -> `_run_v2_pipeline_out_of_core_to_stage` | `run_fk_out_of_core` (`v2_out_of_core.py:258-262` import, `:322-330` call) | `_should_use_out_of_core_relationship_path` `v2_out_of_core.py:165-223` (sequential-eligible + no transforms + all local Parquet + largest rows >= `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` `:86,102`); engine authority `check_out_of_core_compatibility` `:295`; runtime dtype decline `:331-352` | same staged sink + publish path as sequential (`v2_sequential.py:345-460`); label `node_execution_mode="out_of_core_fk"` `v2_out_of_core.py:405` |
| Preview (non-job) | `v2_preview.py:75,138` | `run_pipeline` | preview only | n/a |

Platform admission / preflight (owned before any engine call):
- `estimate_admission` `admission.py:265` (fail-open; multipliers CSV 6.0 / Parquet 1.5 `admission.py:76-77`; FK discount `admission.py:307-319`). Run at job creation, not in runner.
- FK-aware multiplier `admission_fk.py` (`fk_aware_multiplier:256`, discount `FK_BOUNDED_ROUTE_DISCOUNT=0.75:120`).
- Gate enforcement `gate_decision` `admission.py:362`, consulted `queue_worker.py:453`.
- Worker budget `worker_budget.py:50-51`, checked `queue_worker.py:471-473` (sequences concurrency, never rejects).
- Memory-ceiling classifier `memory_ceiling.py:classify_inmemory_ceiling:103` (read-only, does not route).
- Target locks `queue_worker.py:474-504`.

Platform native route: `native_route` / `native_route_enabled` token does NOT exist anywhere in the platform repo (rg zero hits). Never enabled.

### 2.2 CLI (`/home/cam/vscode/decoy`, branch fix/ooc-preflight-advisory-rendering)

The CLI hands config to the engine's unified `run_pipeline` (plain) or `run_mask_pipeline_chunked` (chunked), and never selects out-of-core / sequential / native routes itself. Tokens `execution_mode`, `native_route_enabled`, `auto_chunk`, `fallback_to_pandas`, `max_workers`, `fpe_chunk_count` do not appear in `src/`. `run_fk_out_of_core` / `run_sequential` are never referenced in `src/`.

| Route | CLI caller | Engine entrypoint + call site | Condition | Publication |
|---|---|---|---|---|
| Plain full-frame | `cli/run.py:run:141` | `run_pipeline` (`run.py:356-359` import, `:524-531` call) | else of `if chunked` (`run.py:521`); `--substrate` deliberately NOT forwarded, warns `run.py:334-339` | `_write_mask_outputs` `run.py:532` (def `:996`, reads `targets`) |
| Chunked | `cli/run.py:_run_chunked_mask:845` | `run_mask_pipeline_chunked` (`run.py:872-874` import, `:893-900` call); `select_execution_adapter(substrate=...)` `:876` | `--chunked` flag `run.py:205,519`; chunked+generate rejected `run.py:499-504` | per-table `targets[...].path` `run.py:889-891`; `_write_chunked_output` `:901` (def `:926`) |
| Subset | `cli/subset.py` | `plan_subset` `subset.py:230-237` (dry-run) / `run_subset` `subset.py:242-250` | `--dry-run` split `subset.py:229` | `output_dir=out` `subset.py:249` |
| Public API mask | `api.py:mask` | `run_pipeline` (`api.py:368` import, `:431-438` call) | substrate omitted unless caller sets it `api.py:427-429` | `_write_mask_outputs` `api.py:440` |
| Demo | `cli/demo.py` | `select_execution_adapter().run(...)` directly (`demo.py:245-252`); `compile_plan` `:232` | demo walkthrough | `_write_mask_outputs` `demo.py:253` |
| Plan / compile-explain | `cli/plan.py:185`, `cli/compile_explain.py:153` | `compile_plan` (planning only, no execution) | n/a | emits Plan YAML |

CLI preflight (`cli/preflight.py`, separate command, `decoy run` does NOT auto-invoke it):
- `estimate_job_capacity` `preflight.py:449-451` (engine attr, degrades if absent `:403-410`); verdict rendering under `capacity.out_of_core_fk` `:452-535`; capacity codes `out_of_core_insufficient_memory` / `out_of_core_fanin_exceeds_budget` `run.py:41`, EXIT_CAPACITY `run.py:579-583`.

CLI native route: never enabled (`native_route` absent from `src/`).

---

## A. Consolidated ownership table (route -> owners, file:line)

Legend: E=engine, P=platform, C=CLI. "Publication owner" splits: engine-sink routes vs caller-materialized routes.

| Route | Planning owner | Admission/preflight owner | Execution entrypoint | Diagnostics owner | Publication owner | Platform caller | CLI caller |
|---|---|---|---|---|---|---|---|
| pandas full-frame | E `decide_execution_route` `_pipeline_routing.py:221` (full_frame branch `:489-555`); byte-estimate admission `:428-488` | E byte-estimate/probe `_pipeline_routing.py:428-488`; P `estimate_admission` `admission.py:265` | E `adapter.run` `_pipeline.py:527`; adapter `select_execution_adapter` `_substrate.py:76` | E `execution_telemetry` `_pipeline_route_exec.py:63` + `stamp_execution_metrics` `_pipeline.py:559` | Caller: engine returns `outputs` dict `_pipeline.py:630`; P `write_v2_outputs` `v2_cloud_materialize.py:531`; C `_write_mask_outputs` `run.py:996` | P `v2_runner.py:289-296` (via `run_pipeline`) | C `run.py:524`, `api.py:431`, `demo.py:246` |
| pandas chunked | E `classify_job`/`_chunked_rejection` `_planner.py:325`; routed by `decide_chunk_route` `_pipeline.py:397` (`auto_chunk` default True) | E planner runtime gates `_planner.py:496-640`; P `classify_streaming_eligibility` `streams.py:454` | E `run_mask_pipeline_chunked` via `run_mask_chunked` `_pipeline_route_exec.py:531` | E per-chunk aggregation `_chunked.py` + `execution_telemetry` full_frame stamp | Caller: engine returns `outputs` dict; P incremental `open_target_writer` `streams.py:420`; C `_write_chunked_output` `run.py:926` | P `v2_runner.py:352-358` | C `run.py:893-900` |
| DuckDB out-of-core | E `decide_execution_route` (out_of_core branch) `_pipeline_routing.py:363-397,489`; sub-route `decide_route` `out_of_core/_route_policy.py:94` | E `resolve_ooc_memory_limit`+`enforce_ooc_memory_preflight` `_pipeline_route_exec.py:326-395`; P `_should_use_out_of_core_relationship_path` `v2_out_of_core.py:165` + `check_out_of_core_compatibility` `:295` | E `run_fk_out_of_core` `out_of_core/_runner.py` (via `run_out_of_core_route` `_pipeline_route_exec.py:180`) | E `execution_telemetry` ooc `_pipeline_route_exec.py:412` + residency warning `:425-431` | E sink `ParquetTransactionalSink` (atomic `os.replace` `_transactional_sink.py:258`); P stages then `_materialize_staged_sequential_outputs` `v2_sequential.py:326` | P `v2_out_of_core.py:322-330` (DIRECT, bypasses run_pipeline) | none |
| sequential (bounded FK) | E `decide_execution_route` (sequential branch) `_pipeline_routing.py:399-420,491`; predicate `_sequential_eligible` `:172` | E via router; P `_should_use_sequential_relationship_path` `v2_sequential.py:109` | E `run_sequential` `_sequential.py` (via `run_sequential_route` `_pipeline_route_exec.py:100`) | E `execution_telemetry` sequential `_pipeline_route_exec.py:156` | E sink `ParquetTransactionalSink`; P `_materialize_staged_sequential_outputs` `v2_sequential.py:281-342` | P `v2_sequential.py:191-199` (DIRECT, bypasses run_pipeline) | none |
| native lane 4a (passthrough/redact/truncate) | E `static_candidacy` `_native_route.py:106` + `peek_and_admit` `:210`; gated `native_route_enabled=False` `_pipeline.py:453` | E `_validate_ledger` `_native_route_exec.py:186` | E `_run_native_streaming` `_native_route_exec.py:367`; kernels `native/_kernels_scalar.py` | E `NativeRouteReport`/`NativeRouteLedger` `_native_route.py:258-308`; on `ExecutionResult.native_route` `_pipeline.py:638` | E sink commit/abort `_native_route_exec.py:428-441` (streaming) | none (flag never set) | none (flag never set) |
| native columnar 4b (Faker/pool) | E `plan_native_route`/`_static_route_decision` `native/_dispatch.py:231-326` | E in `run_native_or_oracle_chunked` | E `run_native_or_oracle_chunked` `native/_dispatch.py:577` | E `NativeRouteEvidence` `native/_dispatch.py:146`; `RouteDiagnostics` `native/_route_diagnostics.py` (unwired) | none - no production caller | none | none |
| pandas adapter (5a) | E default substrate branch `_substrate.py:100-110` | E `require_*` knob validation `_substrate.py:51-73` | E `PandasExecutionAdapter` `_pandas_adapter.py` | E adapter stamp `_pipeline_finalize.stamp_execution_metrics` | (inherits host route) | P builds `PandasExecutionAdapter` directly `v2_sequential.py:191` | C `select_execution_adapter` `run.py:876`, `demo.py:245` |
| Polars adapter (5b) | E `resolve_substrate` default `"polars"` `_substrate.py:25,28`; EXPLAIN mirror `_polars_native_rejection` `_planner.py:281` | E `require_*` `_substrate.py:51-73`; polars `fallback_to_pandas` for FK/composite | E `PolarsExecutionAdapter` `polars/_polars_adapter.py` (via `select_execution_adapter` `_substrate.py:100-107`) | E adapter stamp | (inherits host route) | not selected (platform never passes substrate) | C only if `--substrate polars` on chunked (`run.py:876`) |

---

## B. Duplicated responsibilities (consolidation targets)

1. **Route-eligibility decision computed independently in engine and platform (the central duplication).**
   The engine owns the single live router `decide_execution_route` (`_pipeline_routing.py:221`), but the platform BYPASSES `run_pipeline` for FK/streaming routes and re-derives the decision:
   - streaming eligibility: engine `_chunked_rejection` `_planner.py:325` vs platform `classify_streaming_eligibility` `streams.py:454-562` (the platform re-implements the five planner-parity gates by hand at `streams.py:523-538`).
   - sequential eligibility: engine `_sequential_eligible` `_pipeline_routing.py:172` vs platform `_should_use_sequential_relationship_path` `v2_sequential.py:109-121`.
   - out-of-core eligibility + size threshold: engine `decide_execution_route` out_of_core gate `_pipeline_routing.py:363-370` vs platform `_should_use_out_of_core_relationship_path` `v2_out_of_core.py:165-223` (re-uses engine constant `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` but owns the branch).
   Two independent implementations of the same routing decision, kept in sync only by shared exported constants. This is the primary §3.5 overlap the consolidation must remove.

2. **Out-of-core sub-route threshold `decide_route` (`_route_policy.py:94`) vs the outer OOC-admission threshold (`decide_execution_route`).** Two distinct row thresholds (`REORDER_PARENT_KEY_THRESHOLD=2M` vs `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT=5M`) that both gate "how big before we change execution strategy" - documented as intentionally independent (`_pipeline_routing.py:30-33`) but a consolidation target for a single physical-plan cost model.

3. **Memory/capacity admission priced in three places.**
   - engine full-frame byte estimate `_mem_estimate.fits` (via `decide_execution_route` `_pipeline_routing.py:428-488`),
   - engine OOC preflight `enforce_ooc_memory_preflight` `_pipeline_route_exec.py:390`,
   - platform `estimate_admission` `admission.py:265` + `fk_aware_multiplier` `admission_fk.py:256`.
   Three separate memory models for one job.

4. **Publication / atomicity implemented twice with the same POSIX-rename pattern.**
   - engine `ParquetTransactionalSink.commit` `os.replace` `_transactional_sink.py:258-286` (sequential/ooc/native routes),
   - platform `_materialize_file_output` atomic tmp+`os.replace` `v2_cloud_materialize.py:53` (full-frame/chunked routes) and `FileTargetWriter` `streams.py:253-257`.
   Full-frame and chunked routes return an in-memory `outputs` dict and rely on the CALLER to publish atomically; sequential/ooc/native publish through the engine sink. Two atomicity owners depending on route.

5. **Execution telemetry / route-evidence shape stamped per route.** `execution_telemetry` `_pipeline_route_exec.py:63` is shared for full-frame/sequential/ooc, but the native lane hand-rolls its own `_execution_envelope` `_native_route_exec.py:328` and the platform adds `node_execution_mode` labels (`v2_out_of_core.py:405`) plus `memory_ceiling` classification. Route-evidence has no single owner.

6. **Native route evidence: two parallel, non-shared implementations.** `NativeRouteLedger` (4a, wired) `_native_route.py:267` vs `NativeRouteEvidence`/`RouteDiagnostics` (4b, unwired) `native/_dispatch.py:146` + `native/_route_diagnostics.py`. Same job ("prove which route ran"), two schemas.

---

## C. Public engine APIs, config fields, artifacts, determinism rules, error contracts routes depend on

Public API (name -> where defined):
- `run_pipeline` -> `execution/_pipeline.py:135` (exported `__init__.py:103`, `execution/__init__.py:176`)
- `run_pipeline_isolated` -> `execution/__init__.py:177`
- `run_mask_pipeline_chunked` -> `execution/_chunked.py` (exported `execution/__init__.py:175`)
- `run_sequential` -> `execution/_sequential.py` (exported via `execution`)
- `run_fk_out_of_core` -> `execution/out_of_core/_runner.py` (exported `out_of_core/__init__.py:24`)
- `select_execution_adapter`, `resolve_substrate`, `VALID_SUBSTRATES`, `require_bool`, `require_positive_int` -> `execution/_substrate.py:76,28,24,64,51`
- `PandasExecutionAdapter` / `PolarsExecutionAdapter` / `ExecutionAdapter` protocol -> `execution/_pandas_adapter.py`, `polars/_polars_adapter.py`, `_adapter.py`
- `TransactionalSink` / `ParquetTransactionalSink` -> `execution/_transactional_sink.py:51,124` (exported `execution/__init__.py:152,161`)
- `classify_job` / `ExecutionPlan` -> `execution/_planner.py:177,157`
- `check_out_of_core_compatibility`, `resolve_ooc_memory_limit`, `resolve_budget`, `LazySource` -> `out_of_core/__init__.py:15,8,,25`
- `run_native_or_oracle_chunked`, `plan_native_route`, `NativeRouteEvidence` -> `execution/native/__init__.py:16,17` (public export but no production caller)
- `generate_tables` -> `generation/synthesize.py` (exported `__init__.py:135`)
- `compile_plan`, `PipelineConfig` -> `plan/_compile.py`, `config/_pipeline.py` (exported `__init__.py:381,340`)
- `ExecutionResult` (fields: outputs, timings, boundary_conversion_ms, warnings, quality_metrics, table_kinds, row_errors, native_route) -> `execution/_adapter.py`

Config fields routes read (name -> consumer):
- `execution_mode` (`auto`/`sequential`/`full_frame`/`out_of_core`) -> `decide_execution_route` `_pipeline_routing.py:229` (run_pipeline kwarg, NOT config; platform/CLI never pass it)
- `substrate` / env `DECOY_SUBSTRATE` -> `resolve_substrate` `_substrate.py:40`
- `native_route_enabled` (kwarg, default False) -> `maybe_run_native_route` `_native_route.py:310`
- `auto_chunk`, `chunk_size_rows`, `auto_chunk_threshold_rows`, `out_of_core_threshold_rows`, `full_frame_reject_rows`, `out_of_core_budget_bytes`, `out_of_core_reorder_threshold_rows`, `use_byte_estimate_routing`, `use_probe_routing`, `fpe_chunk_count`, `max_workers`, `fallback_to_pandas` -> `run_pipeline` signature `_pipeline.py:146-164`
- `relationships` -> FK routing / `_sequential_eligible` `_pipeline_routing.py:206`
- `validators`, `quarantine`, `vault`/`vault_writer`, `fidelity_report` -> sequential/native disqualifiers `_pipeline_routing.py:210-215`, `_native_route.py:169-174`
- `global_settings.mask_secret_ref` -> `resolve_key_provider` `_pipeline.py:328`

Artifacts:
- Parquet output tables via `ParquetTransactionalSink` (atomic dir rename) / caller `_write_mask_outputs`
- `ExecutionResult.quality_metrics["execution"|"execution_plan"|"execution_adapter"|"residency"|"fidelity_reports"]`
- `ExecutionResult.native_route` (`NativeRouteReport`)
- Compat corpus `tests/integration/compat_corpus/` (locked cross-version artifacts, compatibility-contract 3.1/3.2)

Determinism rules:
- `SEED_PROTOCOL_VERSION = 6` -> `determinism/_derive.py:118` (stamped into Plan `plan/_compile.py:457`)
- pandas full-frame is the pinned oracle; all routes are byte-parity to it (parity tests `tests/parity/test_out_of_core_fk_parity.py`, `tests/parity/native/`)
- deterministic derivation `DeriveContext` / `derive_index` -> `determinism/_derive.py`
- output row order / null positions / dtypes preserved across route + batch + thread boundaries (plan §4.2, §9.4)

Error contracts (code -> raised where):
- `fk_full_frame_oom_risk_rejected` / `fk_full_frame_oom_risk_rejected_estimated` -> `_pipeline_routing.py:475,524,540`
- `invalid_substrate` / `invalid_execution_knob` -> `_substrate.py:37,44,58,66`
- `out_of_core_reorder_threshold_invalid` -> `_route_policy.py:82,88`
- `out_of_core_memory_detection_failed`, `out_of_core_fanin_exceeds_budget`, `out_of_core_parent_rows_unresolved`, `out_of_core_parent_column_missing`, `out_of_core_source_missing`, `out_of_core_relationship_cycle`, `out_of_core_fk_key_dtype_unsupported`, `out_of_core_sort_row_too_wide` -> out_of_core package + `_pipeline_route_exec.py:344,520`, `_route_policy.py:178,229`
- `ConfigError` for forced-mode ineligibility -> `_pipeline_routing.py:381-419`
- `RowErrorsFailedError` -> chunked/native fail-closed `_chunked.py`, `native/_route_diagnostics.py:198`
- `KeyedStrategyRequiresSecret` (GA) -> `keyprovider` (`_pipeline.py:319-325` gate)
- platform capacity codes `out_of_core_insufficient_memory` / `out_of_core_fanin_exceeds_budget` -> `decoy/cli/run.py:41`

---

## D. Route status classification (evidence-based)

| Route | Status | Evidence |
|---|---|---|
| pandas full-frame | **oracle + production** | Pinned compatibility oracle (plan §4.2, `run_pipeline` terminal branch `_pipeline.py:527`). Production caller: platform `run_pipeline` full-frame `v2_runner.py:289`; CLI plain `run.py:524`. Both an oracle (byte-parity target) and the live fallthrough route. |
| sequential (bounded FK) | **production** | Default FK route in engine (`decide_execution_route` sequential branch); platform calls `run_sequential` directly `v2_sequential.py:191`. |
| DuckDB out-of-core | **production** | Engine auto-route for large FK; platform Route 4 wired `v2_orchestrator.py:303`, calls `run_fk_out_of_core` directly `v2_out_of_core.py:322`. (Note contradicts stale `admission_fk.py:86-104` claim it is unreachable.) |
| pandas chunked | **production (gated)** | Engine `auto_chunk` default True; platform gated behind `settings.streaming_execution_enabled` default OFF `streams.py:498`; CLI opt-in `--chunked`. Production-capable, off-by-default at platform. |
| pandas adapter (5a) | **production** | Backs full-frame + sequential + chunked; platform constructs it directly. |
| Polars adapter (5b) | **held / default-but-unexercised-by-callers** | `resolve_substrate` default is `"polars"` (`_substrate.py:25`) so a bare engine call selects it, BUT platform never passes substrate (defaults not exercised - platform builds `PandasExecutionAdapter` directly) and CLI forwards substrate only on `--chunked`. FK/composite fall back to pandas oracle. Plan §5/§6.1 marks Polars frozen pending usage evidence. |
| native lane 4a (passthrough/redact/truncate) | **experimental / held (off by default)** | `native_route_enabled=False` default `_pipeline.py:164`; token absent from platform and CLI (never enabled). Production seam exists, not activated. Plan Phase 3 is where it would be turned on. |
| native columnar 4b (Faker/pool) | **experimental (no production caller)** | `run_native_or_oracle_chunked`/`plan_native_route` referenced only by `scripts/native-baseline/*` benchmarks and `tests/`; zero production call sites in engine, platform, or CLI. `RouteDiagnostics` explicitly standalone/unwired `native/_route_diagnostics.py:24-30`. |

---

## E. Flagged UNKNOWN / unresolved production callers and publication paths (Task 0.3 exit-gate risks)

1. **Documented contract contradicts wired reality (HIGH).** `admission_fk.py:86-104` module docstring asserts "decoy-engine's SC1/SC2 out-of-core route is not reachable at all through this platform's job runner today." Route 4 (`v2_out_of_core.py`, wired at `v2_orchestrator.py:303-314`) contradicts this. The out-of-core route IS reachable. An architecture review relying on that docstring would mis-map ownership. Needs reconciliation before the exit gate.

2. **Split publication ownership by route is a real orphan risk (HIGH).** Full-frame and chunked routes do NOT publish through the engine `TransactionalSink`; they return an in-memory `outputs` dict and depend on the caller (platform `write_v2_outputs` / CLI `_write_mask_outputs`) to write atomically. Sequential/ooc/native publish through the engine sink. So "publication owner" is route-dependent and split across repos; the consolidation's single coordinator (§8.3) must absorb both. Not unknown, but explicitly two owners.

3. **Native columnar route 4b has a public export but no owner.** It is exported from `native/__init__.py` and reachable by any external engine consumer, yet has no production caller and its diagnostics collector is unwired. Whether it is destined for the physical-plan operator layer (Phase 4/5) or is dead weight to retire (Task 4.7) is not decided in-code. Flag: no production ownership, live public surface.

4. **Cloud staging path (`v2_cloud_staging.py`) not fully traced.** The platform stages cloud sources before routing (`v2_orchestrator.py` staging step) and materializes to s3/gcs via `v2_cloud_materialize.py`. The exact atomicity guarantee for cloud (non-POSIX-rename) targets vs the local `os.replace` guarantee was not confirmed line-by-line in this pass. The engine sink's atomicity contract (`_transactional_sink.py`) is POSIX-rename and same-filesystem only; cloud publication atomicity is a separate platform mechanism (`streams.py:314-341` spool+move tail) whose guarantee should be confirmed before the coordinator claims one atomicity contract.

5. **`estimate_job_capacity` (CLI) vs `check_out_of_core_compatibility` (engine) coupling.** CLI preflight calls an engine attr `estimate_job_capacity` (`preflight.py:449`) that degrades to "not checked" if absent (`:403-410`). Its relationship to the engine's actual OOC admission (`enforce_ooc_memory_preflight`) was not confirmed to be the same code path - a CLI PASS may not guarantee the engine admits. Advisory-only today, but a consolidation that promises "one admission decision" must reconcile them.

No other unknown production caller was found: engine, platform, and CLI route invocations all trace to named entrypoints above. The two native routes are the only public execution surfaces with no production caller.

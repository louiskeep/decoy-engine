# Codex independent code-only audit (2026-09-30)

Status: record

Cross-check for the run audit, produced from code reading only (gpt-5.6-sol, read-only). Its revision header's commits do not match this audit's own pinned environment (`environment.json`: platform `origin/main` `0701a954`, CLI `origin/main` `b8274b01`). The value it labels "decoy CLI" (`0701a95422e405cff76eb38c12cfb4f372b151d4`) is in fact this audit's real platform commit, not a CLI commit at all. The value it labels "decoy-platform@origin/main" (`e04b2a2f1c83a8c92078aa0ea66c808efee05025`) is not a platform commit either -- it resolves in the `decoy-engine` repo to the PR #178 merge commit, not to anything in `decoy-platform`. So this is not a clean two-way swap: only the CLI row can be read as "actually the platform commit"; the platform row's value names a different repository's commit entirely, which means the actual platform tree Codex read is unverified, not merely mislabeled. Kept verbatim below except for this note and the en-dash-to-hyphen and stray-artifact cleanup described in `docs/records/audit-2026-09-30/stage-2d-summary.md`.

Audit basis: code only, no execution beyond repository reads. Revisions examined:

- `decoy-engine` `8dc559e554fd48d0996a7dcee02f8a57d94cc2db`
- `decoy-platform@origin/main` `e04b2a2f1c83a8c92078aa0ea66c808efee05025`
- `decoy` CLI `0701a95422e405cff76eb38c12cfb4f372b151d4`

The central result is: **no large platform masking path executes Rust companion kernels today.** Large independent tables use the pandas chunked adapter; large supported FK trees use DuckDB plus Python/Arrow kernels. Rust companion kernels are reachable only through the narrow, resident, single-table, non-chunked Parquet unified slice.

## Legend and routing caveat

- `US-R`: unified slice, compiled Rust companion kernel.
- `US-A`: unified slice, Arrow/Python native kernel without the companion.
- `FF-PD`: resident pandas full-frame.
- `CH-PD`: engine `run_mask_pipeline_chunked`, pandas per chunk.
- `PS-PD`: platform Phase 1 stream feeding `run_mask_pipeline_chunked`; pandas per chunk.
- `SEQ-PD`: table-at-a-time sequential route, pandas.
- `OOC-DP`: DuckDB scans/joins plus Python/Arrow mask kernels.
- `GEN-PY`: Python/NumPy/Faker generation.
- `SUB-PL`: Polars subset closure/materialization.

The row bands are not sufficient to predict every route:

- Single-table platform streaming switches at **256 MiB compressed/source bytes**, not a row count (`decoy-platform@origin/main:api/config.py:156-172`; `api/jobs/_phase1_eligibility.py:254-270`).
- Engine FK auto-routing defaults to a **byte-level memory estimate and optional probe**; its 5M/7.5M row thresholds are the rollback path when byte routing is disabled (`src/decoy_engine/execution/_pipeline_routing.py:257-295,433-524`).
- Platform FK routing still uses the 5M Parquet-footer threshold (`decoy-platform@origin/main:api/jobs/v2_out_of_core.py:165-223`).

The shape tables below use a valid, stable-dtype hash-style base case. The strategy and I/O matrices afterward show the exceptions.

## Capability map by shape

### 1. Single-table mask

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| **Engine / platform wrapper / CLI default:** resident Parquet may take `US-R` for hash/deterministic categorical/bucket perturb/group key/date shift, or `US-A` for redact/truncate/passthrough; otherwise `FF-PD`. **Normal worker:** `PS-PD` only if the source already exceeds 256 MiB and passes Phase 1; otherwise wrapper route. **Adaptive:** immutable plan can select streaming only when a claim plan exists, otherwise full-frame. **CLI `--native`:** succeeds only if actual compiled evidence appears; `--no-native` forces `FF-PD`; `--chunked` is `CH-PD`. **Subset:** `SUB-PL` if a local-Parquet subset block is invoked; it does not mask. (`src/decoy_engine/execution/_unified_slice_admission.py:217-321`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:117-177,254-270`; `src/decoy/_native_gate.py:462-506`; `src/decoy_engine/subset/_api.py:1-7`) | **Engine / wrapper / CLI default:** chunk-compatible jobs take `CH-PD`; the 100k gate prevents the unified Rust slice from being reached. Incompatible/`when`/unstable-dtype jobs remain `FF-PD`. **Normal worker:** `PS-PD` at ≥256 MiB for the four Phase 1 strategies, otherwise eager wrapper → `CH-PD`. **Adaptive:** forced `streaming` or `full_frame`, but the latter still auto-chunks inside the engine. **CLI `--native`:** the engine can finish via `CH-PD`, then CLI rejects before writing because there is no compiled-kernel evidence. (`src/decoy_engine/execution/_planner.py:92-97,218-240,361-404,430-574`; `decoy-platform@origin/main:api/jobs/v2_runner.py:404-442`; `src/decoy/_native_gate.py:445-475`) | Same routes as the middle tier. Phase 1 can make the I/O bounded, but its masking remains `PS-PD`. If Phase 1 is unavailable, the platform wrapper first reads the whole table, then the engine may `CH-PD`; chunking at that late point does not undo the eager read. There is no non-FK OOC route. (`decoy-platform@origin/main:api/jobs/v2_runner.py:279-311,314-442`; `src/decoy_engine/execution/_pipeline_sources.py:64-96`; `src/decoy_engine/execution/_pipeline.py:507-552`) |

### 2. Single-table generate

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| All normal entries use `GEN-PY`. Eligible Faker columns pool only at ≥50k; otherwise Faker is called per row. Claim streaming rejects generation. Adaptive has only full-frame. CLI `--native` and `--chunked` reject generate jobs; default/`--no-native` generate normally. (`src/decoy_engine/generation/_faker_pool.py:221-273`; `src/decoy_engine/generation/synthesize.py:381-456`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:151-155`; `src/decoy/cli/run.py:535-543`; `src/decoy/_native_gate.py:320-331`) | `GEN-PY`; the pool optimization changes Faker selection cost, not the execution route. No bounded generation route exists. Platform admission may defer/reject based on its generation-output reservation. (`src/decoy_engine/generation/synthesize.py:109-198`; `decoy-platform@origin/main:api/jobs/admission.py:973-993`) | `GEN-PY`, resident output. There is no 5M transition, chunked generator, OOC generator, or Rust production generator. (`src/decoy_engine/execution/_pipeline.py:250-258,517-552`; `src/decoy_engine/generation/synthesize.py:139-198`) |

### 3. Mask + generate

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| `GEN-PY` runs first, generated tables are merged into sources, then masking runs `FF-PD`. Unified, sequential, OOC, claim streaming, CLI `--native`, and CLI `--chunked` do not admit mixed jobs. Adaptive sees full-frame only. (`src/decoy_engine/execution/_pipeline_generate_mask.py:95-159`; `src/decoy_engine/execution/_pipeline_routing.py:172-228`; `src/decoy_engine/execution/_unified_slice_admission.py:257-259`; `src/decoy/cli/run.py:535-543`) | Same resident two-stage route: `GEN-PY → FF-PD`. Presence of a generate table prevents the single-mask auto-chunk classification. (`src/decoy_engine/execution/_planner.py:205-240,259-279`; `src/decoy_engine/execution/_pipeline_generate_mask.py:95-159`) | Same route with no large-tier bounded fallback. Engine does not apply the FK full-frame rejection to a no-relationship mixed job; platform host admission is the external protection. (`src/decoy_engine/execution/_pipeline_routing.py:519-524`; `decoy-platform@origin/main:api/jobs/admission.py:973-993`) |

### 4. Multiple mask tables, no relationships

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| Engine/wrapper/CLI default use `FF-PD`: unified requires exactly one configured table, and auto-chunk requires exactly one mask table. Normal worker can nevertheless choose multi-table `PS-PD` if aggregate/source bytes meet 256 MiB and every table passes Phase 1. CLI `--chunked` manually processes tables one by one with `CH-PD`. (`src/decoy_engine/execution/_unified_slice_admission.py:257-259`; `src/decoy_engine/execution/_planner.py:205-240`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:77-134`; `src/decoy/cli/run.py:1080-1105`) | Normal worker normally becomes multi-table `PS-PD` once the byte floor is crossed; otherwise `FF-PD`. The coordinator publishes one completed table at a time. Adaptive can force the pre-priced streaming or full-frame route. (`decoy-platform@origin/main:api/jobs/v2_stream_coordinator.py:92-220,256-300`; `decoy-platform@origin/main:api/jobs/dispatch_route.py:70-140`) | Same. `PS-PD` bounds masking to a chunk/table, but this is not a Rust or OOC route. CLI default remains resident `FF-PD`; manual `--chunked` remains local-only `CH-PD`. (`decoy-platform@origin/main:api/jobs/v2_runner.py:375-442`; `src/decoy/cli/run.py:1039-1105`) |

### 5. FK tree

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| **Engine:** `FF-PD` if the byte estimator/probe confirms fit; otherwise `OOC-DP` when compatible or `SEQ-PD` when not. With byte routing disabled, this tier is `SEQ-PD`. **Wrapper:** same engine choice, but sources were already eagerly loaded. **Normal worker:** always `SEQ-PD`. **Adaptive:** can force available full-frame/sequential routes. **CLI default:** engine choice, but OOC has resident inputs/no sink; `--native` ultimately rejects; `--chunked` supports only the narrow self-mask-safe FK contract. **Subset:** `SUB-PL`. (`src/decoy_engine/execution/_pipeline_routing.py:433-503`; `decoy-platform@origin/main:api/jobs/v2_sequential.py:109-121`; `src/decoy_engine/execution/_pipeline_route_exec.py:254-288`; `src/decoy_engine/execution/_chunked.py:250-355`) | Normal worker remains `SEQ-PD` below 5M. Engine direct remains byte-estimate driven. No FK path uses Rust companion kernels. (`decoy-platform@origin/main:api/jobs/v2_out_of_core.py:165-223`; `src/decoy_engine/execution/_pipeline_route_exec.py:100-176`) | Normal worker uses `OOC-DP` only for every-source-Parquet, no-transform, compatible single-parent acyclic recipes; otherwise `SEQ-PD`. DuckDB streams source/join batches, but masking is Python/Arrow. Adaptive can force full-frame, OOC, or sequential if the plan priced it. (`decoy-platform@origin/main:api/jobs/v2_out_of_core.py:20-74,165-223,252-307`; `src/decoy_engine/execution/out_of_core/_compat.py:19-138,160-320`; `decoy-platform@origin/main:api/jobs/dispatch_route.py:90-140`) |

### 6. FK diamond / multiple parents for one child key

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| Engine: `FF-PD` on confirmed fit, otherwise `SEQ-PD`; OOC rejects the multi-parent child. Normal platform worker selects `SEQ-PD`. Adaptive may force full-frame or sequential. CLI native has no compiled evidence. Subset supports the graph through Polars joins. (`src/decoy_engine/execution/out_of_core/_compat.py:182-197`; `src/decoy_engine/execution/_pipeline_routing.py:471-474`; `decoy-platform@origin/main:api/jobs/v2_sequential.py:109-121`; `src/decoy_engine/subset/_closure.py:113-191`) | Same. There is no row-tier transition to OOC for this shape because compatibility, not size, excludes it. (`src/decoy_engine/execution/out_of_core/_compat.py:182-197`; `decoy-platform@origin/main:api/jobs/v2_out_of_core.py:290-299`) | Platform’s cheap ≥5M gate may attempt OOC, but the compiled compatibility check declines before sink construction and the orchestrator falls back to `SEQ-PD`. (`decoy-platform@origin/main:api/jobs/v2_out_of_core.py:290-299`; `decoy-platform@origin/main:api/jobs/v2_orchestrator.py:366-418`) |

### 7. Self-referential FK

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| Engine: `FF-PD` if fit, otherwise `SEQ-PD`; self-edges are deliberately allowed by sequential ordering but rejected OOC. Normal worker uses `SEQ-PD`. Subset supports self-edges through its monotone fixpoint. (`src/decoy_engine/execution/_pipeline_routing.py:143-169`; `src/decoy_engine/execution/_sequential.py:602-647`; `src/decoy_engine/execution/out_of_core/_compat.py:421-437`; `src/decoy_engine/subset/_closure.py:127-135`) | Same `SEQ-PD` bounded fallback; no Rust. (`src/decoy_engine/execution/_pipeline_routing.py:471-474`; `decoy-platform@origin/main:api/jobs/v2_sequential.py:191-207`) | Platform may enter the OOC gate at ≥5M, but OOC declines `out_of_core_self_referential_fk_unsupported`, then execution falls back to `SEQ-PD`. (`src/decoy_engine/execution/out_of_core/_compat.py:421-437`; `decoy-platform@origin/main:api/jobs/v2_orchestrator.py:366-418`) |

### 8. Cross-table FK cycle

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| Engine direct keeps `FF-PD` when its estimator confirms fit; if not, it rejects because neither sequential nor OOC can order the cycle. **Normal platform worker has a divergence:** its cheap predicate selects sequential without checking cycles, and `run_sequential` then raises `relationship_cycle`. Adaptive can succeed only if its immutable plan forces the always-available full-frame route. Subset supports cycles. (`src/decoy_engine/execution/_pipeline_routing.py:324-330,475-499`; `decoy-platform@origin/main:api/jobs/v2_sequential.py:109-121`; `src/decoy_engine/execution/_sequential.py:640-647`; `src/decoy_engine/subset/_closure.py:127-135`) | Same. Normal platform does not fall back from the sequential cycle error to full-frame. (`decoy-platform@origin/main:api/jobs/v2_orchestrator.py:315-350`; `src/decoy_engine/execution/_sequential.py:640-647`) | Engine direct rejects before read unless full-frame is explicitly forced. Normal platform may first try OOC, then sequential; OOC declines the cycle and sequential raises. Adaptive full-frame is the only platform worker route that can execute it today. (`src/decoy_engine/execution/out_of_core/_compat.py:202-219`; `src/decoy_engine/execution/_pipeline_routing.py:475-499`; `decoy-platform@origin/main:api/jobs/v2_orchestrator.py:366-418`) |

### 9. FK plus validators, vault, fidelity, or post-validation

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| **Engine direct:** validators, actual vault writer, fidelity, or post-validation disqualify sequential/OOC, so the job is `FF-PD` or rejected if the estimator cannot confirm fit. Unified also declines them. **Platform:** validators and vault still run on its `SEQ-PD` route; validators reload every staged output together, while vault rereads one source/output table at a time. Platform never passes `fidelity_report=True`. Post-validation enabled-but-not-enforced is best-effort and is skipped on sequential/OOC; enforce mode rejects those routes before writing. (`src/decoy_engine/execution/_pipeline_routing.py:172-228`; `src/decoy_engine/execution/_unified_slice_admission.py:246-255`; `decoy-platform@origin/main:api/jobs/v2_sequential.py:390-419`; `decoy-platform@origin/main:api/jobs/post_validation_policy.py:11-18,87-103`) | Same. Platform’s relationship route deliberately differs from engine `run_pipeline`: it implements validators/vault around the staged sequential result instead of forcing full-frame. (`decoy-platform@origin/main:api/jobs/v2_sequential.py:352-379,398-427`; `src/decoy_engine/execution/_pipeline.py:421-436`) | For compatible Parquet trees the platform may mask via `OOC-DP`, then lose the end-to-end cardinality bound: validators make every output resident; vault makes the largest source/output resident. Enforced post-validation rejects OOC/sequential; warn-only post-validation does not steer and therefore produces no scan on those routes. (`decoy-platform@origin/main:api/jobs/v2_out_of_core.py:20-74,104-114`; `decoy-platform@origin/main:api/jobs/post_validation_policy.py:11-18,87-103`) |

### 10. FK with generate tables

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| All supported normal entries use resident `GEN-PY → FF-PD`; engine and platform sequential gates explicitly exclude generate tables. Claim streaming and CLI native/chunked reject. Adaptive has full-frame only. (`src/decoy_engine/execution/_pipeline_routing.py:172-228`; `decoy-platform@origin/main:api/jobs/v2_sequential.py:109-121`; `src/decoy_engine/execution/_pipeline_generate_mask.py:95-159`) | Same, with no bounded route. (`src/decoy_engine/execution/_pipeline_routing.py:519-524`; `decoy-platform@origin/main:api/jobs/v2_orchestrator.py:264-266`) | Same or platform host-admission rejection. There is no generated-parent OOC/sequential production route. (`decoy-platform@origin/main:api/jobs/v2_sequential.py:7-10,109-121`; `decoy-platform@origin/main:api/jobs/admission.py:973-993`) |

### 11. Subset-only

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| Engine `run_subset` and CLI `decoy subset` use `SUB-PL`: local Parquet only, key-column projection, Polars closure, then one full table at a time for materialization. Platform has no `run_subset` call site. (`src/decoy_engine/subset/_api.py:77-187,203-211`; `src/decoy_engine/subset/_keys.py:42-57`; `src/decoy/cli/subset.py:231-254`) | Same; no alternate size-tier route. Key frames remain resident. (`src/decoy_engine/subset/_keys.py:1-8,42-57`; `src/decoy_engine/subset/_closure.py:113-191`) | Same. Peak is documented as largest full table plus all key frames because materialization calls `collect()` before writing; this is not streaming OOC. (`src/decoy_engine/subset/_materialize.py:10-16,54-72`) |

### 12. Subset, then mask

| Under 100k | 100k-<5M | 5M-100M+ |
|---|---|---|
| Two explicit jobs: `SUB-PL`, then feed the written Parquet files to ordinary masking. The subset API does not invoke masking automatically. The mask step follows the earlier shape tables based on the surviving row counts. (`src/decoy_engine/subset/_api.py:1-7,179-187`; `src/decoy/cli/subset.py:244-254`) | Same composition. If a surviving independent table remains ≥100k, ordinary engine/CLI masking usually becomes `CH-PD`; an FK output follows sequential/OOC routing. (`src/decoy_engine/execution/_planner.py:218-256`; `src/decoy_engine/execution/_pipeline_routing.py:433-503`) | Same. Subsetting can reduce the second stage below a routing threshold, but there is no fused subset+mask executor and no automatic transfer of subset outputs into `run_pipeline`. (`src/decoy_engine/subset/_api.py:1-7`; `src/decoy_engine/subset/_materialize.py:54-72`) |

## Strategy-stage backend matrix

| Strategy family | Current full-frame/unified behavior | Chunked / Phase 1 | OOC FK |
|---|---|---|---|
| Hash | `US-R` only for the narrow unified shape; otherwise pandas. (`src/decoy_engine/execution/_unified_slice_admission.py:83-135,154-171`) | Supported, but implemented by the pandas adapter in both engine chunking and platform Phase 1. (`src/decoy_engine/execution/_chunked_fk.py:76-109`; `src/decoy_engine/execution/_chunked.py:389-416`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:67-74`) | Supported parent/payload strategy, executed by OOC Python/Arrow mask code, not the Rust companion. (`src/decoy_engine/execution/out_of_core/_compat.py:28-47`; `src/decoy_engine/execution/out_of_core/_mask.py:97-173`) |
| Categorical deterministic | `US-R` when deterministic/config/type admission passes; otherwise pandas. (`src/decoy_engine/execution/_unified_slice_admission.py:96-135,161-168`) | Chunked only with deterministic mode, namespace, and explicit categories; excluded from Phase 1. (`src/decoy_engine/execution/_chunked.py:181-247`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:208-231`) | Payload only, deterministic only; Python OOC kernel. (`src/decoy_engine/execution/out_of_core/_compat.py:34-47,300-310`) |
| Categorical non-deterministic | pandas full-frame. (`src/decoy/_native_gate.py:257-269`) | Rejected by chunked and Phase 1. (`src/decoy_engine/execution/_chunked.py:214-247`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:216-220`) | Rejected. (`src/decoy_engine/execution/out_of_core/_compat.py:300-310`) |
| `bucket_perturb` | `US-R` for admitted string/date configuration; otherwise pandas. (`src/decoy_engine/execution/_unified_slice_admission.py:90-134,165-168`) | Chunked only with explicit date format; Phase 1 rejects. (`src/decoy_engine/execution/_chunked.py:208-213,294-316`) | Payload only with explicit date format; Python OOC kernel. (`src/decoy_engine/execution/out_of_core/_compat.py:35-47,294-299`) |
| `group_key` | `US-R` under the sibling/type/order gates; otherwise pandas. (`src/decoy_engine/execution/_unified_slice_admission.py:103-134,345-355`) | Engine chunked supports it; platform Phase 1 rejects. (`src/decoy_engine/execution/_chunked_group_key.py:85-85`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:216-220`) | Rejected as cross-row work. (`src/decoy_engine/execution/out_of_core/_compat.py:19-27,281-287`) |
| `date_shift` | `US-R` with admitted string/config shape; otherwise pandas. (`src/decoy_engine/execution/_unified_slice_admission.py:103-135,169-170`) | Chunked requires explicit `date_format`; platform Phase 1 rejects. (`src/decoy_engine/execution/_planner.py:361-404`) | Rejected because OOC lacks format-detection/row-error handling. (`src/decoy_engine/execution/out_of_core/_compat.py:48-76`) |
| Redact / truncate / passthrough | `US-A`: Arrow/Python kernels, not compiled Rust. Otherwise pandas. (`src/decoy_engine/execution/_unified_slice_admission.py:83-94,106-135`; `src/decoy_engine/execution/native/_kernels_scalar.py:1-10`) | Supported, but pandas per chunk; all three are Phase 1 allowlisted. (`src/decoy_engine/execution/_chunked_fk.py:76-94`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:67-74`) | Supported using Arrow/Python kernels. (`src/decoy_engine/execution/out_of_core/_compat.py:28-47`; `src/decoy_engine/execution/out_of_core/_scalar.py:98-250`) |
| Faker generation, pooled | Python/Faker builds the pool; NumPy selects from it. No production unified generator. (`src/decoy_engine/generation/_faker_pool.py:317-410`) | Generation is not chunked. Mask-strategy Faker is conditionally supported by engine chunking but excluded from Phase 1. (`src/decoy_engine/execution/_chunked.py:181-247`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:216-220`) | Rejected: no registry-backed cross-batch pool channel. (`src/decoy_engine/execution/out_of_core/_compat.py:48-63`) |
| Faker generation, non-pooled | Per-row Python Faker call with per-row seeding. (`src/decoy_engine/generation/synthesize.py:427-456`) | No generation chunk route. (`src/decoy/cli/run.py:535-543`) | Not applicable/rejected. (`src/decoy_engine/execution/out_of_core/_compat.py:48-63`) |
| FPE | pandas full-frame; no production unified Rust FPE operator. (`src/decoy_engine/execution/_unified_slice_admission.py:83-94`) | Engine/manual chunk supported, still pandas. Phase 1 rejects. (`src/decoy_engine/execution/_chunked_fk.py:80-109`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:216-220`) | Payload supported through Python FF1 code; parent FK key unsupported because the key surface is narrower. (`src/decoy_engine/execution/out_of_core/_compat.py:28-47,438-453`) |
| `text_mask` / `text_redact` | pandas full-frame. (`src/decoy_engine/execution/_unified_slice_admission.py:83-94`) | Engine chunk supported with dtype/`when` restrictions; Phase 1 rejects. (`src/decoy_engine/execution/_chunked_fk.py:80-94`; `src/decoy_engine/execution/_chunked.py:307-316`) | Payload supported through Python mask code; not a parent-key strategy. (`src/decoy_engine/execution/out_of_core/_compat.py:34-47`) |

### Cross-cutting gates

| Axis | Effect today |
|---|---|
| Column `when` | Unified, auto-chunk, platform Phase 1, and OOC all decline it. Full-frame and sequential pandas apply it. The manual chunk entry is less conservative and rejects only specifically unsafe strategy combinations. (`src/decoy_engine/execution/_unified_slice_admission.py:313-321`; `src/decoy_engine/execution/_planner.py:361-404`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:163-175`; `src/decoy_engine/execution/out_of_core/_compat.py:221-233`; `src/decoy_engine/execution/_pandas_adapter.py:405-412`) |
| Table transforms | Platform applies them with pandas before masking. They disqualify claim streaming and platform OOC. Direct engine and CLI `run_pipeline` have no corresponding transform-application call in their execution spine; the platform owns the live call. (`decoy-platform@origin/main:api/jobs/v2_runner.py:162-215,283-285`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:151-155`; `decoy-platform@origin/main:api/jobs/v2_out_of_core.py:181-197`; `src/decoy/cli/run.py:616-637`) |
| Resident vs lazy | Unified and auto-chunk require a resident `pa.Table`. Full-frame materializes every lazy source; sequential materializes one table at a time; OOC is truly bounded only with all sources lazy plus an incremental sink. (`src/decoy_engine/execution/_unified_slice_admission.py:276-289`; `src/decoy_engine/execution/_pipeline_sources.py:64-115`; `src/decoy_engine/execution/_pipeline_route_exec.py:254-288`) |
| Polars masking | Removed; `"pandas"` is the sole masking substrate. Polars remains only in subset. (`src/decoy_engine/execution/_substrate.py:1-10,23-47,75-104`) |

## Input, transport, and output matrix

| Entry | Input/transport | Output |
|---|---|---|
| Engine `run_pipeline` | Caller supplies resident Arrow or `LazySource`. File descriptors validate CSV/Parquet/fixed-width; cloud descriptor models accept CSV/Parquet only. Route-level boundedness depends on lazy sources, not merely the descriptor. (`src/decoy_engine/execution/_pipeline.py:141-183`; `src/decoy_engine/config/_sources.py:38-130`; `src/decoy_engine/execution/_pipeline_sources.py:1-32`) | Normally returns Arrow tables; sequential/OOC can use a transactional sink. (`src/decoy_engine/execution/_pipeline_route_exec.py:100-176,179-288`) |
| Platform wrapper | Loads local CSV/Parquet/fixed-width eagerly. Direct valid S3/GCS descriptors are first staged locally. (`decoy-platform@origin/main:api/jobs/v2_cloud_staging.py:336-389,392-427`) | Local/S3/GCS, CSV or Parquet. (`decoy-platform@origin/main:api/jobs/v2_cloud_materialize.py:118-130,272-305,433-473,531`) |
| Platform Phase 1 | Cloud sources are staged as local files; the stream reader supports only CSV and Parquet. The eligibility predicate does not check format, so a ≥256 MiB fixed-width job can be admitted and then fail at `iter_source_batches`. (`decoy-platform@origin/main:api/jobs/streams.py:15-27,160-191`; `decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:137-231`) | Incremental local/S3/GCS CSV/Parquet writers. (`decoy-platform@origin/main:api/jobs/streams.py:194-298`) |
| CLI default/native/no-native | Local only. Loader distinguishes Parquet by filename suffix and sends every other path through `pd.read_csv`; consequently a fixed-width source is not read with the engine fixed-width reader. S3/GCS is explicitly rejected. (`src/decoy/cli/run.py:1168-1224`) | Local CSV/Parquet by suffix. (`src/decoy/cli/run.py:1227-1265`) |
| CLI `--chunked` | Local CSV/Parquet only. (`src/decoy/cli/run.py:1039-1075,1108-1127`) | Local CSV/Parquet incrementally. (`src/decoy/cli/run.py:1130-1154`) |
| Subset | Local Parquet only; cloud and CSV/fixed-width reject. (`src/decoy_engine/subset/_api.py:203-211`; `src/decoy_engine/subset/_preflight.py:87-115`) | Parquet only. (`src/decoy_engine/subset/_materialize.py:54-72`) |

## Specific answers

### 1. What does `phase1_streaming_tables` run now?

It runs:

```text
claim classifier
  → phase1_streaming_tables
  → run_claim_time_streaming_route
  → run_v2_pipeline_streaming_multitable
  → _run_v2_pipeline_streaming
  → decoy_engine.run_mask_pipeline_chunked
  → PandasExecutionAdapter.run per chunk
```

Evidence:

- The claim stamps the encoded plan at `decoy-platform@origin/main:api/jobs/queue_worker.py:522-563`.
- The worker consumes that stored decision without reclassification at `api/jobs/v2_orchestrator.py:199-211,276-300`.
- The coordinator delegates each table at `api/jobs/v2_stream_coordinator.py:151-170`.
- `_run_v2_pipeline_streaming` imports and calls `run_mask_pipeline_chunked` at `api/jobs/v2_runner.py:373-419`.
- That engine entry documents pandas as the only substrate and constructs `PandasExecutionAdapter` at `src/decoy_engine/execution/_chunked.py:389-416`; per-chunk adapter execution follows later in the same function.

So the current backend is **Arrow streaming I/O around pandas masking**, not the Rust companion. Engine PR #166 removed the former standalone native streaming lane; it was not replaced inside `run_mask_pipeline_chunked`.

### 2. Do binding-resolved S3/GCS descriptors pass engine validation?

**No. They are stored successfully, then fail at worker-time engine validation.**

The resolver emits:

- S3: valid engine fields plus invalid `connection_id` and `connection_name`.
- GCS: invalid `region`, `connection_id`, and `connection_name`.

Evidence:

- Emission: `decoy-platform@origin/main:api/jobs/binding_resolve.py:128-151`.
- Engine S3 source/target models allow `region`, `endpoint_url`, and `credentials_ref`, but not the two connection fields: `src/decoy_engine/config/_sources.py:71-103`; `src/decoy_engine/config/_targets.py:38-69`.
- Engine GCS models allow neither `region` nor the connection fields: `src/decoy_engine/config/_sources.py:105-125`; `src/decoy_engine/config/_targets.py:72-92`.
- All four models use `ConfigDict(extra="forbid")`: the same cited ranges.
- Job creation rewrites the descriptor and immediately persists it into `Job.yaml_snapshot` without rerunning engine `PipelineConfig` validation: `decoy-platform@origin/main:api/jobs/_service.py:412-474,520-554`.
- The stored snapshot is parsed by the worker, then handed to `run_v2_pipeline_from_config`: `api/jobs/runner.py:60-112,482-559`.
- The actual engine-model validation happens at `api/jobs/v2_submission.py:123-129`, through `PipelineConfig.model_validate(raw)` at `api/jobs/v2_config.py:29-46`.
- The resulting `PipelineConfigError` lands the job failed at `api/jobs/runner.py:113-118`.

This affects both source and target connection bindings. Hand-authored cloud descriptors containing only engine-recognized fields are a separate case and can reach staging/materialization.

### 3. Largest supported single-table platform mask at 1M, 10M, 100M

For a valid CSV/Parquet table using Phase 1-compatible strategies:

| Rows | Actual worker route | Backend |
|---|---|---|
| 1M | If source bytes ≥256 MiB: claim-time platform streaming. If below: full-frame branch eagerly loads the source, then engine auto-chunks because 1M ≥100k. (`decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:254-270`; `src/decoy_engine/execution/_planner.py:92-97,218-240`) | `PS-PD` or late `CH-PD`; both are pandas per chunk. |
| 10M | Same byte-dependent split. There is no special 5M transition for a non-FK table. (`src/decoy_engine/execution/_pipeline_routing.py:519-524`; `decoy-platform@origin/main:api/jobs/v2_runner.py:404-442`) | pandas per chunk. |
| 100M | Same. When Phase 1 is admitted, masking memory is chunk-bounded, subject to claim-time disk and host admission. If the compressed source is somehow below 256 MiB, the worker takes the eager wrapper path before engine chunking. (`decoy-platform@origin/main:api/jobs/admission.py:1030-1079,1129-1135`; `decoy-platform@origin/main:api/jobs/v2_runner.py:279-311`) | pandas per chunk; no Rust companion. |

Therefore code alone cannot truthfully say “1M uses route X, 10M route Y”: the platform boundary is bytes. It can say conclusively that **all three use pandas masking**, assuming the job is supported/admitted.

Important exceptions:

- Non-Phase-1 strategies do not get platform streaming and may require full resident loading.
- `when`, transforms, validators, and vault exclude Phase 1.
- Fixed-width can be incorrectly admitted by the classifier and then fail because the streaming reader supports only CSV/Parquet.

### 4. Minimal changes needed to put the large routes on Rust kernels

The smallest coherent sequence is:

1. Add a compiled-operator batch executor behind `run_mask_pipeline_chunked`. That immediately accelerates engine auto-chunk, platform Phase 1, and CLI `--chunked`.
2. Replace OOC payload-mask dispatch with the same compiled batch operators while leaving DuckDB responsible for scanning and FK joins.
3. Add compiled per-table masking to sequential FK execution; initially keep Python FK-map construction/resolution.
4. Add compiled generation selection for pooled Faker and deterministic generators. Arbitrary non-pooled Faker provider calls remain Python unless providers themselves are reimplemented.
5. A companion-native subset would require new relational closure/materialization kernels; there is no small adaptation from the masking kernels.

The existing shadow and deleted code materially reduce the first two tasks, but none is a production Rust solution by itself:

- PR #144 (`611902f0`) proves chunked shadow parity and ragged-batch behavior, but explicitly says it is shadow-only (`tests/physical/test_shadow_chunked.py:1-16` in that commit).
- PR #145 (`98455dae`) proves an OOC physical-plan dispatch, but delegates wholesale to the existing OOC adapter; it does not replace Python mask kernels (`src/decoy_engine/execution/physical/_shadow_coordinator.py:28-34,237-259` in that commit).
- Deleted PR #166’s `_native_route_exec.py` provides useful stream admission, ledger, schema-drift, sink/commit and failure scaffolding, but its batch executor handled only passthrough/redact/truncate and called Arrow/Python scalar kernels, not the Rust companion (`975a0f03^:src/decoy_engine/execution/_native_route_exec.py:129-176`).

## Ranked gaps

| Rank | Gap and what it unlocks | Evidence | Existing code to reuse | Rough size |
|---|---|---|---|---|
| 1 | **Compiled batch execution inside `run_mask_pipeline_chunked`.** Unlocks Rust for every large independent-table route: engine auto-chunk, platform Phase 1, multi-table Phase 1, and CLI chunking. | Today the entry is pinned to pandas (`src/decoy_engine/execution/_chunked.py:389-416`), while large single tables auto-select it at 100k (`src/decoy_engine/execution/_planner.py:92-97,218-240`). | Current physical compiler/coordinator/operator bindings; PR #144 parity corpus; PR #166’s stream ledger, schema drift, and transactional scaffolding. | **M-L**, about 500-1,000 production/test LOC depending on strategy breadth. |
| 2 | **Fix connection-binding descriptors.** Unlocks all platform-created S3/GCS source and target bindings, independently of performance work. Strip provenance fields before engine validation or add a separate provenance envelope; do not widen engine execution descriptors with platform-only keys. | Resolver adds forbidden fields (`decoy-platform@origin/main:api/jobs/binding_resolve.py:128-151`); worker validates later (`api/jobs/v2_submission.py:123-129`); engine models forbid extras (`src/decoy_engine/config/_sources.py:71-125`; `_targets.py:38-92`). | Existing evidence manifest already consumes binding provenance; retain it outside the executable descriptor. | **S**, roughly 50-150 LOC plus regression tests. |
| 3 | **Compiled OOC payload kernels.** Unlocks Rust masking for 5M-100M+ compatible FK trees while preserving DuckDB scan/join behavior. Start with hash/categorical/bucket perturb using existing companion APIs; redact/truncate/passthrough are already vectorized Arrow. | OOC admits the strategies but executes its own Python/Arrow mask dispatcher (`src/decoy_engine/execution/out_of_core/_compat.py:28-47`; `_mask.py:97-173`). | Current unified operators and companion loaders; PR #145’s OOC physical-plan dispatch and parity tests. | **L**, around 1,000-2,000 LOC including batch evidence, schema/error handling, and parity suites. |
| 4 | **Correct platform cycle routing before further FK optimization.** Unlocks cross-table-cycle jobs that engine full-frame can handle, and prevents the current sequential runtime failure. | Platform sequential predicate ignores cycles (`decoy-platform@origin/main:api/jobs/v2_sequential.py:109-121`); engine sequential raises on them (`src/decoy_engine/execution/_sequential.py:640-647`); engine auto-router already has the correct full-frame/reject policy (`_pipeline_routing.py:324-330,475-499`). | Reuse engine graph-cycle classification or expose it as a public route query; adaptive full-frame route already exists. | **S-M**, about 100-300 LOC plus route tests. |
| 5 | **Compiled sequential per-table masking.** Unlocks Rust for diamonds, self-FKs, OOC-incompatible strategies, CSV/fixed-width FK jobs, and OOC runtime fallbacks. | Sequential constructs `PandasExecutionAdapter` directly (`src/decoy_engine/execution/_pipeline_route_exec.py:142-153`; platform duplicate at `decoy-platform@origin/main:api/jobs/v2_sequential.py:191-199`). | Current physical operator bindings, PR #145’s route-dispatch structure, existing sequential transactional sink and FK-map ownership. | **XL**, about 2,000-4,000 LOC; FK child resolution and diagnostics make this materially larger than flat chunking. |
| 6 | **Compiled generation selection.** Unlocks a Rust stage for large deterministic/pool-based generation and mixed jobs. It cannot make arbitrary Faker providers fully Rust without reimplementing them. | Production generation loops in Python (`src/decoy_engine/generation/synthesize.py:139-198,381-456`); pooled path already separates Python pool build from NumPy selection (`_faker_pool.py:317-410`). | Current shadow Faker operator and pool identity/cache work; PR #144’s chunked Faker parity cases. | **M** for compiled pool/index selection; **XL** for non-pooled/provider-native generation. |
| 7 | **Align input-format and transform ownership.** Reject fixed-width from Phase 1 until a streaming reader exists; use the fixed-width reader in CLI; either move transforms into engine `run_pipeline` or explicitly exclude them from direct engine/CLI claims. | Phase 1 has no format gate but stream reader rejects fixed-width (`decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:137-231`; `api/jobs/streams.py:160-191`). CLI reads every non-Parquet suffix as CSV (`src/decoy/cli/run.py:1191-1224`). Platform alone applies table transforms (`api/jobs/v2_runner.py:162-215`). | Platform fixed-width reader call, engine `execution._transforms`, existing eligibility reason machinery. | **S** for fail-closed gates; **M** for true fixed-width streaming and shared transform execution. |
| 8 | **Widen Phase 1 only after compiled batch execution exists.** Unlocks categorical, bucket perturb, group key, date shift, FPE, and text strategies for platform large-table streaming. | Phase 1 hardcodes only hash/redact/truncate/passthrough (`decoy-platform@origin/main:api/jobs/_phase1_eligibility.py:67-74,181-231`) even though the engine chunk entry supports a wider conditional set (`src/decoy_engine/execution/_chunked.py:181-247`; `_chunked_fk.py:76-109`). | Engine’s config-aware chunk compatibility and physical operator admission; avoid copying another static allowlist. | **M**, roughly 300-700 LOC per widened batch including parity tests. |
| 9 | **Native/streaming post-validation integration.** Unlocks enforced post-validation without forcing or rejecting large jobs. | Platform says only full-frame runs the suite and rejects enforced streaming/sequential/OOC (`decoy-platform@origin/main:api/jobs/post_validation_policy.py:11-18,87-103`). | Existing post-validation scanners plus staged/streaming result readers; likely needs incremental or second-pass scans. | **L-XL**, depending on whether checks can be made incremental. |
| 10 | **Streaming subset materialization or companion relational subset.** Unlocks truly cardinality-bounded 100M+ subsetting. Current Polars subset is Rust-backed internally but not the companion and still collects key frames plus one full table. | `scan_parquet(...).collect()` for keys and materialization (`src/decoy_engine/subset/_keys.py:42-57`; `_materialize.py:54-72`). | Existing Polars closure, survivor-row-index contract, and monotone cycle-safe algorithm. | **L** for Polars `sink_parquet`-style materialization; **XL** for a new companion-native closure engine. |


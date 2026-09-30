# Stage 2c: FK/relationship shapes, the cycle bug, a real Postgres claim, and the 430s reconciliation

Status: record (input to `docs/plans/2026-09-30-rust-coverage-evidence-audit.md`)

Runs R043-R068 in `docs/records/audit-2026-09-30/runs.jsonl`, continuing from stage 2b's
R042. Environment: same dedicated venv as stage 2a/2b
(`docs/records/audit-2026-09-30/environment.json`) -- engine editable from this worktree
(companion present, ok, `decoy-native-abi-2`), platform installed from the detached
worktree at `origin/main` `0701a954`, decoy-cli installed but not used this stage.
`psycopg2-binary` was added to the throwaway venv (a dev-only DB driver, same category as
stage 2b's `flask`/`flask_cors` addition; no production dependency file touched).

A throwaway Postgres database, `decoy_audit_20260930`, was created in the `v3-0-fix-pg`
container (127.0.0.1:55438) and migrated to head with the platform's own Alembic chain
(20 revisions, `pg_baseline` through `s12a_lease_recovery_contracts`). No other database
in that container was touched. **The database was dropped at the end of this stage** (see
the closing note).

Scripts added under `scripts/audit/`: `stage2c_fk_shapes.py` (engine-direct FK shapes),
`stage2c_platform_fk.py` (platform worker path + `admission_fk` pricing on the same
shapes), `stage2c_real_claim.py` (the real Postgres-backed claim). One cell per fresh
subprocess; VmHWM read from `/proc/self/status` at the end of each engine-direct process.
The platform-path and claim cells are correctness/routing witnesses, not size-tier
memory probes (same scope choice stage 2b made for its platform-wrapper cells), so their
`peak_memory_vmhwm_kb` reflects the whole platform-worker process, not an isolated masking
step.

## Cells table

| Cell | Shape | Entry point | Route / outcome | Wall (s) |
|---|---|---|---|---:|
| R043 | FK tree, 10k/50k, default | engine-direct | full_frame, byte_estimate_full_frame_fits | 6.09 |
| R044 | FK tree, forced sequential | engine-direct | sequential, pure_mask_fk | 5.93 |
| R045 | FK tree (group_key payload), forced out_of_core | engine-direct | rejected: `out_of_core_cross_row_strategy_unsupported` | 0.41 |
| R046 | FK tree, OOC-compatible payload, forced out_of_core | engine-direct | out_of_core, override_out_of_core | 5.67 |
| R047 | FK diamond, 2k/8k, default | engine-direct | full_frame, byte_estimate_full_frame_fits | 1.29 |
| R048 | FK diamond, forced sequential | engine-direct | sequential, pure_mask_fk | 1.27 |
| R049 | FK diamond, forced out_of_core | engine-direct | rejected: `out_of_core_multi_parent_child_unsupported` | 0.16 |
| R050 | Self-referential FK, 4k, default | engine-direct | sequential, pure_mask_fk (not a cycle) | 0.47 |
| R051 | Self-referential FK, forced out_of_core | engine-direct | rejected: `out_of_core_self_referential_fk_unsupported` | 0.06 |
| R052 | Cross-table FK cycle, 3k, default | engine-direct | full_frame, cross_table_cycle | 0.70 |
| R053 | Cross-table FK cycle, forced sequential | engine-direct | rejected: ConfigError, "cross-table cycle" | 0.09 |
| R054 | Cross-table FK cycle, forced out_of_core | engine-direct | rejected: `out_of_core_relationship_cycle_unsupported` | 0.08 |
| R055 | FK tree + validators, 3k/9k, default | engine-direct | full_frame, validators_present excludes sequential/OOC | 1.26 |
| R056 | FK + generate-kind parent (mixed), 3k/9k, default | engine-direct | full_frame, generate_plus_mask | 0.91 |
| R057 | FK tree (OOC-compat), row-count route-equivalent | engine-direct | out_of_core, out_of_core_large_fk (threshold forced to 10) | 5.60 |
| R058 | FK tree (group_key), row-count route-equivalent | engine-direct | sequential, pure_mask_fk (row-count branch) | 5.74 |
| R059 | FK + validators, row-count reject route-equivalent | engine-direct | rejected: `fk_full_frame_oom_risk_rejected` (threshold forced to 10) | 0.08 |
| R060 | FK tree, 2k/8k | platform worker (`run_v2_pipeline_job`) | success, `execution_mode=sequential_fk` both tables | 1.60 |
| R061 | FK diamond, 2k/8k | platform worker | success, `sequential_fk` all three tables | 1.75 |
| R062 | Self-referential FK, 4k | platform worker | success, `sequential_fk` | 0.90 |
| R063 | **Cross-table FK cycle, 3k (BUG WITNESS)** | platform worker | **FAILED**: `ExecutionError(relationship_cycle)`, job.status=failed | 0.42 |
| R064 | FK tree + validators, 3k/9k | platform worker | success, `sequential_fk` both tables | 1.70 |
| R065 | FK + generate parent (mixed), 3k/9k | platform worker | success, `execution_mode=full_frame` (`legacy_full_frame`) | 1.35 |
| R066 | FK tree (OOC-compat), 2k/8k, below threshold | platform worker | success, `sequential_fk` both tables | 1.42 |
| R067 | FK tree (OOC-compat), thresholds forced to 10 together | platform worker | success, `execution_mode=out_of_core_fk` both tables | 1.49 |
| R068 | Real Postgres claim + worker, 2000-row single table | `queue_worker._claim_next_job` then `run_v2_pipeline_job` | claimed, `phase1_streaming_tables=["people"]`, worker ran `execution_mode=chunked_stream` | claim 0.04 / worker 0.29 |

Every engine-direct rejection above is a real raised `ConfigError`/`ExecutionError`, not an
inferred code path; every platform-path cell created and read back a real `Job` row (and,
for R060-R067/R068, real `JobNodeRun` rows) from the throwaway Postgres database. `admission_fk`
pricing (`is_fk_bounded_route_candidate`, `out_of_core_admission_eligible`) was called directly
(pure, config-only) alongside every platform-path cell; results are in `runs.jsonl`'s
`admission_fk` field per cell and folded into the ledger's section 15a rows.

## A. FK shapes, both entry points

**Route/backend per shape, one line each** (engine-direct default routing at real-small size;
platform worker on the identical real-small config):

- **FK tree** (parent + child, hash keys, native-mix payload with one OOC-compatible strategy
  [`bucket_perturb`] and one OOC-incompatible strategy [`group_key`]): engine-direct default
  `full_frame` (byte estimate fits); forced `sequential` succeeds; forced `out_of_core` declines
  on the `group_key` column specifically (`out_of_core_cross_row_strategy_unsupported`). An
  OOC-compatible-payload variant (drop `group_key`) succeeds under forced `out_of_core`
  (R046) and under row-count route-equivalent auto-routing (R057). Platform worker: real
  Postgres job runs `sequential_fk` on both tables (R060); the row-count route-equivalent
  variant with `admission_fk.OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS` and
  `v2_out_of_core._OUT_OF_CORE_THRESHOLD_ROWS` **both overridden to the same value (10)**
  runs real `out_of_core_fk` on both tables (R067) -- admission and runtime **agree**, confirmed
  by a real run, not just by reading that they default to the same upstream constant.
- **FK diamond** (child FK column shared by two parent relationships, the ambiguous
  same-child-key shape): engine-direct default `full_frame`; forced `sequential` succeeds on
  all three tables; forced `out_of_core` declines (`out_of_core_multi_parent_child_unsupported`).
  Platform worker: real job succeeds `sequential_fk` on all three tables (R061) --
  `admission_fk.out_of_core_admission_eligible` independently returns `(False, "multi_parent_child")`
  for the same config, agreeing with the engine's own OOC decline.
- **Self-referential FK** (`employees.manager_id -> employees.id`): engine-direct default
  routes `sequential` (`pure_mask_fk`, **not** flagged a cycle, per B080); forced `out_of_core`
  declines (`out_of_core_self_referential_fk_unsupported` -- out-of-core's own, stricter edge
  check, contrast with B080). Platform worker: real job succeeds `sequential_fk` (R062), while
  `admission_fk.is_fk_bounded_route_candidate` returns `(False, "self_referential_fk_edge")`
  for the identical config -- confirming with a real run the ledger's B216 finding that this
  pricing exclusion is a conservative choice, not a real runtime disqualifier: the same
  self-FK job that is priced at full (non-discounted) rate still runs the bounded sequential
  route successfully.
- **Cross-table FK cycle** (`a` -> `b` -> `a`): engine-direct default `full_frame`
  (`cross_table_cycle`, succeeds); forced `sequential` and forced `out_of_core` both raise
  `ConfigError`/decline cleanly. Platform worker: **fails** -- see scope B below.
- **FK + validators** (`fk_intact`, `no_orphan_children` on the FK tree): engine-direct
  default `full_frame` (validators disqualify sequential/OOC, B075); the row-count
  route-equivalent variant (byte-estimate off, `full_frame_reject_rows` forced to 10) raises
  the real `fk_full_frame_oom_risk_rejected` `ExecutionError`, because validators leave no
  bounded route available at this forced size. Platform worker: real job succeeds `sequential_fk`
  on both tables (R064) -- the platform's own sequential branch, unlike the engine's own
  `run_pipeline` router, does **not** exclude validators from the bounded route (matches the
  Codex independent audit's shape-9 table: "Platform: validators and vault still run on its
  SEQ-PD route").
- **FK with a generate table (mixed mask+generate)**: a generate-kind parent whose generated
  key column feeds a mask-kind child via the relationship. Engine-direct default `full_frame`
  (`generate_plus_mask`). Platform worker: real job succeeds, `execution_mode=full_frame`
  (`legacy_full_frame` per its `JobNodeRun` export) for the mask table, a separate `generate`
  node for the parent (R065). A pure "generate-only" FK variant (every participant table
  generate-kind, no mask table at all) was **not built this pass**: `has_mask_table` would be
  `False` end to end, and neither the sequential nor OOC/full-frame relationship router has a
  meaningful decision to make without a mask table, so it reduces to plain generation with no
  routing question the plan's dimensions ask about. Recorded as a reasoned scope cut, not an
  oversight.

**Large-tier route-equivalent, both layers, agreeing.** R067 is the cell the plan named
explicitly: `admission_fk.OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS` (claim-time pricing) and
`v2_out_of_core._OUT_OF_CORE_THRESHOLD_ROWS` (runtime dispatch) were overridden together to
10 rows, on an 8,000-row real child table. `admission_fk.out_of_core_admission_eligible`
returned `(True, "out_of_core_admission_eligible")` and the real platform worker run
independently reached `node_execution_mode="out_of_core_fk"` for both tables. Both layers
read `decoy_engine.execution.OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` (5,000,000) by default in
this codebase today (confirmed by the unmodified values printed in `patched_constants_before_override`
in R067's record) -- they agree by construction, and this run confirms they still agree
under an override, not just that they share a source constant. This is route-equivalent, not
capacity-proven: the 8,000-row fixture does not exercise real out-of-core spill, cardinality,
or disk admission at 5M+ scale.

Engine-side large-tier route-equivalent cells (R057-R059) used the same technique on
`out_of_core_threshold_rows` and `full_frame_reject_rows` (`run_pipeline` kwargs), all three
agreeing with their code-read predictions in the branch-witness ledger (B097, B098, B100).

**Not done this pass, with reason**: OOC's `batch_join` vs `reorder` decision (`_route_policy.py`)
was not forced to the `reorder` branch -- every OOC-run FK shape's parent-key count stayed
far under the default 2,000,000-row `REORDER_PARENT_KEY_THRESHOLD`, so `use_reorder=False`
(`_batch_join`) by construction; `out_of_core_reorder_threshold_rows` was not overridden down
to force the alternate branch this pass (recorded in the ledger at B128). The FK-with-a-vault-writer
and FK-with-fidelity_report/post_validation combinations named in the plan's dimension table
were not separately built this pass (validators alone, B075/B214's sibling gate, was used as
the representative "gate excludes bounded route" witness); the engine-direct code already
shows all four gates (validators, vault, fidelity, post_validation) share the same
`_sequential_eligible` decline shape (section 4a, B075-B078).

## B. The cross-table-cycle bug

**Witnessed with a real run, both sides.** The identical cross-table-cycle FK config (table
`a` has an FK to `b`, `b` has an FK to `a`, 3,000 rows each) was run through both entry
points:

- Engine-direct, default `auto` routing (R052): `run_pipeline`'s own router detects the cycle
  (`_has_cross_table_fk_cycle`) and stays on `full_frame` -- **succeeds**, `execution_mode=full_frame`,
  `route_reason=cross_table_cycle`.
- Platform worker, a real Postgres-backed `Job` row (R063): `v2_sequential._should_use_sequential_relationship_path`
  (`v2_sequential.py:109-121`) checks only "has relationships" and "no generate columns" --
  no cycle check exists in this function. It returns `True`, so `run_v2_pipeline_job` takes
  the sequential branch instead of full-frame. `run_sequential_relationship_job` calls the
  engine's `run_sequential`, whose `table_topo_order` (`_sequential.py:640-647`) raises
  `ExecutionError(code="relationship_cycle")`. The exception is not caught by any
  cycle-aware fallback; it propagates out of `run_v2_pipeline_job`. The real Job row ends
  **`status=failed`**, `row_count=None`, one `JobNodeRun` row (`kind=mask`, `status=error`).

The same job succeeds through one entry point and fails through the other. `admission_fk.is_fk_bounded_route_candidate`
returns `(True, "pure_mask_fk")` for this same cyclic config (R063's `admission_fk` field) --
the admission-pricing layer has no cycle check either, so the job is priced as an ordinary
bounded-discount FK candidate right up until the runtime failure. This confirms the Codex
independent audit's ranked gap #4 ("Correct platform cycle routing before further FK
optimization") exactly, and extends it from a code-reading claim to an executed, reproduced
failure against a real database-backed job.

## C. A real Postgres-backed claim (entry point 3)

A single-table config (`id`: passthrough, `email`: hash, `amount`: redact -- all four
`_PHASE1_ALLOWED_STRATEGIES`-compatible except passthrough/hash/redact are three of the
four allowlisted strategies), 2,000 real rows, CSV source, was submitted as a real `Job`
row (`status=pending`) in the throwaway Postgres database via the platform's own ORM model.
`settings.streaming_execution_enabled=True` and `settings.streaming_min_input_mb=0.0` were
set so the small fixture qualifies. The REAL claim function, `api.jobs.queue_worker._claim_next_job`
(flag OFF -- `adaptive_scheduler_lease_authority_enabled` defaults `False`, so this is the
legacy scan, not `scheduler_claim_loop.claim_one_flag_on`), was called directly against
`SessionLocal` bound to the throwaway database (via `DATABASE_URL`). It claimed the job
(`(job_id, "mask", 384.0)`), flipped it to `running`, and persisted
`Job.phase1_streaming_tables = '["people"]'`, decoded back as `StreamingPlan(tables=("people",))`
-- read from the row after the claim returned, not from the knobs set beforehand.

The real worker path, `api.jobs.v2_runner.run_v2_pipeline_job` (re-exported from
`v2_orchestrator`, the same function Celery calls), was then run on that SAME persisted
job. It succeeded (`status=success`, `row_count=2000`); the real `JobNodeRun` row's
`exports` field reads `{"execution_mode": "chunked_stream", "profile_scope": "first_chunk",
"streamed_batches": 1}` -- confirming, end to end through the real claim and worker
machinery (not the stage 2b functions-direct fallback), that the per-chunk engine function
is `decoy_engine.execution.run_mask_pipeline_chunked` on the `PandasExecutionAdapter`, exactly
as the ledger's section 15e and the Codex independent audit's "Specific answers" #1 already
established by reading the code. This closes stage 2b's explicitly deferred item ("A real
Postgres-backed claim ... was used instead [functions-direct]... nothing to drop at the
end").

No service this host lacks was needed for this item: Postgres was reachable (the same
`v3-0-fix-pg` container), the flag-off legacy claim path needs no cgroup supervisor or
adaptive-scheduler infrastructure. The flag-ON path (`scheduler_claim_loop.claim_one_flag_on`,
entry point 4's adaptive scheduler) still needs the cgroup job supervisor socket and cgroup
v2 delegation this devbox lacks (confirmed absent in `preflight.json`, unchanged since stage
2a/2b) -- not attempted, per the plan's own instruction, and distinct from this item, which
only required the flag-off path.

## D. Reconciling "100M masked in ~430s, ~450MB, ~13.5x hash speedup"

**Source, exactly.** The number is engine PR #129 (merge commit `8d833c30`, "Native
throughput: Phase 1 (parallel hash kernel) + Phase 2 (compiled Faker index kernel)"),
specifically the Task 1.6 gate-outcome note recorded in
`docs/plans/2026-09-09-execution-consolidation-and-native-throughput.md` at commit `4e00bbac`
(2026-09-10): *"GCP n2-standard-8 thread sweep, frozen 100M W2, 3 reps/thread. 8-thread 100M
MEDIAN wall = 429.9s <= 600s target ... peak RSS 447MB ... Hash kernel: 1280s baseline ->
299.7s single-thread (4.3x) -> 94.8s at 8t (~13.5x total)."* The raw per-rep JSON lines
backing that median are in (gitignored, not committed)
`decoy-platform/docs/product/release-1-validation-runs/2026-09-10-tb6-50m/engine-bench-feat-native-throughput-consolidation-20260910T140112Z-tb6-50m/remote-results/native-threadsweep.log`
(read directly this pass): the 8-thread, 100M-row record shows `wall_median_s: 429.85`,
`peak_rss_max_mb: 446.9`, and per-rep fields `hash_ms` 87.4 to 103.9s across the 3 reps,
`redact_ms: ~128-146s`, `truncate_ms: ~159-178s`, `execution_mode: "native_streaming"`,
`compiled_kernel_executed: true`. `RECENTLY-SHIPPED.md`'s prose and the platform's
`docs/product/engine-efficiency-outcomes-2026-09-18.md` ("100M rows mask in ~430 seconds flat
... 13.5x faster ... peak at ~450 MB RSS") both consolidate this same run; no separate, later
benchmark reproduces these exact numbers. This raw log
(`native-threadsweep.log`) is gitignored (`decoy-platform/.gitignore:16`, a blanket `*.log`
rule) and is not itself committed; only the summarized gate-outcome note in the plan doc is
tracked, so a reader without local access to that log file can verify the numbers above only
against this record, not by re-reading the source file from a fresh clone.

**Which route it measured: neither the unified lane nor any currently-reachable production
path.** The raw log's `execution_mode: "native_streaming"` and the benchmark worker script
committed in the same PR, `scripts/native-baseline/bench_worker_native.py`, make this
unambiguous by reading the code directly: the worker imports
`decoy_engine.execution.native.run_native_or_oracle_chunked` and calls it directly on a
pre-built Parquet file, batch by batch -- **bypassing `run_pipeline`, the platform, and the
CLI entirely**. This is the SAME compiled chunked-native dispatch (`execution/native/_dispatch.py`,
ledger section 9, B158-B169) that stage 2a's finding 2 proved, by grepping this engine tree,
`decoy-platform`, and `decoy-cli`, has **zero production callers today** -- the only caller
anywhere outside `execution/native/`, `execution/physical/` (where it is defined), and test
files is this bespoke benchmark harness. It is not the unified full-frame Rust lane stage 2a's
R001-R022 exercised (`_unified_slice.py`), not the pandas-only `run_mask_pipeline_chunked` the
live platform Phase-1 streaming path and CLI `--chunked` actually call today (ledger section
15e; confirmed again this pass by R068's real claim+worker run), and not the standalone
"native route" lane engine PR #166 later deleted (`_native_route_exec.py` / `native_route_enabled`)
-- that was a separate, also-never-production-wired lane, not this one. In short: the 430s
figure describes a kernel/lane microbenchmark of a dispatch layer that has never been reachable
by a real customer job, from the day it was measured (2026-09-10) through today
(2026-09-30) -- not a regression, a capability that was never wired to production in the
first place.

**The "13.5x" is a hash-kernel figure, not a whole-job figure.** The gate-outcome note is
explicit: "Hash kernel: 1280s baseline -> 299.7s single-thread (Tasks 1.2+1.5 alone, 4.3x) ->
94.8s at 8t (~13.5x total)" is the isolated hash-operator speedup, and both ends are native:
the 1280s baseline is the Phase 0 native single-thread hash time (`docs/plans/native-throughput-phase0-baseline.md`,
~234k rows/s/col over 3 hash columns at 100M rows, already compiled, already beating the
pandas oracle 2.9x, just single-threaded and not yet Rayon-parallel), not a per-row Python
reference. The 13.5x combines kernel work (1280s to 299.7s at 1 thread, 4.3x) and threading
(299.7s to 94.8s at 8 Rayon threads, about 3.2x): the 94.8s kernel is not the same code as the
1280s baseline. Both ends are native, not native-vs-Python. It is measured on
the same W2 workload's hash columns alone. `RECENTLY-SHIPPED.md` and the outcomes report both
place "13.5x" in the same sentence as "100M rows masked in ... 429.9s", which is accurate as
written (both numbers are about the same benchmark) but reads easily as "the whole job got
13.5x faster," which it did not: at 8 threads, the same raw record shows hash at 87.4 to
103.9s across the 3 reps but redact+truncate together at ~317-324s -- more than 3x the hash
time -- because both operators still ran the OLD per-row Python loop in this benchmark. The
gate-outcome note's own "BOTTLENECK SHIFT" line says so directly: "redact (~142s) + truncate
(~173s) now dominate and run single-threaded."

**The wall-time number is also now stale, separately from the routing question.** The
vectorized Arrow/Python redact/truncate fast path landed the next day (2026-09-11, `d4885f7a`).
A later refresh of the SAME (still production-unreachable) `native_streaming` lane, committed
2026-09-21 (`0731e58b`, `results_native_100m.json`, `native_threads=1` this time), shows
`redact_ms: 3277` (~3.3s) and `truncate_ms: 5108` (~5.1s) at 100M rows -- roughly 44x and 36x
faster than the 2026-09-10 run's per-row loop -- confirming the memory note's independent
finding that the quoted 142s/173s figures are "the GIL-held Python fallback, not the ~2s
pyarrow fast path." Re-running the ORIGINAL 8-thread configuration on today's code would
almost certainly land well under 430s, dominated by hash instead of redact/truncate -- but
that rerun was NOT performed this pass (read-only per the task's instruction; "do not rerun
at 100M").

**Agreement/disagreement with `codex-independent-audit.md`**: Codex's own answer to
"minimal changes to put the large routes on Rust kernels" (ranked gap #1) already names
`run_mask_pipeline_chunked`'s pandas-only implementation as the production gap and separately
credits "PR #144 ... proves chunked shadow parity" and "Deleted PR #166's `_native_route_exec.py`
... shadow-only" as adjacent, non-production evidence -- consistent with this pass's finding
that PR #129's 430s number sits in the same "measured but never wired to production" category,
one PR earlier than the shadow work Codex cites. No disagreement: this pass adds the specific
commit, the raw numbers, and the exact bypassed-entry-point mechanism (direct call to
`run_native_or_oracle_chunked`) that a code-only audit would need to trace through a
benchmark script rather than production code to find.

## Disagreements with codex-independent-audit.md

**None found in scope A/B/C.** Every stage-2c FK-shape run confirmed Codex's shape tables 5-10
exactly (FK tree/diamond/self-ref/cycle/validators/generate-table routes, both entry points),
and the cycle-bug run (scope B) confirmed Codex's ranked gap #4 with a real failure rather than
a code trace. Scope D (above) extends rather than contradicts Codex's own gap #1 framing.

## Ledger updates

The branch-witness ledger's Witness column was filled for: B073-B075, B080-B082, B084-B086,
B088, B090, B092, B094-B101 (section 4, FK routing), B111, B113, B115-B117, B122, B128
(section 6, out-of-core compatibility), B212-B214, B216-B217, B228-B229, B231, B234, B236,
B241, B245, B264 (section 15a/15b/15d, platform admission/runtime/claim). A new paragraph
under section 4b records the cycle-bug witness in full (R052 vs. R063). Ids left unfilled
this pass, with reasons, are noted inline at each row (B094-B096, B099, B101 partial;
B128's reorder branch not forced).

## Not done this stage, with reason

- **OOC `batch_join` vs `reorder`** (`out_of_core_reorder_threshold_rows` forced down): not
  run. Every OOC cell's parent-key count stayed under the default 2,000,000-row threshold, so
  `_batch_join` ran by construction; forcing the alternate branch needs a dedicated fixture
  this stage did not build given the time budget.
- **Generate-only FK shape** (every participant table generate-kind, no mask table): not
  built. Reduces to plain multi-table generation with no FK-routing decision to witness (see
  scope A above for the reasoning).
- **Entry point 4** (adaptive-scheduler `DispatchPlan` worker, flag ON): not attempted --
  this devbox still lacks the cgroup job supervisor socket and cgroup v2 delegation
  (`preflight.json`, unchanged since stage 2a/2b). The flag-OFF claim path used for scope C
  does not need this infrastructure.
- **100M rerun of the native_streaming lane on current code**: explicitly out of scope per
  the task's read-only instruction for scope D.

## Closing: throwaway database dropped

At the end of this stage the throwaway database was dropped, per the task's hard rule
("create ONE throwaway database for the audit ... and DROP it at the end"):

```
DROP DATABASE decoy_audit_20260930;
```

No other database in the `v3-0-fix-pg` container was read from, written to, or touched at
any point this stage.

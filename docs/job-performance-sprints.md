# Job Performance Sprint Plan

**Status:** Drafted 2026-07-01 as the cross-cutting performance plan that sits
beside, not inside, the relationship memory-scaling work. Relationship memory
scaling remains gated on engine PR #22; this plan identifies work that can start
now without rewriting FK internals, and work that should wait until that branch
settles. Being executed on branch `feat/engine-efficiencies` (based on `main`).
An earlier checkpoint prototyped the P1/P4 `run_pipeline` wiring alongside an
unrelated stale copy of the FK stack; that copy is discarded here and the P1/P4
wiring is rebuilt cleanly on top of `main`, which already carries the substrate
adapter selection and chunked execution these sprints depend on. The FK-stack
files (`_sequential.py`, `_transactional_sink.py`, `out_of_core/`) stay on their
own branches under the ordered merge plan and are NOT reintroduced here.

**Audience:** Engine tech lead / PO.

**Scope:** Runtime performance for `run_pipeline` jobs: substrate routing,
chunked execution, out-of-core and sequential relationship routes, Python hot
paths, conversion overhead, benchmarks, and planner-level execution-mode
selection.

**Relationship dependency:** the relationship memory-scaling and out-of-core
sprint plans (`relationships-memory-scaling.md` and
`relationships-out-of-core-sprints.md`) live with the FK stack on its own
branches, not on `main`; they are referenced here by name rather than linked so
this plan builds standalone. This plan must not change FK semantics,
orphan-policy behavior, or out-of-core relationship artifacts until PR #22 lands
or is explicitly superseded.

---

## 1. Executive Summary

The engine already has the right pieces for faster jobs: Arrow boundaries,
Polars substrate support, chunked execution, sequential FK execution,
out-of-core FK work, benchmark fixtures, and native libraries such as PyArrow,
Polars, DuckDB, NumPy, and numexpr.

The performance gap is mostly architecture and planner selection, not absence
of tools:

- Routing `run_pipeline` mask-kind work through the selected execution adapter
  (P1) lets scalar mask jobs honor `DECOY_SUBSTRATE=polars` at the public
  entrypoint. FK and composite work still falls back where Polars cannot run it
  natively.
- Work-node dispatch is serial.
- FK-heavy jobs still depend on relationship memory-scaling work for the large
  wins.
- Some strategy handlers and FK paths materialize Python lists, dicts, tuples,
  and object strings at row scale.
- Chunked and out-of-core routes exist, but are not yet planner-selected for
  ordinary jobs.

Python is a real limiter when it is in the data plane. The mitigation is not a
rewrite in another language. Python should remain orchestration; row-scale work
should move into Arrow kernels, Polars expressions, DuckDB joins, NumPy arrays,
numexpr, chunked streaming, or process-level partitioning where pure Python is
unavoidable.

---

## 2. Python Limitations

Python limits engine speed in these specific places:

1. **GIL-bound loops.** Pure-Python row-level work does not scale with threads.
   FPE, text redaction, nested masking, remap paths, and some provider logic can
   land here.
2. **Object overhead.** Python strings, tuples, lists, dicts, and pandas object
   columns are expensive at millions of rows. FK maps are the clearest example.
3. **Serial orchestration.** The pandas adapter dispatches one work node at a
   time even when nodes have no dependency edge between them.
4. **pandas conversion.** Arrow tables are converted to pandas and back on the
   pandas path. That creates copy cost and can widen/null-coerce types.
5. **FK parent maps.** Python tuple-to-tuple maps are correct but memory-heavy;
   large relationship jobs need native joins or staged narrow key relations.
6. **Faker/provider state.** Shared Faker/RNG state blocks safe per-column
   parallelism until pool construction and sampling are isolated by identity.
7. **Python materialization in out-of-core paths.** Any `.to_pylist()` or
   list-of-keys sized by total table cardinality recreates the memory wall the
   out-of-core route is meant to avoid.

### Mitigations

- Keep simple strategies in Arrow/Polars kernels: `hash`, `redact`,
  `truncate`, `passthrough`, `bucketize`, and value-keyed date transforms.
- Replace Python FK maps with narrow Arrow/Polars/DuckDB joins after the
  relationship memory-scaling contracts settle.
- Use chunked execution for large value-keyed jobs so Python only sees bounded
  batches.
- Use process-level partitioning for unavoidable pure-Python work; derive seeds
  per `(table, column, partition)` so determinism is not tied to scheduling.
- Remove shared mutable Faker state from pool building; make provider pools
  isolated, deterministic, and safe to build in parallel.
- Avoid `to_pylist()` and pandas object columns in hot paths. Prefer Arrow string
  arrays, Polars string columns, and Parquet inputs.
- Treat benchmarks and parity tests as acceptance gates before switching any
  default route.

---

## 3. What Can Start Now

These sprints intentionally avoid FK semantic changes and should not conflict
with relationship memory scaling.

### Sprint P0: Performance Baseline and Gates

**Target complete:** before further default-routing changes; recommended first
follow-up performance sprint.

**Deliverables**

- Add or consolidate benchmark gates for:
  - scalar no-FK masking;
  - FK parent/child masking;
  - faker-heavy wide tables;
  - FPE-heavy tables;
  - text-redaction-heavy tables;
  - chunked large single-table masking;
  - sequential relationship route;
  - out-of-core relationship route, using the current opt-in path.
- Record p50/p95 elapsed, peak RSS, boundary conversion time, and executed
  substrate.
- Add an explicit correctness gate beside every timing result. No performance
  number counts unless parity passes.

**Acceptance**

- Benchmarks can run narrowly in CI or locally without the full test suite.
- Baseline results identify whether time is in conversion, strategy execution,
  FK resolution, profile/compile, or output writing.
- No production behavior changes.

### Sprint P1: Route `run_pipeline` Through Adapter Selection

**Status:** To build on `feat/engine-efficiencies` (prototyped on an earlier,
now-discarded checkpoint). Then measurement and broader gate coverage.

**Deliverables**

- Replace direct `PandasExecutionAdapter()` construction in
  `run_pipeline` with `select_execution_adapter()`.
- Keep mixed generate+mask semantics unchanged.
- Ensure selected-adapter quality metrics are visible where available.

**Acceptance**

- Golden `run_pipeline` tests pass on pandas.
- Substrate/parity tests pass on Polars.
- Scalar no-FK mask jobs can actually take the Polars-native route.
- FK/composite jobs still fall back exactly as before when Polars cannot run
  them natively.

### Sprint P2: Execution-Mode Planner Shell

**Target complete:** after P1; before changing any relationship internals.

**Deliverables**

- Add a planner decision surface that classifies a job into one of:
  - `polars_native`;
  - `chunked`;
  - `sequential_relationship`;
  - `out_of_core_relationship`;
  - `pandas_fallback`.
- First implementation may only observe and report the selected mode while
  preserving current execution behavior.
- Record rejection reasons for non-selected faster modes.

**Acceptance**

- Planner output is deterministic and explainable.
- The default route remains unchanged unless an explicit feature flag enables
  planner routing.
- Rejection reasons are surfaced in tests for common cases: FK edge,
  non-chunk-safe strategy, composite bundle, generated table, and fallback to
  pandas.

### Sprint P3: Auto-Select Chunked Execution for Eligible Single-Table Jobs

**Target complete:** after P2; can land before PR #22 because it uses the
existing chunked compatibility gate.

**Deliverables**

- Route large single-table mask jobs through `run_mask_pipeline_chunked` when
  compatibility checks pass.
- Keep generation out of scope.
- Respect the existing admitted strategy set:
  `hash`, `fpe`, `redact`, `truncate`, `text_redact`, `date_shift`,
  `bucketize`, `passthrough`, plus conditionally admitted deterministic
  `faker` and `categorical`.
- Add a job-level chunk-size knob.

**Acceptance**

- Concatenated chunked output is byte-identical, or documented value-identical
  across substrates where existing parity docs allow schema-level differences.
- Peak RSS drops on large eligible jobs.
- Vault collection remains correct when enabled.
- Non-eligible jobs fail closed back to the full-frame route with a clear
  planner reason.

### Sprint P4: Expose Existing Performance Knobs

**Status:** To build on `feat/engine-efficiencies` (prototyped on an earlier,
now-discarded checkpoint). Broader runtime controls, metadata, and debug
surfacing.

**Target complete:** can run in parallel with P1-P3.

**Deliverables**

- Document and expose job/runtime controls for:
  - `run_pipeline`: `substrate`;
  - `run_pipeline`: `fpe_chunk_count`;
  - `run_pipeline`: Polars `max_workers`;
  - `run_pipeline`: Polars `fallback_to_pandas`;
  - `DECOY_SUBSTRATE`;
  - Polars `enable_out_of_core`;
  - chunk size;
  - pool cache size;
  - post-validation and fidelity-report toggles.
- Ensure every knob appears in job metadata or debug output when non-default.

**Acceptance**

- Operators can reproduce a job's performance mode from logs/manifest metadata.
- Defaults remain conservative.
- Invalid values fail early with typed errors.

### Sprint P5: Faker Pool Determinism and Parallel-Readiness

**Target complete:** after P0 baselines identify faker/provider cost; before
work-node parallelism.

**Deliverables**

- Remove or isolate shared mutable Faker/RNG state from provider pool building.
- Key every pool identity by provider, locale, namespace, seed, pool size, and
  provider config.
- Make pool build byte-stable regardless of worker scheduling.

**Acceptance**

- Existing faker output snapshots remain stable.
- Rebuilding the same pool in two independent workers produces identical values.
- Concurrent builds for different identities do not share mutable RNG state.

---

## 4. What Should Wait for Relationship PR #22

These are the high-value relationship-heavy wins, but they overlap the active
memory-scaling branch and should wait until its contracts are merged or replaced.

### Sprint R1: Native FK Resolution

**Target complete:** after PR #22 lands and the sequential/out-of-core contract
is final.

**Deliverables**

- Replace Python parent-map resolution for admitted large jobs with native
  Arrow/Polars/DuckDB joins or staged parent-key relations.
- Preserve exact orphan-policy semantics: `fail`, `remap`, `warn`, and
  `preserve`.
- Preserve canonical FK match-key normalization and null-sentinel behavior.

**Acceptance**

- No Python structure is sized by total parent or child cardinality on the
  out-of-core path.
- FK parity tests cover int/float normalization, null matching, composite keys,
  multi-parent edges, or explicitly reject unsupported shapes.
- Peak RSS scales with chunk/batch size plus narrow staged relations, not table
  width.

### Sprint R2: Relationship-Aware Auto-Routing

**Target complete:** after R1 has parity and memory gates.

**Deliverables**

- Plug sequential and out-of-core relationship routes into the execution-mode
  planner.
- Prefer `run_sequential` with a transactional sink for production FK jobs where
  it is the measured winner.
- Select out-of-core only for shapes where the memory gate proves a win.

**Acceptance**

- Planner chooses full-frame, sequential, or out-of-core based on compatibility
  and measured thresholds.
- Every relationship route emits the same manifest/evidence contract.
- Failed jobs abort transactional staged output.

### Sprint R3: Polars-Native Composite and FK Expansion

**Target complete:** after R1/R2; not before relationship semantics are stable.

**Deliverables**

- Admit FK/composite jobs to Polars-native execution where the route is proven.
- Keep fallback-to-pandas as a compatibility path until the native route covers
  the public contract.
- Expand native support one strategy and relationship shape at a time.

**Acceptance**

- Substrate matrix remains green.
- No silent downgrade: executed substrate remains recorded.
- Composite bundles and FK children preserve byte output or documented semantic
  parity.

---

## 5. Work-Node Parallelism

### Sprint P6: Dependency-Level Parallel Dispatch

**Target complete:** after P5; after P1/P2 have stable adapter/planner routing.

**Deliverables**

- Use the existing work ordering graph to group independent nodes into levels.
- Run independent nodes within a level concurrently.
- Keep FK children, composite bundles, grouped/windowed strategies, and any
  mutable-state handler serialized until explicitly proven safe.

**Acceptance**

- Output is byte-stable across worker counts.
- Timing collector remains correct under threads/processes.
- Tests cover worker counts 1, 2, and `os.cpu_count()` on representative jobs.

### Sprint P7: Process-Level Partitioning for Python-Heavy Strategies

**Target complete:** after P6 proves scheduler determinism.

**Deliverables**

- Partition unavoidable pure-Python strategy work by row chunk or table chunk.
- Derive partition seeds from the job seed and stable coordinates, never from
  worker order.
- Start with one Python-heavy strategy whose bottleneck is proven by P0.

**Acceptance**

- Byte output is invariant across partition counts.
- Process overhead is lower than saved Python loop time at the target row scale.
- Failure handling preserves partial-output cleanup semantics.

---

## 6. Conversion and I/O

### Sprint P8: Reduce Boundary Conversion

**Target complete:** after P1 shows which jobs still fall back to pandas.

**Deliverables**

- Keep Arrow-backed arrays through simple strategies.
- Avoid `pa.Table -> pandas -> pa.Table` round trips where the selected substrate
  can operate directly.
- Capture per-leg conversion metrics in every adapter route.

**Acceptance**

- Conversion time decreases on scalar/string-heavy jobs.
- Schema parity is documented where Arrow/Polars string widths differ.
- No new pandas nullable-int divergence is introduced.

### Sprint P9: Pushdown and Lazy Plans

**Target complete:** after P8; best for a later performance cycle.

**Deliverables**

- Push projection/filter work into source scans where possible.
- Chain compatible Polars expressions lazily instead of materializing after
  each node.
- Use DuckDB only for relational/spill operations where it has a measured win.

**Acceptance**

- Fewer columns and rows are loaded for jobs with filters/projections.
- Lazy execution improves elapsed time or peak RSS on benchmark fixtures.
- No masking logic is duplicated in SQL.

---

## 7. Caching

### Sprint P10: Cache Profiles, Plans, Pools, and Reference Tables

**Target complete:** after correctness-sensitive routing changes land.

**Deliverables**

- Cache compiled plans by validated config, profile schema fingerprint, engine
  version, and relevant feature flags.
- Cache profiles where source schema and sampling seed are unchanged.
- Cache provider pools and reference tables with bounded memory and explicit
  invalidation.

**Acceptance**

- Repeated platform jobs avoid redundant profile/compile/pool setup.
- Cache keys include seed, namespace, locale, provider config, engine version,
  and source schema where relevant.
- Cache misses are safe by default; stale cache cannot change output bytes.

---

## 8. Recommended Order

1. **P0 now:** benchmark gates and baseline.
2. **P1 follow-up:** measure the selected-adapter route and broaden gates.
3. **P2 now:** add planner shell in observe-only mode.
4. **P3 now:** auto-select chunked for eligible large single-table jobs.
5. **P4 anytime:** expose knobs and job metadata.
6. **P5 before parallelism:** make Faker pools deterministic under concurrency.
7. **Wait for PR #22:** do R1/R2/R3 relationship-native work.
8. **After P5 and R-route stability:** P6/P7 parallel dispatch and partitioning.
9. **After route selection is stable:** P8/P9 conversion and pushdown work.
10. **After behavior settles:** P10 caching.

The practical completion rule: finish P0-P4 before changing defaults broadly;
finish P5 before parallel work; finish relationship PR #22 before native FK
rewrites; finish R1/R2 before making relationship auto-routing the default.

---

## 9. Open Risks

- Polars-native scalar wins can still be hidden by planner choices, conversion
  overhead, or fallback routes even though `run_pipeline` now honors adapter
  selection for mask jobs.
- A planner that silently falls back can make performance unpredictable. Every
  fallback needs an explicit reason.
- Relationship routing must not change orphan-policy bytes or raw-key leakage
  posture.
- Parallel Faker work can break determinism if RNG state is shared.
- Process-level partitioning can cost more than it saves on small jobs.
- Caches can become correctness bugs if keys omit seed, namespace, locale,
  provider config, schema, or engine version.

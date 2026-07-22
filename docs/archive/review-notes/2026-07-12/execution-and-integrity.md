# Execution and Data-Integrity Adversarial Review - 2026-07-12

> Lead re-anchor: the frozen reviewed revision is
> `c1c4f2c2b33af39e1de4874788a7df78a352970c`. Its only source deltas from the
> reviewed behavioral baseline are typing annotations in isolated-run modules; the remaining
> changes are CI/dependency metadata. The findings below remain current. The full frozen-revision
> suite passed: `6281 passed, 38 skipped, 13 deselected`.
> An uncommitted Track B branch appeared after this gate and partially addresses the isolated
> out-of-core loader; it is excluded from this note's finding disposition pending a stable commit.

## Scope and baseline

This note covers the critical execution path in `decoy-engine`: profiling, plan compilation,
route selection, pandas/Polars adapters, sequential and out-of-core FK execution, chunked
execution, synthetic generation, transactional publication, and memory-safety controls.

- Reviewed commit: `1e80016bdbf1aa100f7f21215c919df4c69f41b1` (`origin/main`).
- The shared checkout contained concurrent annotation-only edits in isolated-run modules and CI
  metadata. Those edits were not changed or treated as remediation.
- Earlier July 9 findings were used only as leads. Every finding below was re-verified against
  this commit's source, tests, or an executable counterexample.
- This is a read-only review. No production source was changed.

## System map

The public execution lifecycle is:

1. A caller validates through `PipelineConfig`, then calls `run_pipeline()` with its dumped dict.
2. `run_pipeline()` normalizes the seed, performs bounded profiling, compiles a frozen plan, and
   builds namespace and relationship graphs.
3. Relationship routing chooses full-frame, sequential, or DuckDB out-of-core execution.
4. A separate planner/classifier chooses Polars-native, chunked, or pandas fallback for eligible
   non-relationship jobs.
5. Route-specific adapters generate/mask data, resolve FKs, handle row errors/quarantine, and
   return or stage Arrow tables.

Primary orchestration sources: `src/decoy_engine/execution/_pipeline.py:153-429`,
`src/decoy_engine/execution/_pipeline_routing.py:1-90`,
`src/decoy_engine/execution/_pipeline_route_exec.py:81-324`, and
`src/decoy_engine/execution/_planner.py:45-149`.

## Findings directory

| Rank | Severity | Finding | Principal failure mode |
|---:|:---:|---|---|
| 1 | HIGH | Quarantine output is outside the transaction | Failed jobs publish raw quarantined rows |
| 2 | HIGH | Lazy out-of-core routing eagerly materializes every table | OOM before the bounded runner; false telemetry |
| 3 | HIGH | Pandas/sequential FK resolution rounds integers above `2**53` | Silent identifier corruption |
| 4 | HIGH | Bounded profiling is deterministic head sampling | Ordered tails are invisible to checks and routing guards |
| 5 | MEDIUM | Generate-column validation accepts unknown parameters | Operator typos silently change distributions |
| 6 | MEDIUM | Same-config generated columns are byte-identical | Unrealistic correlated synthetic data |
| 7 | MEDIUM | Memory routing still has multiple authorities and opt-in safety | Default decisions rely on one 32-GB fixture model |
| 8 | MEDIUM | Polars-native claims include pandas ports and a no-op knob | Misleading explain/telemetry and performance controls |
| 9 | MEDIUM | Auto-chunk execution reassembles and copies all chunks | Public route is not end-to-end streaming |

## 1. HIGH - Quarantine output is outside the transaction

### Problem statement

Sequential execution writes quarantine JSONL directly to its final path before committing the
transactional table sink. If the table commit fails, `abort()` cleans the table staging area but
cannot undo the quarantine write. A failed job can therefore publish raw source rows, overwrite a
previous quarantine artifact, and claim no table output was committed while leaving sensitive
evidence behind.

### Sources and evidence

- `src/decoy_engine/execution/_sequential.py:369-398` writes quarantine at lines 383-384, then
  commits the table sink at lines 397-398.
- `src/decoy_engine/execution/_sequential.py:400-408` aborts only the sink after failure.
- `src/decoy_engine/quarantine.py:260-275` creates the final parent and opens the final path with
  mode `"w"`; it has no staging or rollback protocol.
- `src/decoy_engine/execution/_isolated_worker.py:128-145` runs the whole pipeline before staging
  Arrow outputs. A quarantine path embedded in config is consequently also outside isolated-run's
  staging directory.
- The existing transaction tests cover table files but not the quarantine side effect:
  `tests/unit/execution/test_transactional_sink.py:229-266`.

Current-HEAD counterexample: a valid relationship job with one quarantined `format_error` was run
through the sequential route with a `TransactionalSink` whose `commit()` raises. The run raised
`RuntimeError: commit boom`, published no table target, but left
`quarantine_exists True quarantine_bytes 226`.

### Resolution

Introduce one run-level transaction coordinator for all durable side effects: table outputs,
quarantine, vault/evidence artifacts, and the final manifest. Every writer should stage beneath a
run-scoped sibling directory; one atomic directory rename should publish the complete artifact set.
For isolated execution, rewrite config-owned output paths to the child's staging root and expose
only committed artifact-relative paths in the result envelope.

### Verification gate

- Sink commit failure leaves the prior table target and prior quarantine file byte-identical.
- SIGKILL after quarantine staging publishes neither tables nor quarantine.
- Retry after failure cannot observe or append stale quarantine data.
- A successful run atomically publishes tables, quarantine, and manifest together.

## 2. HIGH - Lazy out-of-core routing eagerly materializes every table

### Problem statement

The public lazy route resolves every topological table through `source_loader()` into resident
`pa.Table` objects before invoking the out-of-core runner. This defeats the reason for selecting
out-of-core execution and can OOM before DuckDB's bounded batches run. The emitted telemetry then
reports `loaded_fully_in_memory=False`, even though route execution just loaded every table.

### Sources and evidence

- `src/decoy_engine/execution/_pipeline_route_exec.py:195-207` explicitly documents the full
  per-table residency cost.
- `src/decoy_engine/execution/_pipeline_route_exec.py:212-218` materializes all missing topological
  tables and retains them in `resolved_sources`.
- `src/decoy_engine/execution/_pipeline_route_exec.py:242-248` calculates telemetry from the
  original caller shape, not the resolved source types.
- `src/decoy_engine/execution/_pipeline_route_exec.py:44-78` therefore equates an empty original
  `sources` mapping plus a loader with bounded residency.
- The lower-level runner already supports lazy sources:
  `src/decoy_engine/execution/out_of_core/_runner.py:85-113` accepts `pa.Table | LazySource`, and
  `src/decoy_engine/execution/out_of_core/_runner.py:356-360` iterates lazy batches.
- The route test locks in eager loading rather than bounded behavior:
  `tests/parity/test_out_of_core_group_b_routing.py:128-155`.

Current-HEAD counterexample using the route's existing two-table fixture loaded both
`['parent', 'child']` before execution, then emitted:

```text
{'execution_mode': 'out_of_core', 'route_reason': 'override_out_of_core',
 'eviction': 'per_table', 'outputs_streamed': False,
 'loaded_fully_in_memory': False}
```

### Resolution

Change the loader contract to return `TableSource = pa.Table | LazySource` and pass that mapping
unchanged into `run_fk_out_of_core()`. Local Parquet descriptors should become `LazySource`
instances without crossing a full-table Arrow boundary. Cloud implementations need the same
batch-readable protocol. Compute telemetry from the resolved source capabilities, and represent
input residency and output residency as separate fields.

### Verification gate

An end-to-end `run_pipeline(..., source_loader=..., sink=...)` run over Parquet-backed FK tables
must complete under a hard memory cap while monkeypatched full-table reads fail. The gate must use
the public route, not only `run_fk_out_of_core()` directly, and assert truthful telemetry.

## 3. HIGH - Pandas/sequential FK resolution silently rounds large integers

### Problem statement

When FK output mixes a float parent value with a preserved or warned integer orphan above
`2**53`, pandas coerces the result column to `float64` and silently changes the identifier. This is
the default full-frame behavior and is also used by the sequential route. The out-of-core route
correctly fails closed for the same shape, so semantics differ by route.

### Sources and evidence

- `src/decoy_engine/execution/_pandas_adapter.py:424-453` builds Python FK values and assigns the
  mixed list directly to a pandas column, triggering NumPy float coercion.
- `tests/parity/test_out_of_core_fk_parity.py:997-1057` pins the behavior: source orphan
  `9007199254740993` becomes `9007199254740992.0` in the pandas oracle while out-of-core raises
  `out_of_core_fk_key_dtype_unsupported`.
- `tests/parity/SEMANTIC_DIFFERENCES.md:40-47` correctly describes this as silent referential-
  integrity drift.
- Sequential execution uses the same `PandasExecutionAdapter` FK dispatch:
  `src/decoy_engine/execution/_pipeline_route_exec.py:122-131`.

The focused current-HEAD test passed because it asserts the known corruption as the oracle's
expected output. This is a test-contract defect, not evidence that the behavior is safe.

### Resolution

Define one lossless FK result-typing contract shared by all routes. Resolve into Arrow-native
arrays or a representation that preserves matched and orphan values, then cast only after proving
every output value is representable. If no lossless common type exists, all routes must fail closed
with the same typed error and context. Do not use pandas coercion as the semantic oracle for key
columns.

### Verification gate

Parameterize full-frame, sequential, and out-of-core tests across PRESERVE/WARN/REMAP/FAIL,
matched and orphan values, signed boundaries, `2**53 +/- n`, nulls, and composite keys. Assert both
Arrow schema and exact values; no route may silently change an identifier.

## 4. HIGH - Bounded profiling is deterministic head sampling

### Problem statement

SC7a removed full-source materialization, but also changed representative random sampling into
the first `N` rows for every supported file format. Ordered and partitioned sources can hide tail
categories, nulls, longer strings, PII shapes, or high-cardinality behavior. The profile is marked
sampled and stamped with a seed, but the seed does not affect the bounded sample.

### Sources and evidence

- Parquet reads first batches: `src/decoy_engine/profile/_readers.py:134-157`.
- Fixed-width reads first records: `src/decoy_engine/profile/_readers.py:163-190`.
- CSV uses `pd.read_csv(..., nrows=sample_rows)`:
  `src/decoy_engine/profile/_readers.py:197-227`.
- `_profile_one_source()` passes that already-capped frame to the walker:
  `src/decoy_engine/profile/_source.py:207-224`.
- `walk_dataframe()` random-samples only when `frame_len > sample_rows`; a frame already capped to
  exactly `sample_rows` bypasses the RNG: `src/decoy_engine/profile/_walk.py:109-125`.
- The sampled distinct count feeds a memory-probe uniqueness guard even though the source warns
  that undercounting can defeat it: `src/decoy_engine/execution/_pipeline_routing_signals.py:418-434`.
- Existing bounded tests assert read caps and invariants, not representativeness:
  `tests/unit/profile/test_bounded_profiling.py:62-110`.

Current-HEAD counterexample: a 200-row ordered Parquet file put category `A` and non-null values in
the first 100 rows, category `B` and nulls in the final 100. With `sample_rows=100`, the profile
reported `segment(null_count=0, distinct_count=1)` and `nullable(null_count=0)`, despite exact
`row_count=200`.

### Resolution

Separate exact structural metadata from sampled distribution statistics. For Parquet, use footer
schema plus deterministic row-group-stratified sampling. For stream-only formats, use a bounded-
memory reservoir when a full byte scan is acceptable, or an explicitly named head sample when it
is not. Stamp `sample_method`, sample size, coverage, and uncertainty. Never use a sampled distinct
count as proof of safe admission; treat it as unknown/conservative unless an exact statistic is
available.

### Verification gate

Add adversarial ordered datasets with late-only nulls, categories, wide values, and unique keys;
seed-change tests; row-group coverage assertions; and tolerance checks against full profiles.
Safety decisions must remain conservative when statistics are sampled.

## 5. MEDIUM - Generate-column validation accepts unknown parameters

### Problem statement

`GenerateColumnConfig` is the exception to the package's advertised strict validation rule. It
allows arbitrary extra keys, so optional-parameter typos survive `PipelineConfig.model_validate()`
and are ignored by the generator. The job succeeds with different semantics than the operator
requested.

### Sources and evidence

- `src/decoy_engine/config/__init__.py:1-16` states `extra="forbid" at every level`.
- `src/decoy_engine/config/_tables.py:101-118` deliberately sets `extra="allow"`.
- `src/decoy_engine/config/_tables.py:143-199` validates only required parameters and explicitly
  leaves unrelated extras accepted.
- `src/decoy_engine/generation/synthesize.py:358-377` reads `weights`; a misspelled key falls back
  to uniform sampling.

Current-HEAD counterexample: a complete `PipelineConfig` with categorical key `weigths: [1, 0]`
validated and dumped that typo unchanged. Generation then sees no `weights` and samples uniformly.

### Resolution

Replace the flat open model with a discriminated union of typed per-generator models using
`extra="forbid"`. If flat serialization must remain stable, preserve it at the serialization layer
rather than allowing an unbounded input schema. Generate the dispatch registry and documentation
from the same union so validation and execution cannot drift.

### Verification gate

For every generator type, test its valid keys plus misspellings and unknown keys through the full
`PipelineConfig` choke point. Unknown parameters must fail before profiling or generation.

## 6. MEDIUM - Same-config generated columns are byte-identical

### Problem statement

Generation seed identity excludes the column name, and there is no separate stable column identity.
Two semantically different columns with identical generator configuration therefore emit the same
value on every row. For example, separately declared first-name and middle-name columns can collapse
to identical columns. This preserves rename stability but produces unrealistic synthetic data.

### Sources and evidence

- `src/decoy_engine/generators/derivation.py:1-29` defines the name-independent contract.
- `src/decoy_engine/generators/derivation.py:43-56` excludes `name` from the fingerprint.
- `src/decoy_engine/generators/derivation.py:200-222` derives the column root solely from that
  fingerprint and the job key/seed.
- `tests/unit/test_r3_10_generation_key_contract.py:1-18` declares same-config equality as a V1
  contract, and lines 122-128 assert byte-identical outputs for differently named columns.

### Resolution

Add a stable immutable `generation_id` or `seed_namespace` that is separate from the display name.
Bind derivation to that identity. Renames retain the ID and remain stable; independently declared
columns receive distinct IDs and decorrelate. Treat this as a seed-protocol migration with an
explicit compatibility mode; do not silently change existing outputs.

### Verification gate

The same ID must reproduce across runs and renames; distinct IDs with the same strategy/config must
not be lockstep; legacy manifests must either retain old output under a declared compatibility
version or fail with a migration instruction.

## 7. MEDIUM - Memory routing has multiple authorities and opt-in safety

### Problem statement

The live relationship router, static execution planner, chunk router, estimator/probe, and runtime
governor do not form one authoritative execution plan. Default production behavior still uses row
thresholds calibrated to one width-16, 32-GB fixture. The newer byte estimator and probe are off by
default, and the runtime governor is a standalone primitive not wired into `run_pipeline()` or the
platform worker.

### Sources and evidence

- `src/decoy_engine/execution/_planner.py:56-93` keeps full planner routing disabled.
- `src/decoy_engine/execution/_planner.py:117-149` documents fixed 5M/7.5M thresholds based on one
  32-GB width-16 memory model.
- `src/decoy_engine/execution/_planner.py:256-269` defers FK disposition to another router.
- `src/decoy_engine/execution/_pipeline.py:178-179` defaults estimator and probe routing off.
- `src/decoy_engine/execution/_pipeline_routing.py:10-74` describes two independent live routing
  layers.
- `src/decoy_engine/execution/_governor.py:45-55` defaults the governor off and states it is not
  wired into the public pipeline or platform worker.

### Resolution

Create one immutable `ExecutionPlan` that owns selected route, rejected alternatives, compatibility
result, estimated peak, source-residency requirements, sink requirements, and fallback ladder.
Execute that object without re-deciding. Replace host-specific row constants with cgroup-aware byte
budgets calibrated from telemetry, then graduate the estimator/governor behind an explicit staged
rollout and rollback policy.

### Verification gate

One matrix test should prove `explain == selected route == executed route` for every strategy,
relationship shape, source capability, host budget, and override. Run bounded-memory sentinels on
narrow/wide and low/high-cardinality fixtures under cgroup limits, not only a row-count proxy.

## 8. MEDIUM - Polars-native claims include pandas ports and a no-op knob

### Problem statement

The Polars adapter classifies every handler-registry key as native, but seven handlers convert the
target column to pandas and back. Planner explanations and execution telemetry label those
strategies `polars`, masking conversion costs. `max_workers` is validated and stamped but explicitly
unused.

### Sources and evidence

- `src/decoy_engine/execution/polars/_polars_adapter.py:70-102` defines all registry keys as native
  and stores unused `max_workers` at lines 89-90.
- `src/decoy_engine/execution/polars/_polars_adapter.py:153-160` admits registry keys to the pure
  Polars loop.
- `src/decoy_engine/execution/polars/_strategies/__init__.py:54-90` registers native handlers and
  pandas ports in one undifferentiated mapping.
- `src/decoy_engine/execution/polars/_strategies/_pandas_port.py:26-47` performs Polars -> pandas ->
  Polars conversion per strategy.
- `src/decoy_engine/execution/polars/_polars_adapter.py:230-239` stamps every work node as `polars`.
- `src/decoy_engine/execution/_planner.py:272-313` uses the same registry to explain native status.

### Resolution

Give each capability one machine-readable disposition: `native`, `ported`, `fallback`, or
`unsupported`. Planner, adapter, telemetry, and benchmarks must consume the same metadata. Reject
non-default no-op controls until implemented, or remove them from the public surface. Telemetry
should report actual substrate transitions and conversion time per strategy.

### Verification gate

Each strategy's route explanation must match observed substrate transitions. A control-behavior test
must show that changing every exposed performance knob changes a measured execution property, or
validation must reject that setting.

## 9. MEDIUM - Auto-chunk execution reassembles and copies all chunks

### Problem statement

The manual chunk iterator is streaming, but the public auto-chunk route collects every masked chunk
and every `ExecutionResult`, concatenates them, then calls `combine_chunks()`. It begins with an
already-resident input `pa.Table` and has no sink-aware output path. Peak memory can include the
source, all masked chunk buffers, the concatenated table, and a contiguous-copy output at once.

### Sources and evidence

- `src/decoy_engine/execution/_pipeline_route_exec.py:301-318` builds `chunk_results`, materializes
  `masked_chunks = list(...)`, and then concatenates.
- `src/decoy_engine/execution/_chunked.py:280-389` shows the underlying manual API is a lazy iterator.
- `src/decoy_engine/execution/_chunked.py:447-503` normalizes every chunk, concatenates, and calls
  `combine_chunks()`.
- `src/decoy_engine/execution/_pipeline_chunk_route.py:30-70` accepts only resident caller tables
  for auto-route classification.

### Resolution

Make auto-chunk sink-aware and write each masked batch through `TransactionalSink.write_batches()`.
Aggregate only compact timing/warning counters. When the caller explicitly requests a resident
table, retain chunked Arrow buffers unless a contiguous array is a documented downstream
requirement; avoid unconditional `combine_chunks()`.

### Verification gate

Measure peak RSS through the public auto route with wide variable-width columns. With a sink, peak
must remain approximately `O(input chunk + output chunk + fixed overhead)` as row count grows. Add
a sentinel that fails if the implementation stores every chunk or calls `combine_chunks()` on the
streaming path.

## Reconciliation with the July 9 review

- The old finding that profiling eagerly loads whole Parquet/CSV sources is stale at this commit:
  SC7a introduced bounded readers, and SC7b routes from profile row counts before invoking the lazy
  execution loader.
- The old reject-after-eager-profile finding is therefore also stale in its original form.
- End-to-end boundedness remains unproven and functionally broken on the public lazy out-of-core
  executor because it materializes all tables after admission. `CHANGELOG.md:12-26` itself says
  SC7c had not landed at the reviewed baseline.
- The July routing-complexity and partial-Polars findings remain current.
- Out-of-core's narrower admitted strategy set is not itself a defect: its compatibility gates
  intentionally fail closed. The defect is the public loader/residency boundary around that runner.
- Previously reported bool-seed, shuffle-source, seed-width, and mypy-override issues are closed and
  are not repeated here.

## Verification record

Focused tests run against current HEAD:

```bash
.venv/bin/pytest -q \
  tests/unit/profile/test_bounded_profiling.py \
  tests/unit/execution/test_lazy_path_route_admission.py \
  tests/unit/execution/test_transactional_sink.py \
  tests/parity/test_out_of_core_fk_parity.py::test_matched_float_and_int_orphan_beyond_precision_fails_closed \
  tests/unit/test_r3_10_generation_key_contract.py
```

Result: `51 passed in 2.16s`.

Additional current-HEAD probes established:

- ordered Parquet tail values are absent from bounded profiles;
- the lazy out-of-core route loads every topology table but reports bounded input residency;
- sequential commit failure leaves a 226-byte final quarantine JSONL;
- a misspelled optional generate parameter survives the full `PipelineConfig` choke point.

## Limitations

- This slice did not run the entire suite, cloud integration tests, or multi-million-row RSS probes.
- No external platform worker was inspected here; the conclusion that the governor is unwired is
  based on the engine's own current source statement.
- Performance findings are based on object-lifetime/source analysis and existing fixture design;
  precise peak multipliers require the SC7c-style public-route memory harness.
- Review conclusions apply to commit `1e80016`; uncommitted concurrent annotation/CI edits were not
  part of the behavioral baseline.

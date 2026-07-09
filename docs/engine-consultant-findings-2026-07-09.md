# Decoy Engine Consultant Findings - 2026-07-09

External architecture and engine-design review of `decoy-engine` at commit
`02b18cc` on branch `main`.

Scope: broad architecture, engine design, capability gaps, missing pieces,
large risks, efficiencies, and missing tests. This review intentionally avoids
copy/documentation polish except where code/docs disagree about executable
behavior.

## Executive Summary

The engine shows strong engineering discipline: strict config validation,
fail-closed strategy gates, parity harnesses, sentry tests, and explicit
compatibility boundaries. The central architecture is coherent: validate a
pipeline config, profile sources, compile a frozen plan, then execute through a
route-specific adapter.

The main issue is that the bounded-memory story is not end-to-end true through
the public `run_pipeline()` path. Routing can choose sequential or out-of-core
execution, but the profiling step still eagerly loads configured sources before
route selection. This can OOM a large job before the out-of-core route gets a
chance to run.

The second issue is design complexity around execution routing. Planner,
relationship routing, chunk routing, out-of-core compatibility, and route
execution are split across multiple modules with intentionally overlapping
knowledge. The current tests are good, but this shape will get harder to change
as more strategies and substrates land.

## Review Inputs

- Source tree under `src/decoy_engine`.
- Graphify cache for `decoy-engine`: 3,837 nodes, 7,304 edges, 247
  communities. Graph hubs included `ColumnSeed`, `Table`, and
  `StrategyContext`.
- `README.md`, `CODEMAP.md`, `pyproject.toml`, routing/execution modules, and
  representative test suites.
- Targeted verification:

```bash
.venv/bin/pytest -q \
  tests/unit/execution/test_pipeline_routing.py \
  tests/unit/execution/test_out_of_core_routing.py \
  tests/unit/execution/test_auto_chunk_routing.py \
  tests/sentry/test_public_import_boundary.py \
  tests/sentry/test_source_hygiene.py \
  tests/unit/test_public_api.py
```

Result: `125 passed in 20.60s`.

Full collection check:

```bash
.venv/bin/pytest --collect-only -q
```

Result: `5919/5932 tests collected (13 deselected)`.

Coverage note: coverage tooling was not installed in the repo virtualenv
(`.venv/bin/python -m coverage` failed with `No module named coverage`), so this
review does not claim line or branch coverage.

## Findings

### F1 - HIGH - `run_pipeline()` is not end-to-end bounded-memory

**Evidence**

- `run_pipeline()` profiles before making the execution route decision:
  `src/decoy_engine/execution/_pipeline.py:302`.
- `profile_source()` iterates `config["sources"]` and calls `_load_source()`:
  `src/decoy_engine/profile/_source.py:100-103`.
- `_load_file_source()` uses eager pandas reads for CSV and Parquet:
  `src/decoy_engine/profile/_source.py:154-163`.
- S3 and GCS profile reads also fetch the full object into memory before pandas
  reads it: `src/decoy_engine/profile/_source.py:176-290`.
- Routing only happens after `profile_source()` and `compile_plan()`:
  `src/decoy_engine/execution/_pipeline.py:302-334`.

**Why it matters**

The out-of-core and sequential routes are designed to reduce masking memory
pressure, but a large job can fail during profiling before those routes are
selected. This undermines the advertised scaling architecture for
`run_pipeline()` even if lower-level out-of-core runner tests pass.

The test suite already has direct out-of-core streaming tests and memory
sentinels, but those exercise route internals and probe scripts. They do not
prove the public `run_pipeline()` lifecycle is bounded from source acquisition
through output.

**Recommended work**

1. Introduce a profiling abstraction that can use the same source residency mode
   as execution:
   - `profile_loader(table) -> batches/table metadata`, or
   - `ProfileSource` protocol with file, lazy Parquet, S3, GCS implementations.
2. Split profile requirements by downstream checks:
   - structural table/column metadata,
   - bounded samples,
   - exact distinct/count data only when required.
3. Decide route-admission before full profiling where possible. For large FK
   jobs, the admission decision should be able to use cheap file/object metadata
   plus config and relationship shape.
4. Add an end-to-end `run_pipeline()` memory sentinel for a lazy/out-of-core job,
   not only direct `run_fk_out_of_core()` or probe-script tests.

**Acceptance tests**

- A `run_pipeline(execution_mode="out_of_core", source_loader=..., sink=...)`
  test that monkeypatches resident full-table conversion in the profile path to
  fail, proving profiling did not eagerly materialize sources.
- A regression test where large Parquet-backed FK inputs complete through
  `run_pipeline()` under a memory cap that full-frame profiling would exceed.
- A test that validates route telemetry cannot claim bounded input residency if
  profiling has already loaded all sources resident.

### F2 - HIGH - Full-frame reject-before-read happens after eager profiling

**Evidence**

- Reject-before-read is implemented in `decide_execution_route()`:
  `src/decoy_engine/execution/_pipeline_routing.py:401-429`.
- That decision receives `largest_table_rows` after profiling and plan compile:
  `src/decoy_engine/execution/_pipeline.py:302-334`.
- The planner constants target large FK jobs at 5,000,000 and 7,500,000 rows:
  `src/decoy_engine/execution/_planner.py:117-149`.

**Why it matters**

The code intends to reject large FK jobs before a full-frame OOM. In the current
public path, the engine can still perform expensive/eager source reads during
profiling before it reaches the reject. That is a semantic mismatch between the
name of the safety mechanism and its actual position in the lifecycle.

**Recommended work**

1. Move coarse large-job rejection/admission ahead of eager profile reads.
2. Use source metadata to compute row-count estimates for Parquet and cloud
   object sources when possible.
3. If exact metadata cannot be obtained cheaply, require an explicit operator
   override rather than guessing.

**Acceptance tests**

- A config with a huge declared/metadata row count and unsupported FK route
  fails with `fk_full_frame_oom_risk_rejected` before invoking source reads.
- A lazy-loader-only config can still be forced into out-of-core when metadata
  is absent but the operator explicitly selects it.

### F3 - MEDIUM - Execution routing has too many active decision surfaces

**Evidence**

- `classify_job()` is an explain/static planner surface:
  `src/decoy_engine/execution/_planner.py:174-269`.
- The actual relationship route is decided elsewhere:
  `src/decoy_engine/execution/_pipeline_routing.py:262-430`.
- The planner explicitly says relationship routes are deferred to the live
  router: `src/decoy_engine/execution/_planner.py:36-47`.
- `PLANNER_ROUTING_ENABLED` is a hard `False` seam:
  `src/decoy_engine/execution/_planner.py:89-93`.
- Chunk routing calls back into `classify_job()` from
  `decoy_engine.execution._pipeline_routing`.

**Why it matters**

The current split is understandable from an incremental delivery history, but
it creates a drift risk:

- explain mode can diverge from route behavior,
- route admission can diverge from execution compatibility,
- new strategy work must update several registries/gates,
- staff need to know which planner is authoritative for which route.

The tests are doing real work here. That is good, but it is also evidence that
the design needs many guardrails to stay correct.

**Recommended work**

1. Promote one `ExecutionPlanner` object/function to own all route decisions:
   polars-native, chunked, sequential, out-of-core, and full-frame.
2. Return a single immutable plan containing:
   - selected route,
   - admission/rejection reasons,
   - required source residency,
   - streaming/sink requirements,
   - route-specific compatibility result.
3. Make `run_pipeline()` execute that plan rather than recomputing decisions.
4. Retire or explicitly delete `PLANNER_ROUTING_ENABLED` once all live routes are
   planner-owned.

**Acceptance tests**

- One golden explain-plan fixture per route.
- Property or parameterized test proving `execution_plan.mode` and actual
  telemetry route cannot disagree.
- Mutation-style test: change one compatibility gate and verify planner/executor
  drift is caught.

### F4 - MEDIUM - Public API exports incomplete/stubbed capabilities

**Evidence**

- `SchemaInspector` is exported from `decoy_engine.__init__` and raises
  `NotImplementedError`: `src/decoy_engine/schema/inspector.py:11-13`.
- `LicenseVerifier.verify()` always returns a free-tier license:
  `src/decoy_engine/license/verifier.py:16-23`.
- `decoy_engine.__init__` documents both as public contract pieces:
  `src/decoy_engine/__init__.py:1-36`.

**Why it matters**

Public exports shape caller expectations. Stubs are acceptable pre-GA, but they
must be treated as release blockers or clearly scoped beta surfaces. Otherwise
platform/CLI code can build around behavior that is explicitly fake.

**Recommended work**

1. Decide for each stub:
   - implement before GA,
   - move behind an experimental namespace,
   - remove from top-level `__all__`, or
   - keep exported but document as intentionally unavailable in a machine-checkable
     capability matrix.
2. Add a release gate that fails if GA mode is enabled while known stubs remain
   top-level public exports.

**Acceptance tests**

- In pre-GA, public-stub tests assert the exact failure/placeholder behavior.
- In GA, a sentry test rejects exported stubs unless explicitly allowlisted with
  a removal date.

### F5 - MEDIUM - Polars is not yet a full native execution substrate

**Evidence**

- Pandas handler registry covers the broad scalar strategy surface:
  `src/decoy_engine/execution/_strategies/__init__.py:41-68`.
- Polars native registry covers a smaller set and wraps many strategies through
  `PandasStrategyPort`: `src/decoy_engine/execution/polars/_strategies/__init__.py:54-90`.
- Parity tests intentionally accept value-level equality rather than Arrow type
  equality: `tests/parity/test_strategy_substrate_parity.py:64-71`.

**Why it matters**

The Polars path is useful, but "Polars substrate" does not mean fully native
Polars execution for many strategies. That affects performance expectations and
can surprise platform admission or job-estimation logic.

**Recommended work**

1. Maintain a machine-readable per-strategy substrate matrix:
   - pandas oracle,
   - polars native,
   - polars via pandas port,
   - unsupported/fallback.
2. Emit this distinction in execution telemetry when a Polars run falls back or
   ports through pandas.
3. Prioritize native ports by actual workload mix, not by strategy count.

**Acceptance tests**

- A test that every pandas strategy is classified in the substrate matrix.
- Telemetry assertions for native vs ported vs fallback Polars execution.
- Performance tests only claim Polars speedup for strategies marked native.

### F6 - MEDIUM - Out-of-core is a well-gated subset, not a general route

**Evidence**

- Out-of-core payload strategies are explicitly listed in
  `src/decoy_engine/execution/out_of_core/_compat.py:20-48`.
- Several strategies are deliberately deferred with concrete rejection reasons:
  `src/decoy_engine/execution/out_of_core/_compat.py:49-118`.
- Parent-key strategy support is narrower than payload-column support:
  `src/decoy_engine/execution/out_of_core/_compat.py:120-139`.

**Why it matters**

The fail-closed design is good. The risk is product and platform admission drift:
external callers can overestimate out-of-core eligibility if they only check
strategy membership or do not distinguish payload vs FK parent-key usage.

**Recommended work**

1. Expose a single public admission API that accepts config/profile/source
   metadata and returns the authoritative route decision.
2. Avoid external callers using `OUT_OF_CORE_SUPPORTED_STRATEGIES` as anything
   stronger than a necessary-but-not-sufficient hint.
3. Keep deferred strategy reasons stable enough for platform UX to display.

**Acceptance tests**

- Platform-facing admission tests using representative configs, not only
  strategy names.
- Tests for parent-key unsupported cases for every strategy that is payload-only
  admitted.

### F7 - MEDIUM - Source/target coverage lags likely enterprise needs

**Evidence**

- Source descriptors support only file, S3, and GCS:
  `src/decoy_engine/config/_sources.py:128-130`.
- DB and SFTP are explicitly deferred:
  `src/decoy_engine/config/_sources.py:22-23`.
- Profile source dispatch supports only file, S3, and GCS:
  `src/decoy_engine/profile/_source.py:133-151`.
- Target descriptors support only file, S3, and GCS:
  `src/decoy_engine/config/_targets.py:95-98`.

**Why it matters**

For a data masking engine, database and SFTP paths are common production
requirements. The current scope may be fine for beta, but it is a clear
capability gap for broader enterprise use.

**Recommended work**

1. Decide whether DB/SFTP belong in engine core, connector packages, or
   platform-only orchestration.
2. If connectors remain external, ensure `PipelineConfig` can represent them
   through a stable connector reference without adding raw secrets.
3. Build contract tests for connector packages against `FileSource`/`FileSink`
   style protocols.

**Acceptance tests**

- Source and target descriptor contract tests for external connector discovery.
- Security tests proving credentials remain opaque in engine configs and errors.

### F8 - LOW - Static tooling still references removed V1 graph modules

**Evidence**

- `src/decoy_engine/graph` is absent.
- `pyproject.toml` still lists `decoy_engine.graph.*` modules in mypy strict
  overrides: `pyproject.toml:400-413`.
- Some docs/security files still reference old graph paths, though this review
  did not assess documentation correctness as a primary scope.

**Why it matters**

Stale tooling config weakens confidence in static gates. If an override targets
modules that no longer exist, the team may believe strict coverage exists where
it does not.

**Recommended work**

1. Remove stale `decoy_engine.graph.*` mypy overrides.
2. Add a sentry test that every strict mypy override module resolves to a real
   module unless explicitly marked as historical.
3. Keep documentation cleanup separate from engine remediation unless stale docs
   are used by active gates.

**Acceptance tests**

- `mypy` override-resolves test.
- CI fails on stale module names in strict coverage allowlists.

### F9 - LOW - Module-size ratchet allows several large hotspots to remain

**Evidence**

- Module-size sentry allowlist includes known oversized files:
  `tests/sentry/test_module_size.py:38-60`.
- Current large files include:
  - `storm/detectors.py` around 1,046 LOC,
  - `quality/synth_report.py` around 863 LOC,
  - `storm/eval/fixtures.py` around 719 LOC,
  - `storm/model_pack/trainer.py` around 694 LOC,
  - `plan/_checks.py` around 691 LOC.

**Why it matters**

The ratchet prevents further growth, which is good. But the largest files sit
in risk-heavy areas: detection, quality reporting, training/evaluation, and plan
checks. Those are exactly where review and change safety matter.

**Recommended work**

1. Convert the allowlist into a decomposition queue with owners and target
   boundaries.
2. Prioritize `plan/_checks.py` because compile checks are central to fail-closed
   safety.
3. Split detector catalogs from detector execution logic in `storm/detectors.py`.

**Acceptance tests**

- Keep the existing LOC ratchet.
- Add package-level public contract tests after decomposition so splits do not
  alter behavior.

## Test Gaps

1. **No end-to-end bounded-memory `run_pipeline()` proof.** Existing
   out-of-core tests prove lower-level streaming behavior; the public pipeline
   still profiles eagerly.
2. **No coverage report available from this environment.** Install coverage in
   the dev extra or add a documented command that always works in the repo venv.
3. **Planner/executor drift is controlled by many targeted tests, but the design
   still depends on duplication.** Add higher-level route-plan golden fixtures.
4. **Public stubs need GA-phase sentries.** Current tests assert stub behavior;
   they should also reject stubs when `RELEASE_PHASE` flips to GA.
5. **Static config staleness needs a sentry.** Mypy overrides and allowlists
   should resolve to real modules/files.

## Suggested Priority Order

1. F1/F2: make profiling and reject-before-read compatible with bounded-memory
   execution.
2. F3: consolidate route planning before more strategy/substrate expansion.
3. F4/F8: clean GA/public-surface gates and stale static-tooling config.
4. F5/F6/F7: publish an executable capability/admission surface for substrates,
   out-of-core, and connectors.
5. F9: decompose large hotspots as touched, starting with plan checks.

## Non-Findings / Positive Signals

- Config validation is strict and centralized through `PipelineConfig`.
- Strategy and route gates generally fail closed rather than producing best-effort
  output.
- The out-of-core compatibility module records explicit rejection reasons.
- The parity harnesses are meaningful and include faithful-rejection checks.
- Public/internal boundary sentries exist, even if the checked boundary is narrow.
- The default high-risk routing tests passed in this review environment.

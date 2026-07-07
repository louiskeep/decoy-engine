# Program: 100M-Row FK Scaling (engine + platform)

Source of goal: Cam directive 2026-07-06 - "my goal with this app is 100M+ rows."
Grounding evidence: `docs/relationships-memory-scaling.md` (memory model + Options 1–5),
`decoy-platform/scripts/gcp-bench/BENCHMARK-QUESTIONS.md` (what's known vs. must-measure),
the parked platform spec `docs/backlog/capability-review-2026-07/sprint-e-execution-metrics-admission.md`.

This is a **program** (a set of sprints), authored for Cam sign-off. Nothing here is built
yet. Build tiers are assigned per the model-tiering directive. Each sprint below is sized to
expand into its own `docs/plans/…-implementation-guide.md` once GATE-1 resolves.

## Why this program exists (the one-paragraph problem)

FK (multi-table, relationship-preserving) jobs are **memory-bound, not CPU-bound**. The
default full-frame path materializes every table full-width plus masked copies plus the FK
parent-key maps (a 10M-key map is ~1–2 GB by itself). Peak RSS is linear and predictable -
`≈ 144 MB + 3.3 MB · (rows_per_table / 1000)` per table (§6 of the memory-scaling doc) - so on
a 32 GB box full-frame FK **OOMs near ~9M rows/table (~27M total)**. RAM is a cliff, not a
slope: past the ceiling there is no "slower," only the OOM-killer. 100M+ is therefore **not a
product limit - it is the limit of one architecture choice** (full-frame in-memory), and the
code that removes it (`feat/option4-out-of-core`, a bounded-batch DuckDB route where nothing is
sized by table cardinality) is **already written but unmerged, unrouted, and narrow**. This
program lands it, routes to it automatically, widens it to realistic recipes, and adds
pre-flight OOM prevention so a too-big job is diverted or rejected in <1s instead of crashing
after 25 minutes.

## Current landed state (verified 2026-07-06, not assumed)

- **On `main`:** Option 2 `run_sequential` + `ParquetTransactionalSink` landed 2026-06-30
  (`execution/_sequential.py`, `_transactional_sink.py`); platform routes FK jobs to it
  (~25–27 % peak-RSS win, ceiling ≈ largest single table + retained key maps). `run_pipeline`
  is still **pandas full-frame with no substrate/planner routing** (hardcodes
  `PandasExecutionAdapter()`); the chunked path is CLI-only, non-FK.
- **`feat/engine-efficiencies`** (7 ahead / **36 behind**): rewrites `run_pipeline` with
  substrate selection (`select_execution_adapter`) + auto-chunk routing (100k-row threshold) +
  a `_planner.py` `classify_job` classifier (has `out_of_core_relationship` as a mode but
  `RELATIONSHIP_ROUTE_DEFERRED`) + P0 perf gates. `PLANNER_ROUTING_ENABLED = False`.
- **`feat/option4-out-of-core`** (25 ahead / **28 behind**): adds the `out_of_core/` package
  (`run_fk_out_of_core`, `_budget`, `_compat`, DuckDB batch join/stage/emit) as a **sibling
  entrypoint** (does NOT touch `run_pipeline`). Branched **before** Option 2 landed, so its
  copies of `_sequential.py`/`_transactional_sink.py` are now redundant with main - the rebase
  keeps only the genuinely-new `out_of_core/` + `_chunked_fk.py` + `_fk_keys.py` + strategy
  batch-kernel edits. Supported strategies today: **only `hash, redact, truncate, passthrough`**.
- **`feat/fk-ri-option1-streaming`** (5 ahead / 28 behind): Option 1, an **alternative** to
  Option 4. Superseded - see GATE-1 #1 (propose drop).

## GATE-1 (Cam) - PROPOSED, awaiting sign-off

Each item is a product decision that changes the sprint set. My recommendation is stated;
Cam confirms or overrides before any build.

1. **Option 4 is THE 100M path; drop Option 1.** DuckDB out-of-core (bounded RAM regardless of
   cardinality) is the only path that completes 100M on 32 GB. `feat/fk-ri-option1-streaming`
   (Option 1) is a narrower self-mask-only variant it supersedes. *Propose: archive Option 1,
   build on Option 4.*
2. **Landing order: efficiencies → option4 → wire.** Land the routing spine
   (`feat/engine-efficiencies`) first (smaller, owns `run_pipeline`/planner), then rebase +
   land `feat/option4-out-of-core` on top (owns the OOC runner), then flip the route. *Propose:
   accept.*
3. **v1 strategy scope for 100M - Group (b) or through Group (c)?** Out-of-core is safe only for
   **value-keyed, row-local** strategies (output depends only on the value + static key, never
   other rows / full column / per-group state / a `when` predicate). Groups (below): (a) shipped
   4; (b) low-risk adds `fpe, text_redact, date_shift, bucketize` + conditional `faker,
   categorical`; (c) needs-proof `text_mask, geo_generalize, code_set, bucket_perturb, formula,
   derived, nested`; (d) impossible-at-scale cross-row. **This is the biggest scope fork** -
   Group (b) = a ~4-sprint critical path to a shippable 100M; Group (c) = ~6 sprints. *Propose:
   land + route + Group (b) for the first shippable 100M (SC0–SC3 + validate), Group (c) as a
   fast-follow (SC4) driven by which real customer recipes need it.*
4. **OOM prevention posture: measure-only default, enforcement opt-in** (inherits Sprint E
   GATE-1: `ADMISSION_CONTROL_ENABLED=false` default; over-soft-threshold → queue-with-visible-
   reason; over-hard-ceiling → reject **before read**; admin override). And: when a too-big FK
   job's recipe is out-of-core-eligible, **prefer reroute-to-streaming over reject.** *Propose:
   accept.*
5. **`when`-predicate recipes don't scale to 100M in v1.** Out-of-core has no row-gating (it
   masks every non-null row unconditionally; admitting a `when` predicate would silently
   over-mask). *Propose: accept for v1, document the limit; row-gating is a later sprint only if
   a customer needs predicated masking at 100M.*
6. **DuckDB ships in the engine.** It is already a **core** dependency (MIT-licensed). The OOC
   route hard-requires it (`out_of_core_backend_unavailable` if absent). *Propose: accept;
   note the footprint in release notes.*
7. **Additive to the compatibility contract.** Out-of-core is a new *route* producing the same
   masked output semantics as full-frame (parity-tested), not a surface change - no frozen-
   surface break pre-GA. *Propose: accept.*

## Sprints

Task-ID prefix **SC** (scale). Engine unless marked platform.

### SC0 - Land the routing spine (rebase `feat/engine-efficiencies`)  ·  tier: **Sonnet** (Opus review)
Rebase onto main (7 ahead / 36 behind). Reconcile the one text conflict (`execution/__init__.py`
export surface) and re-verify the `run_pipeline`/`_substrate`/`_chunked` rewrite against main's
Option-2 changes. Land: `select_execution_adapter` in `run_pipeline`, auto-chunk routing at the
100k threshold, `_planner.py classify_job`, P0 perf gates. Relationship routes stay deferred.
- **AC:** single-table chunk-safe jobs ≥100k auto-route to chunked; `classify_job` classifies FK
  jobs (route deferred, reason recorded); P0 perf gates in CI; all three mains green.

### SC1 - Land the out-of-core FK runner, opt-in (rebase `feat/option4-out-of-core`)  ·  tier: **Opus**
Rebase onto post-SC0 main (25 ahead / 28 behind). **Drop** the branch's now-redundant
`_sequential.py`/`_transactional_sink.py` (main has them); keep `out_of_core/` + `_chunked_fk.py`
+ `_fk_keys.py` + strategy batch-kernel edits. Reconcile `_strategies/_truncate.py`. Land
`run_fk_out_of_core` as an **opt-in sibling** (not yet auto-routed), DuckDB-capability-gated,
with `_budget` (25 % host-RAM DuckDB `memory_limit` + `batch_rows`, temp-disk fail-closed).
- **AC:** the capability sentinel passes on main - full **and** sequential OOM under a hard
  1024 MB cap at 400k×16 while out_of_core **completes with FK parity**; opt-in invocation
  documented; supported strategy set = the initial 4.

### SC2 - Wire auto-routing to out-of-core  ·  tier: **Opus** (routing correctness is high-stakes)
Flip `RELATIONSHIP_ROUTE_DEFERRED`: `classify_job` routes an eligible FK job to
`run_fk_out_of_core` when (size ≥ threshold) ∧ (all strategies in supported set) ∧ (no `when`
predicate) ∧ (single-parent-per-child). Dispatch the sibling entrypoint from `run_pipeline` /
the job runner (the missing wire - option4 never touched `run_pipeline`). Ineligible-but-large
jobs fall back to sequential if it fits, else surface an actionable "cannot stream this recipe:
<reason>" - **never a silent OOM.**
- **AC:** a 50M-total FK job with `hash` keys auto-selects out_of_core and completes on a 32 GB
  box; an unsupported-strategy job is rerouted or rejected with a specific reason before read;
  the routing decision + rejected-mode reasons land in `ExecutionResult.quality_metrics`.

### SC3 - Widen out-of-core: Group (b), low-risk  ·  tier: **Sonnet** (Opus review)
Add to `_INITIAL_SUPPORTED_STRATEGIES` the strategies already proven chunk-safe on main
(`CHUNK_SAFE_STRATEGIES`): **`fpe`, `text_redact`, `date_shift`, `bucketize`**. Add **`faker`,
`categorical` conditionally** under the exact deterministic-value-keyed guards the chunked path
already encodes (`deterministic: true` + `namespace` + explicit `pool_size`/`categories`). Each
strategy = a batch-kernel + a byte-parity test vs full-frame on the value-keyed columns.
- **AC:** every Group-(b) strategy runs through out_of_core with byte-parity vs full-frame and
  FK parity intact; the `_compat` gate admits them and still rejects everything in (c)/(d).

### SC4 - Widen out-of-core: Group (c), needs-proof  ·  tier: **Opus** (fast-follow; see GATE-1 #3)
Vet and admit where provably row-local: `text_mask, geo_generalize, code_set, bucket_perturb`,
and the same-row-column-set family (`formula, derived, nested`) **only if all referenced columns
travel in the same batch** (batch-locality analysis required per strategy). Explicitly reject
what can't be proven this sprint, with a reason string in `_compat`.
- **AC:** each admitted strategy carries a chunk-invariance proof + parity test; each deferred
  one has a documented `_compat` rejection reason; no over-masking regression.

### SC5 - Prevention: promote platform Sprint E (estimator + admission gate)  ·  platform · tier: **Sonnet** (Opus review)
Promote the parked `sprint-e/execution-metrics`. **E1:** additive/nullable `Job` metrics
(`queue_wait_ms, run_duration_ms, input_size_bytes, output_size_bytes` + `peak_memory_mb`
caveat). **E2:** a quantitative peak-MB estimator (from input size / rows×width, the
`classify_inmemory_ceiling` structural signal, and the strategy set) compared to the box budget
- **measure-only by default** (`ADMISSION_CONTROL_ENABLED=false`); when enabled, over-soft →
queue-with-reason, over-hard → reject **before read**, admin override. **Integrate with SC2:** a
too-big FK job whose recipe is OOC-eligible is **rerouted to streaming, not rejected.** Calibrate
all numbers from SC6 - never hardcode a marketing number.
- **AC (from Sprint E spec):** an oversized job's admission decision (estimate + threshold +
  would-reject reason) is **recorded before the read-into-memory step**; with control enabled the
  job is queued-with-reason or hard-rejected before read, with an actionable message, **never a
  raw OOM/MemoryError**; product docs cite a **real measured** number (provisional pending PO
  sign-off); concurrent-submission benchmark cell green.

### SC6 - Validate 100M on GCP (32 GB)  ·  tier: **Sonnet** (run) + **Opus** (interpret)
Use the already-built `decoy-platform/scripts/gcp-bench/engine-bench.sh`. Run **early** after SC1
(opt-in) to get real 50M/100M numbers that calibrate SC5, then again post-SC2 (auto-routed).
Tiers: full-frame + sequential sweeps to the OOM ceiling; out_of_core at 50M and **100M** total
(`--fifty-m-total 100000000`); concurrency + forced-OOM signature. Capture wall-clock, peak RSS,
temp-disk; commit under `docs/product/release-1-validation-runs/`.
- **AC:** a 100M-total FK job **completes bounded** on 32 GB with FK parity; peak RSS stays ~flat
  (≤ budget) across 50M→100M (proving cardinality-independence); the committed run backs the
  "100M+ on 32 GB" claim; SC5's estimate is within a stated tolerance of the measured peak.

## Dependency graph / sequencing

```
SC0 ──▶ SC1 ──▶ SC2 ──▶ (shippable 100M with Group-b via SC3)
                 │
        SC3 (after SC1; parity-testable without the wire) ──▶ SC4 (fast-follow)
        SC6 early baseline (after SC1, opt-in) ──┐
                 │                               ├──▶ SC5 (measure-only build any time; calibrate from SC6)
        SC6 full (after SC2, auto-routed) ───────┘
```
Critical path to a **shippable, auto-routed, OOM-safe 100M with realistic-enough recipes:**
**SC0 → SC1 → SC2 → SC3 → SC6**, with SC5 built in parallel (measure-only) and calibrated from
SC6's early baseline. SC4 is the GA-completeness fast-follow gated by real customer recipes.

## Non-goals (explicit)

- **Multi-worker / concurrent jobs.** Concurrency stays 1 (fine for self-hosted single-tenant
  batch v1); the parallel-jobs lift is platform Sprint H, not this program.
- **Cross-row strategies at scale - Group (d):** `shuffle` (full-column permutation),
  `joint_mask`, `grouped_series`, `windowed_date`, `derived_aggregate`, `group_key`. These keep
  the full-frame ceiling; recipes using them at >~9M rows/table are out of scope until a bounded
  relational lowering is designed.
- **Row-gating (`when`) in out-of-core** (GATE-1 #5).
- **True per-job memory-delta accounting** (Sprint H; SC5 keeps process-wide `ru_maxrss` + caveat).
- **Flat single-table 100M** is already handled by the chunked path; if a flat strategy sweep is
  wanted, restore `benchmark_canonical.py` (not on main) - tracked as a `BENCHMARK-QUESTIONS.md`
  gap, not a sprint here.

## Process (per sprint, house standard)

DEVELOP → SELF-CHECK (CI-gate mirror: `ruff format --check`, `sphinx -W`, no-extras env,
`pytest -m "not perf"`) → adversarial REVIEW (dennis) → REMEDIATE → docs (barry) → CI-gate →
GATE-2 (Cam) → merge. Perf-sensitive sprints (SC1/SC2/SC6) additionally run `pytest -m perf`
and attach the out-of-core sentinel + a scale record to the PR.

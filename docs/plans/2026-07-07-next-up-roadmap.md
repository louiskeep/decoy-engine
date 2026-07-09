# Next-Up Development Roadmap

**Written:** 2026-07-07. **Purpose:** the ordered list of what we complete next
when we return to development. Captures the in-flight 100M-row scaling sprints
(SC1-SC6) at their current status, then four cross-repo follow-on items Cam
named for after the scaling program.

This is a planning/tracking doc, not an implementation guide. Each sprint below
already has (or will get) its own `docs/plans/…-implementation-guide.md`. The
canonical detail for the scaling sprints lives in
`docs/plans/2026-07-06-100m-row-scaling-program.md` (PR #33).

Related roadmaps: `docs/job-performance-sprints.md` (P0-P10 / R1-R3 runtime
performance), `docs/relationships-memory-scaling.md` (the memory model this
program removes the ceiling from).

---

## Part A - Current sprints: 100M-row FK scaling program (SC0-SC6)

The goal: FK (multi-table, relationship-preserving) jobs to 100M+ rows on a
32 GB box. Full-frame FK is memory-bound and OOMs near ~9M rows/table
(~27M total); the out-of-core DuckDB route (bounded RAM regardless of
cardinality) is the only path that completes 100M. This program lands that
route, auto-routes to it, widens its strategy surface, and adds pre-flight OOM
prevention. Full spec + GATE-1 decisions: the 100M program doc (PR #33).

| Sprint | What | Status |
|--------|------|--------|
| **SC0** | Land the routing spine (`run_pipeline` substrate selection + auto-chunk + `_planner.classify_job` + P0 perf gates) | **DONE** - landed via engine PR #31 (engine main 0.3.0) |
| **SC1** | Land the out-of-core FK runner as an **opt-in, unwired sibling** (`run_fk_out_of_core`, DuckDB-gated, budgeted); initial strategy set `hash/redact/truncate/passthrough`; parity harness | **DONE** - merged 2026-07-09 via engine PR #34 (fail-closed hardening pass, dennis APPROVE 0 BLOCKER/0 HIGH). See "SC1 status" below. |
| **SC2** | Wire auto-routing: the live router selects out-of-core for eligible large FK jobs; ineligible-but-large jobs reroute-to-sequential or reject-before-read with a reason (never silent OOM) | **DONE** - part 1 (CF1/CF2/CF3 hardening) merged via PR #37; part 2 (the auto-routing wire) merged 2026-07-09 via PR #38 (dennis re-review APPROVE, 0 BLOCKER/0 HIGH). See "SC2 status" below. |
| **SC3** | Widen out-of-core to Group (b) strategies (`fpe`, `text_redact`, `date_shift`, `bucketize` + conditional `faker`/`categorical`), each with byte-parity vs full-frame | **QUEUED** |
| **SC4** | Widen out-of-core to Group (c) strategies (`text_mask`, `geo_generalize`, `code_set`, `bucket_perturb`, `formula`/`derived`/`nested` where batch-local) - in v1 critical path per Cam | **QUEUED** |
| **SC5** | Platform Sprint E: peak-MB estimator + admission gate (measure-only default; over-hard-ceiling reject before read; reroute OOC-eligible jobs to streaming). OOM **prevention**. | **QUEUED** (platform) |
| **SC6** | Validate 100M on GCP (32 GB) with the built `scripts/gcp-bench/engine-bench.sh` battery; commit the run that backs the "100M+ on 32 GB" claim | **QUEUED** - needs gcloud auth + spend confirmation. Overlaps Part B item 1. |

**Critical path to a shippable, auto-routed, OOM-safe 100M:**
SC0 → SC1 → SC2 → SC3 → SC6, with SC5 built in parallel (measure-only) and
calibrated from SC6's early baseline. SC4 is the GA-completeness item Cam put in
the v1 critical path.

### SC1 status - DONE (merged 2026-07-09, PR #34)

The prove-or-reject fail-closed hardening pass Cam specced on 2026-07-07 landed
as 3 independently-reviewable commits: (1) `_mask.py` truncate raw-leak backstop
- invalid config now raises the oracle's `StrategyError` codes instead of
falling back to `passthrough_array`; (2) `_batch_join.py`/`_join.py` int/float
FK-key sink-parity - a representability-guarded round-trip cast replaces the
unsafe-cast crash, failing closed with `out_of_core_fk_key_dtype_unsupported`
when a key can't survive the cast losslessly; (3) `_compat.py` - the audit this
pass required turned up a bonus gap (multi-table FK-cycle admission that
crashed `_table_order` uncoded), now gated fail-closed pre-runner.

dennis adversarial review: **APPROVE, 0 BLOCKER / 0 HIGH / 1 MEDIUM / 2 LOW.**
Full mirror gates green (ruff, format, mypy, regression-gate 5680 passed/36
skipped/1 pre-existing unrelated env failure, parity harness, memory sentinel).
The MEDIUM did not block the SC1 merge (fails closed, opt-in/unwired route,
zero live blast radius today) but **is a hard precondition for SC2** - see CF3
below.

**Carry-forwards tracked into SC2 (must be resolved before the route is wired):**
- **CF1** - chunked-FK self-masking gate reachability: re-review before relaxing
  `_planner._chunked_rejection` or the platform gate.
  **RE-CONFIRMED (SC2 part 1, 2026-07-09):** still holds. `git show --stat`
  confirms SC1 (`c14aa7e`) did not touch `_planner.py`, so the engine guard
  (`_chunked_rejection`'s `if config.get("relationships")` short-circuit, which
  skips `check_chunked_compatibility` entirely) is unchanged. The relaxed
  FK-child admission remains reachable only by a direct caller of
  `check_chunked_compatibility` / `run_mask_pipeline_chunked` with an FK config,
  of which none exists in engine or platform. No code change. Pinned by
  `test_execution_planner.py::...::test_fk_relationships_keep_chunked_self_masking_gate_unreachable`
  so a future SC2/SC3 relaxation cannot silently make it reachable. (The second
  guard, platform `plan_streaming_route`, lives in decoy-platform and is
  unchanged by SC1; its re-review is recorded here for the cross-repo record.)
- **CF2** - `out_of_core/_batch_join.py` composite partial-null orphan parity
  divergence (documented, module docstring lines ~40-47): gate or explicitly
  accept before SC2 wires the route.
  **RESOLVED → GATED (SC2 part 1, 2026-07-09):** the docstring divergence does
  not reproduce in the canonical `composite_fk_group` shape (proven oracle-parity
  by a 400-trial fuzz + pinned tests across orphans/partial-nulls/all policies).
  It only reproduces when composite-FK child columns are masked as independent
  `scalar` nodes (double-masking): the oracle keeps the scalar-masked value on
  orphans while out-of-core preserved the **raw** value - a raw-value leak.
  Orphans can't be excluded statically, so per the fail-closed default that
  shape is now gated (`out_of_core_composite_fk_scalar_child_unsupported`); the
  parity-proven group shape and single-column scalar children stay admitted.
- **CF3** (dennis SC1 review) - intra-batch int-beyond-2^53 + float FK output
  crashed with an **uncoded** `ArrowInvalid` at `_join.py:304`
  (`_append_output_batch`). The gate admits float-parent/int-child FK edges with
  no dtype check (`_compat.py` `_check_edge`); a batch mixing a matched float
  value with an orphan int key beyond float-precision hit `pa.array(...,
  from_pandas=True)` before `cast_fk_chunk` (CF3's sibling fix from SC1) was
  ever reached. Production-reachable on a gate-admitted config, not just a
  parity-harness gap.
  **RESOLVED (SC2 part 1, 2026-07-09):** chose option (b) - guarded
  `_append_output_batch` the same way `cast_fk_chunk` guards the cross-batch
  cast (the compat gate never sees source dtypes/value ranges, so it can't
  reject this at admission time), raising the coded
  `out_of_core_fk_key_dtype_unsupported` fail-closed rejection instead of the
  raw crash. The pandas oracle's silent rounding on this same shape
  (`9007199254740993` → `9007199254740992.0`, not authoritative) is recorded in
  `tests/parity/SEMANTIC_DIFFERENCES.md`.

All three carry-forwards resolved on PR #37 (branch `sc2/carryforward-hardening`,
open); dennis review pending before merge and before the actual auto-routing
wiring (SC2 part 2) begins.

### SC2 status - DONE (part 1 PR #37 merged; part 2 PR #38 merged 2026-07-09)

**Part 1** (PR #37, merged): the CF1/CF2/CF3 fail-closed hardening above.

**Part 2** (PR #38, merged 2026-07-09): the actual auto-routing wire.

- **Reconciliation of the two routing mechanisms.** The repo had two: the
  planner's `classify_job` (whose `out_of_core_relationship` mode only ever
  recorded `RELATIONSHIP_ROUTE_DEFERRED` and fell through to `pandas_fallback`)
  and `_pipeline_routing.decide_execution_route` (the S2 mechanism that ALREADY
  decides sequential-vs-full_frame for pure-mask FK jobs and is the live surface
  `run_pipeline` early-returns on). Investigation confirmed `decide_execution_route`
  is the **sole live router** for FK jobs -- `classify_job`'s FK branch never
  routes. Decision: **extend `decide_execution_route`** with a third
  `out_of_core` route + a fail-closed reject-before-read; leave `classify_job`
  as the static EXPLAIN/chunked classifier but update its now-stale "FK stack on
  another branch" disposition to point at the live router (`PLANNER_ROUTING_ENABLED`
  stays `False`). Activating the planner's dormant FK branch as a second router
  was rejected: it would duplicate the live decision and drift.
- **Priority order** (`execution_mode="auto"`, pure-mask FK): **out_of_core**
  (`check_out_of_core_compatibility` admits AND largest mask table
  >= `out_of_core_threshold_rows`) > **sequential** (bounded but O(cardinality))
  > **full_frame**. A large relationship job that no bounded route can take
  (not out-of-core-eligible, disqualified from sequential) is **rejected before
  the mask step** with coded `ExecutionError` `fk_full_frame_oom_risk_rejected`,
  never left to a silent full-frame OOM. `execution_mode="out_of_core"` added as
  an explicit fail-closed force (mirrors `"sequential"`); `"full_frame"` still
  forces full-frame and bypasses the reject (operator escape hatch).
- **Thresholds** (per largest mask table, in `_planner`, kwarg-overridable for
  the SC5 estimator): `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT = 5_000_000`,
  `FULL_FRAME_REJECT_ROWS_DEFAULT = 7_500_000`. Anchored to the documented
  full-frame FK memory model (`docs/relationships-memory-scaling.md` §6:
  `peak_RSS ~= 144 MB + 3.3 MB * rows/1000` for a 3-table width-16 hash chain,
  OOM near ~9M rows/table / ~30 GB on 32 GB). 5M projects to ~16.6 GB (half the
  32 GB box, past the measured 250k-1M out-of-core overhead-regression zone);
  7.5M to ~24.9 GB (~78%, the danger zone below the ~9M cliff with margin for
  wider payloads). Conservative interim constants; the memory-scaling doc is
  explicit that precise box+schema-calibrated MB prediction is SC5's job.
- **Strategy surface unchanged** (SC1's `hash/redact/truncate/passthrough`): an
  unsupported strategy is a routing MISS (stays sequential/full_frame), never a
  run failure. Widening is SC3/SC4.
- **Verification:** ruff / ruff format / mypy clean; full `tests/unit` +
  `tests/parity` green (new: `test_out_of_core_routing.py`,
  `test_out_of_core_routing_parity.py` proving eligible-large routes to
  out-of-core with byte-parity vs the oracle, ineligible-large rejects before
  read, ineligible-small unchanged); memory sentinel unchanged.
- **Module-size BLOCKER (dennis first pass) - RESOLVED.** `_pipeline.py` and
  `_pipeline_routing.py` both breached the ~600 LOC orchestration cap on the
  first fix attempt (653 / 773 LOC). Fixed via genuine decomposition, not an
  allowlist: `_pipeline_route_exec.py` (route executors, 324 LOC) and
  `_pipeline_finalize.py` (post-mask finalize: reproducibility stamps, BF1
  fidelity report, D8 validator/quarantine, 210 LOC) split out. Final sizes
  all ≤600, dennis re-review confirmed the split is a clean separation of
  concerns (decision logic vs execution vs finalize), not a mechanical dodge.

**Carry-forward into SC3 (tracked, not a blocker):**
- **M1** - the `source_loader` lazy-load branch in
  `_pipeline_route_exec.py` (~line 212, reached only via the
  `execution_mode="out_of_core"` forced escape hatch when sources are not yet
  resident) is dennis-verified correct - it resolves the same table set the
  sequential path loads (`table_topo_order(plan, graph)`), the static compat
  gate can't be invalidated by lazy-vs-resident sources, and a loader
  exception fails closed before any sink opens - but **no test exercises it**;
  every current forced-OOC test passes fully-resident sources. Fix: one test
  driving `run_pipeline(..., sources={}, execution_mode="out_of_core",
  source_loader=<fixture loader>)` asserting the loader is invoked for the
  full `table_topo_order` set and reaches byte-parity with the resident
  oracle. Do this before or alongside SC3 so the branch isn't shipped
  untested indefinitely.

### Deliverables already shipped alongside this program
- **PR #33** - the 100M program doc (`docs/plans/2026-07-06-100m-row-scaling-program.md`).
- **PR #19 (platform)** - the GCP benchmark harness (`scripts/gcp-bench/`:
  `engine-bench.sh`, `remote-engine-bench.sh`, `BENCHMARK-QUESTIONS.md`). Ready
  to run once auth + spend are approved (this is Part B item 1's tooling).

---

## Part B - Next to complete once we return (Cam, 2026-07-07)

Four cross-repo items to complete after the scaling program. Ordered as listed.

### 1. GCP benchmark + testing
Run the real scale/memory/OOM battery on GCP and record the numbers. Tooling is
already built (platform PR #19 `scripts/gcp-bench/engine-bench.sh` - spins up a
32 GB `n2-standard-8`, ships the engine as a git bundle, runs the
`fk_memory_probe` battery B1-B5, tears down). This **is** SC6 plus the broader
battery that answers the original benchmark questions (largest job, concurrency,
OOM signature, 50M/100M wall-clock).
- **Blocked on run, not build:** no active gcloud account (`gcloud auth login`),
  a project, `--confirm-cost`, and the base image (`build-base-image.sh`).
  Building is free; running costs money. Benchmark against a **named ref**
  (post-SC1 branch, then post-SC2 main), not "the repo".
- **Depends on:** SC1 merged (for the opt-in OOC numbers), ideally SC2 (for the
  auto-routed numbers). Feeds SC5's estimator calibration.

### 2. Wire up UI to engine
Wire the platform Job Studio UI end-to-end against the now-complete backend
(the finish-open-ended program S1-S10 completed the half-built surfaces the UI
needs). Known gap to close: the fixed-width authoring fork (pipeline builder
still authors fixed-width via the legacy `FwDefinition`, so a UI pipeline-source
can't yet reference a saved `FixedWidthLayout` - API+engine layer works + tested,
UI picker deferred). See `decoy-platform` and the UI-redesign notes (function-box
"Job Studio" wizard).
- **Repo:** decoy-platform (+ decoy-web for the front end).

### 3. CLI testing
Build out test coverage for the decoy CLI (`docs/cli.md` surface). Verify the
CLI paths - chunked single-table route, config validation, typed-error behavior
- hold under the post-scaling engine. Establish the CLI as a first-class tested
surface, not just an incidental entrypoint.
- **Repo:** decoy CLI / engine.

### 4. Web UI/UX testing + remapping
Test and remap the decoy-web UI/UX: audit the current flows against the completed
backend + new Job Studio model, fix UX gaps, and remap navigation/IA where the
product has moved (e.g. surfaces added by S8-S10, the /proof page, quality
panel). Includes visual/interaction QA.
- **Repo:** decoy-web.

---

## Resume checklist

1. ~~**SC1** - execute the prove-or-reject hardening pass on PR #34~~ **DONE**,
   merged 2026-07-09.
2. ~~**SC2** - resolve CF1 + CF2 + CF3, wire auto-routing.~~ **DONE** (part 1
   PR #37; part 2 PR #38, both merged). M1 (untested lazy-loader branch)
   carried forward, see "SC2 status" above.
3. **SC3 → SC4** - widen the strategy surface (parity-tested). Fold in the
   M1 test carry-forward.
4. **SC5** (parallel) - platform estimator + admission gate, measure-only.
5. **SC6 / Part B item 1** - GCP 100M benchmark run (needs auth + spend).
6. **Part B items 2-4** - UI-to-engine wiring, CLI testing, web UI/UX.

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
| **SC1** | Land the out-of-core FK runner as an **opt-in, unwired sibling** (`run_fk_out_of_core`, DuckDB-gated, budgeted); initial strategy set `hash/redact/truncate/passthrough`; parity harness | **IN REVIEW** - build complete, PR #34, CI all-green. See "SC1 status" below. |
| **SC2** | Wire auto-routing: `classify_job` selects out-of-core for eligible large FK jobs; ineligible-but-large jobs reroute-to-sequential or reject-before-read with a reason (never silent OOM) | **QUEUED** - blocked on SC1 merge + carry-forwards CF1/CF2 |
| **SC3** | Widen out-of-core to Group (b) strategies (`fpe`, `text_redact`, `date_shift`, `bucketize` + conditional `faker`/`categorical`), each with byte-parity vs full-frame | **QUEUED** |
| **SC4** | Widen out-of-core to Group (c) strategies (`text_mask`, `geo_generalize`, `code_set`, `bucket_perturb`, `formula`/`derived`/`nested` where batch-local) - in v1 critical path per Cam | **QUEUED** |
| **SC5** | Platform Sprint E: peak-MB estimator + admission gate (measure-only default; over-hard-ceiling reject before read; reroute OOC-eligible jobs to streaming). OOM **prevention**. | **QUEUED** (platform) |
| **SC6** | Validate 100M on GCP (32 GB) with the built `scripts/gcp-bench/engine-bench.sh` battery; commit the run that backs the "100M+ on 32 GB" claim | **QUEUED** - needs gcloud auth + spend confirmation. Overlaps Part B item 1. |

**Critical path to a shippable, auto-routed, OOM-safe 100M:**
SC0 → SC1 → SC2 → SC3 → SC6, with SC5 built in parallel (measure-only) and
calibrated from SC6's early baseline. SC4 is the GA-completeness item Cam put in
the v1 critical path.

### SC1 status (the current sprint)

Built and on PR #34 (CI all-green: build, mypy, ruff, regression-gate,
strategy_parity, substrate pandas+polars, compat-preflight, pip-audit). The
route is **opt-in and unwired** - nothing auto-routes to it until SC2, so every
finding below has **zero live blast radius** today.

Six adversarial Codex rounds progressively hardened the route. The route is
"permissive-by-design" (admit broad, fall back permissively), which keeps
surfacing fail-open / oracle-divergence spots. Round 6 found one **[P1]** raw-leak
backstop (`out_of_core/_mask.py:70` - an invalid `truncate` config falls back to
`passthrough_array` and publishes the raw column/FK key instead of raising like
the pandas oracle's `truncate_*_invalid`) plus one **[P2]** int/float sink-parity
gap.

**Decision (Cam, 2026-07-07): converge SC1 with a prove-or-reject hardening
pass** before merge - make the route **fail-closed by construction**: no
passthrough/permissive fallbacks anywhere (invalid config raises exactly like the
oracle), and the `_compat` gate admits **only** the dtype/topology/strategy
surface the parity harness proves, rejecting everything else with a reason
string. Goal: "admitted ⟹ oracle-parity" becomes a theorem, which is what ends
the whack-a-mole. This is the next action when we resume.

**Carry-forwards tracked into SC2 (must be resolved before the route is wired):**
- **CF1** - chunked-FK self-masking gate reachability: re-review before relaxing
  `_planner._chunked_rejection` or the platform gate.
- **CF2** - `out_of_core/_batch_join.py` composite partial-null orphan parity
  divergence: gate or explicitly accept before SC2 wires the route.

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

1. **SC1** - execute the prove-or-reject hardening pass on PR #34 (fix the R6
   [P1] `_mask.py:70` raw-leak backstop first - non-negotiable), re-run Codex to
   confirm convergence, dennis gate, then merge.
2. **SC2** - resolve CF1 + CF2, wire auto-routing.
3. **SC3 → SC4** - widen the strategy surface (parity-tested).
4. **SC5** (parallel) - platform estimator + admission gate, measure-only.
5. **SC6 / Part B item 1** - GCP 100M benchmark run (needs auth + spend).
6. **Part B items 2-4** - UI-to-engine wiring, CLI testing, web UI/UX.

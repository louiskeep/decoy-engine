# B-10 job-time estimator (engine primitive): completion plan for a remote worker

Status: plan
Feature: F24 (full-lifecycle capacity estimation), advisory, ENGINE primitive only.
Tier: R2 (new public engine API surface + a perf-shaped claim). Plan gate chain: Dennis -> Codex.
Branch to continue: `claude/job-time-estimator` (engine, built Slices 1+2).
Author: orchestrator (local). Executor: remote claude.ai/code worker.
Created: 2026-10-28.

Rules consulted: `~/dev-rules/00-universal.md`, `~/dev-rules/development-loop.md`,
`~/dev-rules/risk-and-exceptions.md`, `~/dev-rules/performance.md`,
`~/dev-rules/testing.md`, `~/dev-rules/feature-dev.md`,
`~/dev-rules/api-and-compatibility.md`; engine `CLAUDE.md` established-methodology.

## 1. Problem and definition of done

The public engine `estimate_job` primitive (advisory job time + peak bytes from
schema + a bounded sample, not a full-table read) is BUILT on the branch (Slices 1+2) and
self-reports green, but has had no gate and is 88 commits behind main. This plan takes
the ENGINE PRIMITIVE to merge-ready. The CLI surface (Slice 3) and the platform advisory
surface (Slice 4 / F24 Phase 2) are explicit follow-ons, OUT of scope here.

**Definition of done.** Rebased onto current engine main, the estimator units +
throughput-calibration units + public-API `__all__` test + the physical-seam sentry pass,
ruff + mypy clean, PR opened, Dennis -> Codex green.

## 2. Current state (built on the branch, do NOT rebuild)

- `src/decoy_engine/execution/_throughput_calibration.py` (runtime model, `RuntimeBand`)
  and `src/decoy_engine/execution/_job_estimate.py` (`estimate_job`, `JobEstimate`).
- Public export of `estimate_job` + `JobEstimate`; the physical-seam sentry allowlists the
  two new modules; `tests/unit/execution/test_job_estimate.py` +
  `test_throughput_calibration.py`.
- `estimate_job(config_dump, base_dir) -> JobEstimate`: composes route
  (`decide_execution_route`, promotion flags OFF), `peak_bytes` (`estimate_peak_bytes`),
  and a `RuntimeBand` (bytes/route throughput anchors; wide confidence band).

## 3. Established solutions (surveyed)

Reuses existing engine primitives (route decision, peak-bytes estimate, calibration
anchors). No new crypto/statistical primitive. Calibration anchors come from recorded
benches (native/streaming mask directly measured = `medium` confidence; out_of_core /
full_frame / sequential conservative, un-benched = `low` confidence with a wider band).
Honest and cited, not a new benchmark campaign (`performance.md`: scope the microbench,
report the correctness guard + variance). A dedicated per-route throughput bench is a
named follow-up, not this slice.

## 4. Remaining work to merge-ready

1. **Rebase onto current engine main** (~89 commits behind, rebase onto the current
   tip). `git merge-tree` shows 0 conflict markers and the shared-file edits are
   additive-only (`__all__` / allowlist appends), but that proves a MERGE is clean, not
   the REBASE the plan prescribes, so confirm the rebase actually applies cleanly before
   relying on it. The one shared file with churn is
   `tests/sentry/test_physical_seam_disconnection.py` (its allowlist append for the two
   new modules must survive the reconciliation; the new modules are new files, exempt by
   being allowlisted). Confirm every imported private helper still exists with a
   compatible signature on main (route/peak/calibration/seed helpers).
2. **Fix the vacuous generate-path test (MED).** `test_generate_job_gets_a_rows_based_runtime_band`
   declares its table with `"columns": [...]`, so `classify_table_kinds` treats it as a
   MASK table (it returns "generate" ONLY when `generate_columns` is present), the config
   routes down the mask path, and the test's real assertion sits behind an
   `if est.priceable and est.runtime is not None:` body that never runs: the
   `has_generate and not has_mask` branch (the `_generation_rows` +
   `estimate_generation_runtime_band` path) has NO end-to-end coverage. Fix the fixture to a
   VALID generate config: build via `PipelineConfig.model_validate(raw).model_dump()`, drop
   the table `mode`, use `generate_columns` with a simple valid column
   (`{"name": "x", "type": "sequence"}`, not `strategy`/`provider`) so `classify_table_kinds`
   (truthy `generate_columns`) selects the generate branch with `priceable=True`. Assert
   UNCONDITIONALLY (no `if est.priceable` guard): priceable True, runtime present, positive
   ordered bounds, a generation basis, comparing TWO substantial row counts so a constant or
   mask-based band cannot pass.
3. **Forward `run_storm` to the router (MED).** main's `decide_execution_route` accepts it and
   `capacity.py` forwards it, but `estimate_job` does not, so a `run_storm=True` FK job could
   be reported sequential/out-of-core though storm disqualifies those. Forward the flag + test
   it. The reported route is a COARSE layer-1 prediction (omits later chunk/native dispatch +
   mixed admission); say so.
4. **Mixed jobs (MED).** `estimate_job` prices generation only for generate-ONLY jobs, so a
   mixed fixed-width-mask + generate job returns `priceable=True` pricing only mask bytes and
   silently omits generated volume. For this slice mark a mixed job `priceable=False` with a
   reason (no silent under-price); add a mixed-job acceptance case. Document that generate-only
   results intentionally have `priceable=True` with `peak_bytes=None` (state it in §6).
5. Add a router-REJECTION acceptance case: a config that makes `decide_execution_route`
   raise its reject-before-read returns `route="unknown"`, `priceable=False`, a reason, with
   both promotion flags off and the input config unchanged. Also pin the bounded-read
   behavior (never reads the whole table) per the §6 guarantee correction.
6. Rebuild is pure-Python (no Rust companion needed for the estimator). Re-run the
   estimator + calibration units, the public-API `__all__` test, and the physical-seam
   sentry; `ruff check` + `ruff format --check` + `mypy` on the changed units.
7. Open a PR (none exists); do NOT merge or fabricate a receipt. The orchestrator runs the
   gate (Dennis -> fixes -> Barry docs reconcile -> exact-HEAD receipt -> Codex) and merges
   on green. Barry reconciles the public-API docs + the older plan's stale status.

## 5. Observable behavior, failure modes, acceptance

- **Advisory-only (the pinned guarantee):** `estimate_job` NEVER raises (wraps compute in
  try/except, returns `JobEstimate(route="unknown", priceable=False, notes=(...))` on any
  failure), NEVER gates/admits, NEVER changes routing (router runs read-only with
  promotion flags off). An unpriceable column (e.g. variable-width with no sample) returns
  `priceable=False` with a reason, route still reported.
- **Determinism:** profiling seed derived from the job config; no wall-clock input; route
  and peak are deterministic functions of schema + profile.
- **Honest accuracy:** a ROUGH wide-band advisory, never a point estimate; `medium`
  confidence only for the two directly-measured routes, `low` (wider band, host-caveated)
  for the rest. No hard accuracy bound is promised, and the plan says so.
- Acceptance: the existing estimator/calibration tests (anchors round-trip within their
  band; band monotonic in work, ordered, floored; never raises on unknown route / zero
  work; unpriceable is honest not a raise) pass on the rebased tree; measure test strength
  on the two changed units.

## 6. Pinned guarantee, non-goals, and the API-shape note

**Guarantee.** A deterministic, wide-band, NON-gating time + peak-bytes advisory computed
from schema + a BOUNDED SAMPLE, not a full-table read and not metadata-only: `estimate_job`
calls `profile_source`, which in bounded mode reads up to ~10k source rows (Parquet consumes
a record batch). The guarantee is "no FULL read", the plan must NOT claim metadata-only, and
an acceptance test should pin the bounded-read behavior (it never reads the whole table). It
never raises and
never alters execution.

**Non-goals.** Not a gate or admission control (advisory per the capacity spec); not a
per-operator exact-ms model; not a new benchmark campaign; the CLI `preflight` surface
(Slice 3) and the platform advisory surface (Slice 4 / F24 Phase 2) are deferred. When
those land they consume this primitive and will need an engine version bump + pin at that
time, NOT in this merge.

**API-shape note (for the reviewer and future consumers).** The built `JobEstimate` shape
intentionally diverges from the plan's original headline signature: no `budget_bytes`
argument; `priceable: bool` instead of `unpriceable`; `notes` instead of `warnings`;
`confidence` nested inside `RuntimeBand` rather than a top-level `runtime_seconds` +
`confidence`. This is the agreed shape; any later CLI/platform wiring codes to it.

## 7. Remote-worker scope (phase 0) and hand-back

Phase 0 = rebase + re-run the engine-primitive tests + ruff/mypy + open PR, on
`claude/job-time-estimator`. Engine-only, no cross-repo coupling, no pin change. Push the
branch and open a PR against engine `main`; do NOT merge. The orchestrator runs the
code gate (Dennis -> Codex) and merges on green.

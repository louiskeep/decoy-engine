Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification

# Phase F slice F4: generate + mixed-route capacity validation (1M/10M/100M)

Per the Phase F program plan (F4: 1M/10M/100M capacity) and Cam's recommended scope
(100M capped; 200M deferred; GCP budget pre-authorized). F0/F1*/F2/F3* context:
masking-at-scale is already proven (Phase E: 100M out-of-core bounded at 8 GiB).
F4's NEW surface is capacity of the FULL pipeline that INCLUDES generation + the
shipped F2 mixed route (generation resident + mask on the bounded Rust routes).
(*F1/F3 held pending Cam's re-baseline; F4 validates the CURRENTLY-shipped pipeline.)

## 1. Problem + goal

There is no capacity/bounded-memory validation for generation or generate+mask at
scale (confirmed: scripts/fk-scale + fk_memory_probe + bench-* are masking-only or
throughput-only). Goal: validate that a mixed generate+mask job (the F2 production
route) runs at 1M/10M/100M TOTAL rows with BOUNDED memory, CORRECT output
(byte-identical to the full-frame mixed oracle + FK parity + committed row counts),
and recorded throughput, reusing the existing fk-scale measurement scaffolding.

## 2. Scope (this slice) + explicit deferrals

IN scope (build + light validation, no new spend):
- A new capacity bench worker for the MIXED route, modeled on
  scripts/fk-scale/bench_worker_fk.py, driving the PRODUCTION run_pipeline through
  the F2 mixed route (a generate-kind parent table + a masked child over one FK
  edge; generation resident, child masked on the bounded chunked route).
- Reuse verbatim: scripts/fk-scale/_supervisor.py (external ru_maxrss, kill-safe),
  _parity.py (bounded FK parity + committed row counts), tests/perf_fixtures/
  ooc_route_witness.py (route/spill witness for the mask half). Extend
  build_fk_chain.py (or a sibling) to emit a GENERATE-table config for the parent
  plus a masked child fixture (the parent is generated, not read).
- Byte-identity gate: the worker (or its correctness test) compares the mixed-route
  output to the full-frame mixed oracle for the SAME job+seed (reuse the F2 parity
  helpers), so capacity validation also re-confirms correctness at scale.
- A FAST wiring/correctness test under tests/perf/ (pytest.mark.perf, runs in the
  default gate) that runs the worker at a TINY size (e.g. 2-5k rows) in a
  subprocess and asserts: completed, bounded (external peak under a generous small
  ceiling), mixed route actually taken (native backend witness), FK parity ok,
  byte-identical to oracle.
- An OPT-IN scale test (pytest.mark.benchmark, excluded from the default suite) for
  the 1M and 10M tiers.
- RUN the LIGHT tiers LOCALLY now as evidence: 1M (smoke) + 10M, memory-monitored,
  within the proven bound; record results.

DEFERRED (explicit, for a Cam-paced step - scale/spend):
- The 100M tier run (heavy ~1.5h, memory-intensive) - run it deliberately (local
  within the proven 8 GiB bound, or GCP within the pre-authorized budget) when Cam
  can see it; the harness supports it but F4-build does not auto-run 100M unattended.
- The GCP engine-bench.sh "fk-scale/mixed" battery case: the survey flagged this may
  not be live (only fk|throughput|unified-cert validated). Adding/confirming the GCP
  battery wiring is part of the deferred 100M step, not this build slice.
- 200M (Cam-deferred).

## 3. Design (reuse-first)

- `scripts/fk-scale/bench_worker_mixed.py` (new): builds/uses a resident generate
  parent + a disk-backed masked child (one FK edge, admitted by the F2 mixed route),
  runs run_pipeline under AUTO, emits BENCH_JSON. Reuses _supervisor for external
  RSS when invoked via a supervisor entrypoint; reuses _parity for FK + row counts;
  uses ooc_route_witness to assert the mask half's route/backend and bounded spill.
  Three distinct limits kept separate (budget-mb vs rss-ceiling-mb vs mem-cap-mb),
  exactly as bench_worker_fk.py does.
- Fixture: extend scripts/fk-scale/build_fk_chain.py (or a thin sibling
  build_mixed_fixture.py) to produce (a) a generate-table CONFIG for the parent and
  (b) the masked child parquet; tiers passed as explicit row counts (D4 §4 tiers:
  10M=2M/3.5M/4.5M analog; 1M smoke; 100M=20M/35M/45M for the deferred run).
- Correctness: assert byte-identical to the full-frame mixed oracle for the same
  job+seed at the tiny + 1M sizes (reuse the F2 sweep's oracle-vs-mixed comparison).

## 4. Acceptance

- Fast perf test (default gate): tiny mixed job -> completed, bounded, mixed route +
  native backend witnessed, FK parity ok, byte-identical to oracle. Deterministic,
  fast (seconds), subprocess-isolated.
- Opt-in benchmark test: 1M + 10M tiers complete, bounded (external peak <= declared
  ceiling), FK parity ok, byte-identical to oracle, throughput recorded.
- LIGHT-tier evidence recorded (1M + 10M) in
  decoy-platform/docs/product/release-1-validation-runs/<date>-f4-mixed/ (summary
  json) + a decoy-engine docs/records/ note; ROADMAP/RECENTLY-SHIPPED updated.
- 100M + GCP explicitly listed as the deferred paced step (not run here).
- ci-mirror clean; the new scale test carries @pytest.mark.benchmark (excluded from
  the default suite per pyproject addopts); the fast test carries pytest.mark.perf.
- Evidence-vs-gate split honored: scale RUNS are EVIDENCE (a missed bar is a finding,
  Slacked), not a hard CI gate; only the fast wiring test + byte-identity gate.

## 5. Risk

The mask-half bounded-memory behavior is already proven (Phase E / D4); the new risk
is the generate-resident parent's memory at scale (generation stays whole-table
resident by design). At 100M the resident generated parent could dominate RSS - this
is exactly what the deferred 100M run measures, and why generation-at-scale is F4's
real question. The light tiers (1M/10M) validate the harness + bound shape without
the heavy run. Keep the generated parent width modest; measure resident-parent RSS
separately in BENCH_JSON so the deferred 100M run has a clean signal.

## 6. Gate chain

Codex plan-gate (this doc) -> build (Sonnet, reuse fk-scale infra) -> ci-mirror +
fast perf test + 1M/10M opt-in run -> dennis -> Codex final -> merge on green +
Slack. DOCUMENT: ROADMAP + RECENTLY-SHIPPED + a records note; 100M/GCP flagged as
the deferred paced step for Cam. graphify refresh when the main-checkout layout
allows.

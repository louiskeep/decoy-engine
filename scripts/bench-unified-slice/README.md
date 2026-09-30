# Unified-slice benchmark (Task 4.5 D9): v1 CERTIFIED 2026-09-20 (historical), v2 recert pending

Status: the statistical comparison harness (`bench_compare.py`) is **built**,
covered by its own fast test suite
(`tests/physical/test_bench_compare_harness.py`), and was **D9-certified**
(schema v1, the retired ratio gate) as of 2026-09-20 on the reference host
(GCE n2-standard-8), re-run on the route-activation commit: `d9_certified:
true`, all tiers pass -- 10k RSS 1.023x, 100k 1.048x, 1M 1.094x (under the
per-tier band), wall ~4.3x faster at 1M. Cert artifact: `decoy-platform`
`docs/product/release-1-validation-runs/2026-09-20-d9-recert/`. (The
2026-09-18 cert under `.../2026-09-18-tb6-50m/` remains the per-tier
RSS-budget baseline; this recert confirms the activation commit is unchanged
on the measured lane.) `d9_certified` only flips true after the harness's
real 10k/100k/1M sweep runs on a bench node and every gate passes there;
that sweep is a deliberate offline invocation (multi-minute per arm), never
a CI step.

**2026-09-30: the RSS ratio gate is retired; the 2026-09-20 v1 certification
above is historical and no longer current.** The gate itself is now the
absolute `--max-peak-rss-mb` ceiling below, and `d9_certified` additionally
requires that ceiling to be declared and met (not just cert-shape sample
sizes), so schema v2 (`harness_version: "2.0.0"`) needs its own
recertification run on the reference host under `--max-peak-rss-mb 6656`
before it can be cited as current. That v2 recert is PENDING -- the
1.023x/1.048x/1.094x figures above are the measured record of the v1 run
under the old per-tier ratio band; they describe what actually ran then, not
a live v2 result. See `docs/plans/2026-09-30-retire-d9-rss-ratio.md`.

## What is here now

- `bench_worker_unified.py` -- the frozen workload substrate: one process that
  masks the fixed nine-column W2-minus-`pt_ts` shape (3 keyed-hash, 2
  passthrough, 2 redact, 2 truncate) through `run_pipeline`, selected between the
  legacy route (`UNIFIED_BENCH_FLAG=off`) and the unified lane (`on`) by an
  environment flag, and prints one `BENCH_JSON` record (per-strategy timing,
  `out_rows`, `unified_slice_activated`, `workload_fingerprint`). It is
  smoke-tested end-to-end by `tests/physical/test_bench_unified_slice_harness_smoke.py`.
- `bench_compare.py` -- the statistical comparison driver. Runs the worker as a
  fresh subprocess per arm per rep, alternating off/on order by rep parity, and
  reports a paired per-rep ratio (median + inclusive p95), a seeded bootstrap
  CI, and the peak-RSS evidence (`ru_maxrss` from `os.wait4`, the authoritative
  terminal source, plus the ratio as information) per tier. Every tier is
  gated by size (the 100k/1M ratio rule, or the 10k point-difference-plus-
  50ms-floor rule) on wall time; RSS is gated against a declared absolute
  `--max-peak-rss-mb` ceiling, at every tier. A `run_ok`/`d9_certified`
  two-state model keeps a tiny smoke invocation (`--tiers 200 --reps 2
  --warmup 1`) honestly labelled `SMOKE COMPLETE (d9_certified=false)` -- it
  can never print `D9 PASSED`. `--require-cert` makes a non-certifying run
  exit non-zero (and refuses to run at all without a declared ceiling), for
  the offline cert invocation to use.

## Owed: the offline D9 certification run

Before Task 4.6 caller activation, run the harness for real:

```
python scripts/bench-unified-slice/bench_compare.py --require-cert \
    --max-peak-rss-mb 6656 --out d9_cert_results.json
```

on a quiet bench node (the default tiers/reps/warmup/bootstrap already match
the cert minima: 10k/100k/1M rows, 20 reps, 3 warmups, 10000 bootstrap
resamples; `6656` MiB is the documented 6.5 GiB-at-100M reference-host
ceiling). `d9_certified: true` in the output plus the `D9 PASSED` banner is
the certification; anything else (including a clean exit without
`--require-cert`) is not.

## Cam's cross-revision-baseline descope (2026-09-15)

The "flag-off overhead vs main <= 1%" cross-revision check described in an
earlier version of this document is **descoped**. Cam: "we can descope the old
baseline then... not tied to a specific older baseline." `bench_compare.py`
compares flag-off vs flag-on at the SAME revision only.

**Limitation this leaves on record:** a same-revision off-vs-on comparison does
not certify historical flag-off performance, nor a regression that hits both
arms equally (a common-mode regression neither side of the comparison would
show up as a ratio change). Codex confirmed this is an acceptable gap for
Task 4.6's narrow decision, given the separate structural proof that flag-off
is inert at this revision: flag-off returns at the flag check before importing
any `execution.physical` code
(`tests/physical/test_unified_slice_inertness.py`), so the added cost from the
flag itself is a bounded constant, not per-row work -- it is not, however, a
substitute for an actual historical-baseline measurement, which this harness
does not attempt.

## The full D9 requirement (unchanged, now implemented)

- **Tiers:** 10k, 100k, 1M rows. **Warmups:** >= 3 discarded. **Reps:** >= 20 timed.
- **Per-repetition pairing/alternation:** alternate the off (legacy) and on
  (unified) arms at the REPETITION level, not whole-arm sweeps, so a short host
  load spike cannot land entirely inside one arm.
- **Identical workload, enforced:** both arms run this same worker/source; the
  harness asserts the `workload_fingerprint` matches between arms AND that every
  rep's `out_rows` equals the requested `n_rows` (a worker that silently masks
  the wrong row count fails, not passes).
- **Activation, enforced:** the `on` arm must actually activate the unified slice
  (`unified_slice_activated is True`); a silent legacy fallback fails closed.
- **Thresholds:** 100k & 1M median new/old wall <= 1.10 and p95 <= 1.15; 10k median
  regression <= max(10%, 50 ms); peak RSS <= a declared `--max-peak-rss-mb`
  absolute ceiling (no ratio against the pandas arm's peak; retired 2026-09-30,
  docs/plans/2026-09-30-retire-d9-rss-ratio.md).
- **Fail-closed RSS:** a missing peak-RSS sample (per rep OR aggregate) is a
  FAILURE -- the RSS bound cannot be certified without the evidence, so the harness
  never silently drops it and reports PASS.
- **Statistics:** median, p95, and a bootstrap CI on the paired ratio; the CI
  upper bound is itself gated, not just reported.

## Known limitations from the dennis review (carry-forwards, not blocking the harness)

- **Off-arm lane identity is worker-self-reported, not observed (MEDIUM-2).** The
  harness trusts the worker's `execution_mode` / `unified_slice_activated` fields
  to prove the off arm ran the legacy route. If the unified lane ever executed
  under `unified_slice_enabled=False`, the off arm would still self-report
  `legacy_full_frame` / `False` and both arms could benchmark the same lane. The
  worker is frozen (out of scope for this harness) and flag-off inertness is
  proven separately by `tests/physical/test_unified_slice_inertness.py`, so this
  is a known residual, not a false claim in shipped output. Making the harness
  observe (rather than trust) the off-arm route is a worker + harness change.
- **RSS gate compares max-of-max (MEDIUM-3, historical).** This applied to the
  since-retired ratio gate: `max_on_ru_maxrss <= rss_budget_ratio(n_rows) *
  max_off_ru_maxrss`. As of 2026-09-30 the gate is `max_on_ru_maxrss <=
  max_peak_rss_mb * 1024`, an absolute ceiling with no dependency on the
  pandas arm's peak, so a masked paired regression on the off arm no longer
  matters to this gate -- only the on arm's own peak does. Kept for history:
  under the old gate, non-physical per-rep variance (one off rep spiking to
  match on's peak) could mask a paired memory regression.

## Separate follow-up: FOLLOWUP-BENCH-DRIVER-HARDEN

`scripts/native-baseline/bench_driver.py` is a SHARED driver used by other perf
programs (native-baseline). Task 4.5 reverted its changes rather than modify
shared tooling unilaterally. Harden it with its own consumers in scope: None-safe
tier-summary formatting (a `None` `hash_tput` must not crash the summary line) and
fail-closed per-rep RSS aggregation.

## Separate follow-up: FOLLOWUP-UNIFIED-EXCEPTION-BOUNDARY (before Task 4.6)

The lane's fail-closed boundary in `src/decoy_engine/execution/_unified_slice.py`
catches only `ShadowDifference`. For an ALREADY-ADMITTED job, an unexpected raise
from `compile_physical_plan`, a `build_live_physical_plan_inputs` helper, or the
native kernel mid-batch would propagate to the caller (flag-on) rather than reroute
to the legacy route. No known trigger exists given how narrow admission is, and the
lane is default-off with caller activation deferred, so this does not block the 4.5
merge. Before Task 4.6 flips any caller default on, widen the boundary so any
unexpected exception on an admitted job also fails closed (reroute to the legacy
route, or raise `UnifiedSliceInvariantError`), never a raw compiler/kernel type.
(dennis final-review LOW-1, 2026-09-15.)

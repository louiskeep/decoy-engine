# Mutation grading: `execution/_probe.py` -- memory micro-probe

TQ isolated-substrate grade (branch `tq/isolated-substrate-grade`) by
`scripts/tq_mutate.py`. `_probe.py` runs a down-scaled job through the isolated
runner and does a two-point memory fit (+ guards) to estimate the full job's peak
RSS. Finding #15 listed it as an "un-gradeable subprocess substrate", but
`run_pipeline_isolated` is INJECTED via a `run_isolated=` parameter, so the probe's
own fit/guard logic runs in the PARENT and grades in-process with a fast in-process
fake runner -- no de-mock, no standalone-per-mutant runner. Second parent-resident
module (after `_governor`); confirms the parent-resident tier is the same additive
cadence as the pure tier. Public surface: `probe_peak_bytes`, `probe_fits`,
`_run_one_probe`, `_fit_line`, `uniqueness_saturation_risk`. Not crypto/RI.

## Numbers

Baseline (existing `test_probe.py` + `test_probe_routing.py`): 158/251 = 62.95%
LOGIC. Full triage of the 93 survivors (two kill files, per-cluster): **49 LOGIC
kills** + **18 proven equivalent** + **26 accepted non-contract**. Re-grade with both
kill files in the selection: **207/251 = 82.47% LOGIC (tool-native, 0 unresolved)**;
all 49 LOGIC targets absent from the survivor set, no drift. Above the measure-first
bar max(62.95 + 15, 75) = 77.95%.

| Function | Survivors | Killed | Proven equiv | Accepted non-contract |
|---|---|---|---|---|
| `probe_peak_bytes` | 66 | 34 | 11 | 21 |
| `probe_fits` | 12 | 5 | 7 | 0 |
| `_run_one_probe` | 11 | 6 | 0 | 5 |
| `_fit_line` | 2 | 2 | 0 | 0 |
| `uniqueness_saturation_risk` | 2 | 2 | 0 | 0 |
| **total** | **93** | **49** | **18** | **26** |

Kill files: `tests/unit/execution/test_probe_peak_kills.py` (16 tests, 0.65s) and
`tests/unit/execution/test_probe_fits_run_kills.py` (14 tests). A `run_isolated=`
fake returns controlled peak-RSS points so the two-point fit and every guard verdict
grade deterministically in-process.

## Kills (49)

### `probe_peak_bytes` (34) -- peak kills file
The input-validation boundaries (`target_rows <= 0`->`<= 1`; the `0 < low_frac <
high_frac <= 1.0` fraction chain, each pinned at its boundary fraction); the two
short-circuit verdicts as the bool `False` not `None` (uniqueness / opaque-generate)
which `not result.conclusive` cannot see; the `mem_cap_bytes` forwarding to each
run; and every post-measurement inconclusive branch pinned on BOTH its verdict
(`is False`) and its exact `low_point`/`high_point` `ProbePoint`s -- degenerate
equal-rows, non-positive slope (`<= 0` boundary at slope 0 and 0.5), negative
extrapolation (estimated == 0), and the raw-bytes floor (estimate on the floor).
137: the conclusive `reason="measured"` -> None, killed by the "always populated"
invariant (`reason is not None`), not by wording.

### `probe_fits` (5) + `_run_one_probe` (6) + `_fit_line` (2) + `uniqueness_saturation_risk` (2) -- fits/run kills file
`probe_fits`: default `error_band` 0.30 (not 1.3), the `error_band < 0` guard (a 0.0
band is valid), the `budget_bytes <= 0` guard (a 1-byte budget is valid), the
`(1 + error_band)` margin multiplier, and the strict `< budget` fit test at
exact-on-budget. `_run_one_probe`: the conclusive verdict as bool `False` not `None`
on both the no-row-count and unclean-measurement branches, plus the forwarded
`run_isolated` args (downscaled sources, `mem_cap_bytes` verbatim + present,
`isolate=True`). `_fit_line`: the degeneracy guard `<= 0` (equal rows -> None, not
div-by-zero; one-row spread fits the exact line) -- tested by direct import since
the public path short-circuits it. `uniqueness_saturation_risk`: the missing
row-count `continue` (not `break`) and the `>=` threshold boundary.

## Proven equivalent (18)

- `probe_peak_bytes` 80-90 (11): the `if fit is None` branch after `_fit_line`.
  Unreachable -- the preceding guard returns on `low.rows == high.rows`, and
  `_fit_line` returns None only when `high.rows < low.rows`; `scale_row_count` is
  monotonic non-decreasing in fraction and `probe_fractions` is validated
  `low_frac < high_frac`, so `high.rows >= low.rows` always. Dead defensive code.
- `probe_fits` 11-17 (7): the `estimated_bytes is None` AssertionError branch is
  unreachable -- `ProbeResult.__post_init__` forbids conclusive=True with a None
  estimate, so no public construction reaches it.

## Accepted non-contract (26)

Reachable-branch message/exception prose, killable only by brittle
full-message-equality (the code path / `match=` substring / reference data is pinned
separately). Each verified to survive standalone (rc 0).
- `probe_peak_bytes` 5-7, 25, 28-30, 37-39, 72, 73, 103, 104, 131-135, 151, 152 (21):
  the `execution_mode` ValueError (`match="execution_mode"` survives) and the
  uniqueness / opaque / degenerate / slope / floor `reason` strings, plus the
  conclusive-path `reason="measured"` relabels (137 pins populated-ness, not the
  literal).
- `_run_one_probe` 18-21, 52 (5): the no-row-count reason-string prose (the
  `reference_table` repr is pinned by `test_probe.py`; the surrounding sentence is
  not) and the empty-error suffix literal (telemetry only).

## Candidate findings

None. No mutation exposed a wrong fitted peak, slope, intercept, verdict, boundary,
or forwarded arg.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_probe.py` and
the test selection to `tests/unit/execution/test_probe_peak_kills.py`,
`tests/unit/execution/test_probe_fits_run_kills.py`,
`tests/unit/execution/test_probe.py`, and `tests/unit/execution/test_probe_routing.py`;
then `rm -rf mutants && python scripts/tq_mutate.py --run --jobs 6`. NOTE:
`test_probe_routing.py` is a slow (~41s) integration suite, so the survived-bucket
re-adjudication is slow; the fast kill files carry the full-triage kills.
`source_paths` stays at the package root.

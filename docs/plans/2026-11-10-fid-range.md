# FID-RANGE: numeric fidelity scale that one value cannot stretch

Status: plan

Rules consulted: 00-universal, risk-and-exceptions, development-loop (Proportion, PLAN),
testing (Test depth), CODEMAP "Where Things Go"

Base: engine main `97a1cbbd`. Roadmap row 24v (V1, blocks V1, engine). Source: DI models QA
2026-11-10 item C3 (CLI repo `docs/records/2026-11-10-cli-qa-generation-distribution-models.md`;
re-test on engine `5c24a75e`: outlier 0.940, skew 0.969, price 0.926). Null scoring is done
(engine #438, row 24l E3) and is not touched. One slice, engine only. Undated file name as the
orchestrator asked; CODEMAP lists undated plans as exceptions.

## Goal

A numeric column whose output shape is wrong must score low, even when the source holds one
extreme value. Today the quantile error is divided by `max - min`, so a single 1,000,000 in a
column of values near 150 makes any output look 94% similar.

Guarantee: when the source IQR is positive, numeric `value_similarity` uses a scale that the top
and bottom 25% of source rows cannot change, and every numeric score is unit-free (multiplying
source and output by the same positive constant leaves it unchanged). Non-goals: other kinds, joints, `extra_metrics`, the DP path, the generator (row
24u GEN-NUM-SHAPE), the CLI report path, the platform.

## Current behavior

- `src/decoy_engine/quality/fidelity.py:403-439` `_numeric_similarity`: RMSE over the shared
  quantile keys (snapshots carry `p05 p25 p50 p75 p95`, `quality/snapshot.py:428-433`), each
  difference divided by `src_range = max(src_max - src_min, 1.0)` (`fidelity.py:424-430`);
  similarity `1 - min(1, rmse)`, method `quantile_rmse`.
- The module docstring (`fidelity.py:18-23`) and `docs/distribution-integrity/snapshot-and-fidelity.md:107-111`
  state that formula. The doc also says "Scores are symmetric", already false for numeric
  (the scale comes from the source only).
- The `1.0` floor is in column units, so a column living in `[0, 0.01]` scores near 1.0 for any
  drift (0.997 for a 3-standard-deviation shift, table below).
- Column `similarity` = `value_similarity * missing_value_similarity` (`fidelity.py:300-320`, #438).
  Consumers: `quality/report.py:154` (grade), `generation/_fidelity_gate.py:149` (warn at 0.8),
  `execution/_pipeline_finalize.py:157`, `quality/policy.py` (strategy floors).

## Established solutions

Search run: web search for IQR-normalized error metrics, plus the codebase (`quality/`). Found:
RPIQ, the ratio of performance to interquartile distance (Bellon-Maurel et al. 2010; implemented in
R `yardstick::rpiq` and `chillR::RPIQ`), defined as IQR of the observed values over RMSE. It was
introduced because range and standard deviation scales mislead on skewed data. SDMetrics
`KSComplement` is the rank view; the engine already reports it as `extra_metrics.ks_complement`.
The design follows RPIQ: RMSE over the quantile grid divided by the source IQR, kept in the
existing `1 - min(1, x)` form. Custom code is only the zero-IQR fallback, which RPIQ leaves
undefined.

## Chosen scale: source IQR (p75 - p25)

IQR, not a rank scale. p25 and p75 are already in every snapshot, so there is no snapshot field
and no format change. A rank scale needs the raw source or a finer ECDF than five points, and the
rank view already exists as `ks_complement`.

- Heavy tail (one 1,000,000 among values near 150, or a log-normal price): the middle half of the
  rows sets the IQR, so the outlier cannot widen it. p05/p95 errors still count in full against
  that spread, which is what flags a wrong tail.
- Constant or near-constant: IQR is 0 once half the rows share one value, so a fallback is needed.
  Every fallback is in the column's own units, so no rule depends on the unit.

Scale, from the source snapshot only, first rule that applies:

1. p25 and p75 present and `p75 - p25 > 0`: scale = IQR.
2. p05 and p95 present and `p95 - p05 > 0`: scale = `p95 - p05`.
3. `max - min > 0`: scale = `max - min`, with no 1.0 floor. This covers near-constant columns (90%
   or more of rows on one value) and hand-built grids with neither spread pair; the p50-only
   fixtures keep their numbers.
4. `max == min` (true constant): similarity is 1.0 when every shared quantile of the output equals
   the source's (exact equality after snapshot rounding), else 0.0.

Rule 3 can still be widened by an outlier, but only when at least 90% of the rows sit on one
value, where the five-point grid can only say "on the value" or "off it". The unit-dependent
`max(range, 1.0)` floor is removed everywhere. One private helper in `fidelity.py`
(`_quantile_scale`, returns the scale or `None` for rule 4). Output shape unchanged: no new key,
`method` stays `quantile_rmse`, no scale value emitted (value-free). Design notes: none, no new
structure.

## Behavior change: which scores move

Only numeric `value_similarity`, and through it `similarity`, `marginal.score`, `overall_score`,
the grade, the generation fidelity warning and policy verdicts. Prototype of the new rule on
`97a1cbbd`, using exactly the constructions of the acceptance tests below:

| Case (acceptance test) | Today | New |
|---|---|---|
| outlier, wrong shape (1) | 0.940333 | 0.0 |
| outlier, good fit (2) | 0.999999 | 0.963201 |
| price, wrong shape (3) | 0.963021 | 0.0 |
| price, good fit (4) | 0.998041 | 0.876074 |
| normal, fresh draw (5a) | 0.997081 | 0.984698 |
| normal, bin-uniform (5b) | 0.992359 | 0.959939 |
| half-SD shift (6) | 0.923332 | 0.598035 |
| half-SD shift x0.001 (7) | 0.986642 | 0.598035 |
| small-unit shift (8) | 0.997 | 0.0 |
| zero-mass, x1 / x1000 / x0.001 (10) | 0.552786 / 0.552786 / 0.955279 | 0.552786 all three |
| constant 7 to 7 / 7 to 8 (11) | 1.0 / 0.0 | 1.0 / 0.0 |
| constant 0.007 to 0.008 (11) | 0.999 | 0.0 |

## Failure modes

- F1 Near-constant source (rule 3): 90% or more of rows on one value plus a far tail value gives a
  wide range scale, so tail drift is under-scored. Accepted: the grid cannot resolve it, the score
  is the same in every unit, and `ks_complement` still reports the mass shift.
- F2 Small samples or heavy tails: p05/p95 noise against the IQR costs a good fit points (the
  price good fit is 0.876). With tens of rows a good fit can drop below 0.8 and fire the
  generation warning. Report only.
- F3 Generated numeric columns score lower until row 24u fixes the generator. That is the honest
  result this row exists for.
- F4 A consumer with policy mode `fail` and numeric floors can fail jobs it passed before.
  Changelog marks this Breaking.
- F5 A builder lowers a bound to make a test pass. Every bound below is fixed.

## Acceptance tests

New file `tests/unit/quality/test_fidelity_numeric_scale.py`. Each array is scored through
`compute_distribution_snapshot(pd.DataFrame({"x": arr}))` then `compute_fidelity`, and the test
reads the column's `value_similarity`. Each random array uses its own
`np.random.default_rng(<seed>)`, created fresh, with exactly the calls named, in the order named.

`bin_uniform(src, n, seed)` (test-local, a stand-in for the C2 generator so row 24u cannot move
these tests): `rng = np.random.default_rng(seed)`; `counts, edges = np.histogram(src, bins=10)`;
`idx = rng.choice(10, size=n, p=counts / counts.sum())`; `return rng.uniform(edges[idx], edges[idx + 1])`.

1. `test_single_outlier_wrong_shape_scores_low`: `src = np.append(default_rng(101).normal(150.0, 20.0, 4999), 1_000_000.0)`;
   output `bin_uniform(src, 5000, 102)`. Value similarity <= 0.10 (today 0.940333).
2. `test_single_outlier_good_fit_stays_high`: same `src`; output
   `np.append(default_rng(103).normal(150.0, 20.0, 4999), 1_000_000.0)`. >= 0.95.
3. `test_skewed_price_wrong_shape_scores_low`: `src = np.round(default_rng(201).lognormal(3.0, 1.2, 5000), 2)`;
   output `bin_uniform(src, 5000, 202)`. <= 0.10 (today 0.963021).
4. `test_skewed_price_good_fit_stays_high`: same `src`; output
   `np.round(default_rng(203).lognormal(3.0, 1.2, 5000), 2)`. >= 0.85.
5. `test_normal_good_fit_stays_high`: `src = default_rng(301).normal(100.0, 25.0, 2000)`; outputs
   (a) `default_rng(302).normal(100.0, 25.0, 2000)` and (b) `bin_uniform(src, 2000, 303)`. Both >= 0.90.
6. `test_half_sd_shift_is_penalized`: source as in 5; output `default_rng(304).normal(112.5, 25.0, 2000)`.
   0.40 <= value similarity <= 0.75 (today 0.923332).
7. `test_iqr_score_is_unit_free`: the pair from 6 with source and output both multiplied by 1000,
   and both by 0.001. All three scores equal within 1e-6 (today the x0.001 pair scores 0.986642).
8. `test_small_unit_column_drift_is_seen`: `src = default_rng(401).normal(0.005, 0.001, 5000)`;
   output `src + 0.003`. <= 0.10 (today 0.997).
9. `test_zero_iqr_falls_back_to_p05_p95`: hand-built source stats `min -100, max 100`, quantiles
   `{p05: 0, p25: 10, p50: 10, p75: 10, p95: 20}`; output the same with `p50: 12`. Value similarity
   == approx(0.955279) (one difference 2/20, RMSE sqrt(0.01/5)); today 0.995528 (range 200).
10. `test_zero_spread_fallback_is_unit_free`: `src = np.concatenate([np.zeros(97), np.full(3, 100.0)])`,
    `out = np.concatenate([np.zeros(90), np.full(10, 100.0)])`, then the same pair times 1000 and
    times 0.001. All three == approx(0.552786) (rule 3: only p95 differs, by the full range);
    today the x0.001 pair scores 0.955279.
11. `test_constant_column`: `np.full(5000, 7.0)` vs itself is 1.0; vs `np.full(5000, 8.0)` is 0.0;
    `np.full(5000, 0.007)` vs `np.full(5000, 0.008)` is 0.0 (today 0.999).
12. `test_partial_grid_uses_range`: p50-only fixture, 50 vs 75 on min 0, max 100, is 0.75.
13. `test_numeric_entry_shape_unchanged`: numeric entry keys equal today's set; method `quantile_rmse`.

Must pass unchanged, bounds not edited: `tests/unit/quality/test_fidelity.py`,
`tests/unit/generation/test_statistical.py` (`test_distribution_shape_matches_source`,
`overall_score >= 0.85`), `test_fidelity_gate.py`, and every test in
`test_fidelity_missing_value.py` except the one re-baseline below. A failure there is a stop and a
report.

## Re-baselines

Intentional, each with its new value written in the diff:

- `tests/unit/quality/test_fidelity_missing_value.py::test_literal_product_cell` (line 148 on
  `97a1cbbd`): a ten-unit shift on all five quantiles was 0.9 over the range 100; over the IQR 50
  it is 0.8. `value_similarity` 0.9 to 0.8, `similarity` 0.72 to 0.64, comment updated.
  `missing_value_similarity` stays 0.8. `test_value_times_missing_cell` computes its base from the
  comparator and does not change.
- `tests/snapshots/golden/quality_fidelity/numeric_drift.sha256` (rule 2, scale 90: 0.85 to 0.833333).
- `tests/snapshots/golden/quality_report/row_count_drift.sha256` (0..19 vs 0..9: numeric value
  0.686 to 0.371).

Regenerate goldens with `UPDATE_SNAPSHOTS=1`. Any other moved digest or pinned number is a stop.
Out of scope, follow-up: the platform honest-capability statement
(`decoy-platform/docs/backlog/distribution-integrity/honest-capability-statement.md`) and its pin
`tests/test_di0_quality_contract.py` re-baseline at the next platform engine re-pin; platform is
parked, so it goes on the re-pin checklist. The CLI QA record's C3 row takes new re-test numbers
when the row closes.

## Changelog fragment (`changelog.d/fid-range.md`)

```
Breaking: numeric fidelity scores drop for skewed or drifted columns; re-check fidelity_warn_threshold and policy floors.

### Changed (numeric fidelity scale, FID-RANGE, 2026-11-10)

Numeric column similarity now divides the quantile error by the source interquartile range
(p75 - p25) instead of the source min-to-max range, so one extreme value no longer hides a wrong
shape. When that range is 0 it uses p95 - p05, then max - min. A constant column scores 1 when the
output quantiles match it and 0 otherwise. The old 1.0 floor is gone, so scores no longer depend
on the column's units. The report shape and method name do not change.
```

## Placement

| File | Change | Directory and CODEMAP line | Sits beside |
|---|---|---|---|
| `src/decoy_engine/quality/fidelity.py` | `_quantile_scale`, `_numeric_similarity` uses it, docstring | `quality/` (CODEMAP: quality report and comparators) | same file; 573 lines, keep it at or under 600 (repo CLAUDE.md; over 600 needs a `test_module_size.py` census entry) |
| `tests/unit/quality/test_fidelity_numeric_scale.py` | new, tests 1-13 | `tests/unit/quality/` | `test_fidelity.py` (646 lines, too near the cap to grow) |
| `tests/unit/quality/test_fidelity_missing_value.py` | one re-baseline | `tests/unit/quality/` | - |
| `tests/snapshots/golden/quality_fidelity/numeric_drift.sha256`, `quality_report/row_count_drift.sha256` | re-baselined | existing golden harness | - |
| `docs/distribution-integrity/snapshot-and-fidelity.md` | formula row, fallback, numeric not symmetric | CODEMAP doc index: distribution integrity | - |
| `changelog.d/fid-range.md` | new fragment | "Changelog fragments" row | other fragments |

No new module, no CODEMAP change, no choke point (one call site).

## Slice and risk

One slice, branch `feat/fid-range`: helper and comparator, tests, re-baselines, doc, fragment. T0
on the quality test files; T1 impact set `tests/unit/quality`, `tests/unit/generation`,
`tests/snapshots`. The `quality-fidelity/v1` format is unchanged (values only), so no full-suite
trigger.

Risk: R1, low. Scoring and reports only; no data path, no output data, no logs, no new emitted
value. One independent reviewer (Codex). V1 bar: no function break, data loss, non-admin leak or
cascading design; F1 and F2 go to V2-LOG if raised. No open owner question.

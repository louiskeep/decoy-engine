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

Guarantee: numeric `value_similarity` uses a scale that the top and bottom 25% of source rows
cannot change. Non-goals: other kinds, joints, `extra_metrics`, the DP path, the generator (row
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

Scale, from source quantiles only, first rule that applies:

1. p25 and p75 present, `p75 - p25 > 0`: scale = IQR.
2. p05 and p95 present, `p95 - p05 > 0`: scale = `p95 - p05`.
3. A spread pair present but every present spread is 0 (90% or more of rows on one value, or a
   constant column): scale = `1.0` in column units, today's zero-range floor. A constant column
   scores exactly as today (7 vs 7 is 1.0, 7 vs 8 is 0.0).
4. Neither spread pair present (hand-built or foreign snapshots only; `compute_distribution_snapshot`
   always emits all five keys): today's `max(max - min, 1.0)`. Existing p50-only fixtures keep
   their numbers.

No floor on a positive spread, so the score no longer depends on units. One private helper,
`_quantile_scale(src_q, src_min, src_max) -> float`, in `fidelity.py`. Output shape unchanged: no
new key, `method` stays `quantile_rmse`, no scale value emitted (value-free). Design notes: none,
no new structure.

## Behavior change: which scores move

Only numeric `value_similarity`, and through it `similarity`, `marginal.score`, `overall_score`,
the grade, the generation fidelity warning and policy verdicts. Prototype on `97a1cbbd`, seeded,
5,000 rows unless noted:

| Case | Today | IQR rule |
|---|---|---|
| N(150, 20) plus one 1,000,000; output uniform within 10 equal-width source bins (the C2 shape) | 0.941 | 0.0 |
| same source; fresh draw of the same shape plus the outlier | 1.0 | 0.987 |
| log-normal(3, 1.2) price, 2 decimals; bin-uniform output | 0.970 | 0.0 |
| same price; fresh draw | 0.999 | 0.949 |
| N(100, 25), 2,000 rows; fresh draw | 0.992 | 0.955 |
| N(100, 25); bin-uniform output (bins not stretched) | 0.994 | 0.963 |
| N(100, 25); output shifted half a standard deviation | 0.936 | 0.623 |
| N(0.005, 0.001); output shifted by 0.003 | 0.997 | 0.0 |
| 97% zeros; output 90% zeros (rule 3) | 0.990 | 0.0 |

Good fits lose a few points to p05/p95 sampling noise and stay at 0.94 or above.

## Failure modes

- F1 Near-constant source (rule 3) scores harshly: 97% to 90% zeros is 0.0, because p95 moved
  hundreds against a 1.0 scale. Accepted: 7% of the mass left the dominant value and the grid
  cannot say less. `ks_complement` keeps the softer view.
- F2 Small samples: with tens of rows, p05/p95 noise against a small IQR can push a good fit below
  0.8 and fire the generation warning. Report only.
- F3 Generated numeric columns score lower until row 24u fixes the generator. That is the honest
  result this row exists for.
- F4 A consumer with policy mode `fail` and numeric floors can fail jobs it passed before.
  Changelog marks this Breaking.
- F5 A builder lowers a bound to make a test pass. Every bound below is fixed.

## Acceptance tests

New file `tests/unit/quality/test_fidelity_numeric_scale.py`. Inputs are seeded numpy arrays
scored through `compute_distribution_snapshot` then `compute_fidelity`. "Bin-uniform" is a
test-local helper: pick one of the source's 10 equal-width `np.histogram` bins by count, draw
uniform inside it. It stands in for the C2 generator so row 24u cannot move these tests.

1. `test_single_outlier_wrong_shape_scores_low`: 4,999 x N(150, 20) plus 1,000,000.0; bin-uniform
   output, 5,000 rows. Similarity <= 0.10.
2. `test_single_outlier_good_fit_stays_high`: same source; 4,999 fresh N(150, 20) plus 1,000,000.0.
   Similarity >= 0.95.
3. `test_skewed_price_wrong_shape_scores_low`: 5,000 x log-normal(3, 1.2) rounded to 2 decimals;
   bin-uniform output. Similarity <= 0.10.
4. `test_skewed_price_good_fit_stays_high`: same source; fresh draw. Similarity >= 0.90.
5. `test_normal_good_fit_stays_high`: 2,000 x N(100, 25); fresh draw and bin-uniform output, both
   >= 0.90.
6. `test_half_sd_shift_is_penalized`: N(100, 25) vs N(112.5, 25), 2,000 rows; 0.40 <= similarity <= 0.75.
7. `test_score_is_unit_free`: case 6 with source and output both multiplied by 1,000 and by 0.001;
   the three similarities equal within 1e-6.
8. `test_small_unit_column_drift_is_seen`: N(0.005, 0.001) vs the same plus 0.003; <= 0.10.
9. `test_zero_iqr_falls_back_to_p05_p95`: hand-built source grid `{p05: 0, p25: 10, p50: 10,
   p75: 10, p95: 20}`, output the same with `p50: 12`; similarity == approx(0.955279).
10. `test_constant_column_keeps_unit_floor`: 5,000 x 7.0 vs itself is 1.0; vs 8.0 is 0.0.
11. `test_partial_grid_keeps_range_scale`: p50-only fixture, 50 vs 75 on min 0, max 100, is 0.75.
12. `test_numeric_entry_shape_unchanged`: numeric entry keys equal today's set; method `quantile_rmse`.

Must pass unchanged, bounds not edited: `tests/unit/quality/test_fidelity.py`,
`test_fidelity_missing_value.py`, `tests/unit/generation/test_statistical.py`
(`test_distribution_shape_matches_source`, `overall_score >= 0.85`), `test_fidelity_gate.py`.
A failure there is a stop and a report.

## Re-baselines

Regenerate with `UPDATE_SNAPSHOTS=1`. Only these digests may change:

- `tests/snapshots/golden/quality_fidelity/numeric_drift.sha256` (p05/p95 scale 90: 0.85 to 0.833333).
- `tests/snapshots/golden/quality_report/row_count_drift.sha256` (0..19 vs 0..9: numeric value
  0.686 to 0.371).

Any other moved digest is a stop. Out of scope, follow-up: the platform honest-capability
statement (`decoy-platform/docs/backlog/distribution-integrity/honest-capability-statement.md`)
and its pin `tests/test_di0_quality_contract.py` re-baseline at the next platform engine re-pin;
platform is parked, so it goes on the re-pin checklist. The CLI QA record's C3 row takes new
re-test numbers when the row closes.

## Changelog fragment (`changelog.d/fid-range.md`)

```
Breaking: numeric fidelity scores drop for skewed or drifted columns; re-check fidelity_warn_threshold and policy floors.

### Changed (numeric fidelity scale, FID-RANGE, 2026-11-10)

Numeric column similarity now divides the quantile error by the source interquartile range
(p75 - p25) instead of the source min-to-max range, so one extreme value no longer hides a wrong
shape. When that range is 0 it uses p95 - p05, then 1.0. The report shape and method name do not
change.
```

## Placement

| File | Change | Directory and CODEMAP line | Sits beside |
|---|---|---|---|
| `src/decoy_engine/quality/fidelity.py` | `_quantile_scale`, `_numeric_similarity` uses it, docstring | `quality/` (CODEMAP: quality report and comparators) | same file; 573 lines, keep it at or under 600 (repo CLAUDE.md; over 600 needs a `test_module_size.py` census entry) |
| `tests/unit/quality/test_fidelity_numeric_scale.py` | new, tests 1-12 | `tests/unit/quality/` | `test_fidelity.py` (646 lines, too near the cap to grow) |
| `tests/snapshots/golden/quality_fidelity/numeric_drift.sha256`, `quality_report/row_count_drift.sha256` | re-baselined | existing golden harness | - |
| `docs/distribution-integrity/snapshot-and-fidelity.md` | formula row, fallback, numeric not symmetric | CODEMAP doc index: distribution integrity | - |
| `changelog.d/fid-range.md` | new fragment | "Changelog fragments" row | other fragments |

No new module, no CODEMAP change, no choke point (one call site).

## Slice and risk

One slice, branch `feat/fid-range`: helper and comparator, tests, goldens, doc, fragment. T0 on
the two quality test files; T1 impact set `tests/unit/quality`, `tests/unit/generation`,
`tests/snapshots`. The `quality-fidelity/v1` format is unchanged (values only), so no full-suite
trigger.

Risk: R1, low. Scoring and reports only; no data path, no output data, no logs, no new emitted
value. One independent reviewer (Codex). V1 bar: no function break, data loss, non-admin leak or
cascading design; F1 and F2 go to V2-LOG if raised.

Owner question (default chosen, proceed unless overridden): rule 3 keeps today's absolute 1.0
scale for near-constant columns, so a 97% to 90% zero shift scores 0.0.

# Snapshots, fidelity and the quality report

Status: current

[Back to the developer reference](index.md)

The measurement half of the subsystem: summarize two frames, compare the
summaries, assemble a report, apply a policy.

## Pipeline

```text
source_df ─ compute_distribution_snapshot ─┐
                                           ├─ compute_diagnostic ─┐
output_df ─ compute_distribution_snapshot ─┤                      │
                                           ├─ compute_fidelity ───┼─ assemble_quality_report
                                           └─ compute_shape_fidelity ┘        │
                                                                 apply_quality_policy
```

`compute_quality_report(source_df, output_df, ...)` runs the whole chain.
`assemble_quality_report` takes pre-computed parts, for callers that already
hold snapshots.

## The snapshot (`quality/snapshot.py`)

```python
compute_distribution_snapshot(
    df,
    *,
    joint_columns=None,          # [(col_a, col_b), ...]
    numeric_bins=10,
    categorical_top_k=20,
    contingency_top_k=25,
    high_cardinality_columns=(),
    fanout_columns=(),
    cell_format=False,           # True adds a per-column `cell_format` block
)
```

`profiling._snapshot_values.distribution_snapshot_document` wraps it with
`cell_format=True` and withholds the cell values of columns the profiler flags
as sensitive (engine #420). Columns the profiler does not flag keep their
values, including rare labels and exact `min` and `max`.

Top-level shape:

```json
{
  "schema_version": "distribution-snapshot/v1",
  "row_count": 500,
  "columns": { "<name>": { "dtype": "...", "kind": "...", "null_count": 0,
                           "non_null_count": 500, "distinct_count": 5,
                           "stats": { } } },
  "joints": [ { "columns": ["a", "b"], "cell_count": 13,
                "cells": [ {"key": ["CA", "gold"], "count": 130} ],
                "other_count": 0 } ]
}
```

### Column kinds and their stats

| Kind | Assigned when | `stats` |
|---|---|---|
| `numeric` | Numeric dtype | `min`, `max`, `mean`, `std`, `quantiles` (`p05`, `p25`, `p50`, `p75`, `p95`), `bin_edges`, `bin_counts` (equal-width over min to max) |
| `categorical` | Bool dtype, or object dtype with at most 30 distinct values | `top_values` (`[{value, count}]`, sorted by count then value), `other_count` |
| `datetime` | Datetime dtype | `min`, `max` (ISO), `year_bins` (`[{year, count}]`) |
| `freetext` | Object dtype with more than 30 distinct values | `length` (`min`, `max`, `mean`, `std`), `length_bin_edges`, `length_bin_counts` |
| `empty` | No non-null values | none |

Design points:

- **Equal-width bins with a fixed count**, not an adaptive rule, so the bin
  count is predictable. Bin edges still differ between two snapshots because
  each uses its own min and max. That is why the primary numeric comparison
  uses quantiles, not bins.
- **The 30-distinct cutoff** (`_CATEGORICAL_DISTINCT_CAP`) is the single fit-time
  source of truth for categorical versus freetext.
- **`high_cardinality_columns`** forces `categorical` and keeps every value. It
  is limited to 100,000 distinct values and 16 MiB of label bytes and raises a
  typed error past either. The CLI `decoy fit` does not expose it today.
- **Joint tables** drop rows where either column is null, cast both to strings,
  and keep the top 25 cells.
- **The snapshot is not a validator.** Unknown columns in `joint_columns` are
  skipped silently.

The snapshot holds real category labels. It is the sensitive artifact of this
subsystem. Reports built from it are not.

## Diagnostic (`quality/diagnostic.py`)

Structural checks that say whether a comparison is meaningful. Output is
`quality-diagnostic/v1` with `passed` and a `checks` list.

| Check | Fails when |
|---|---|
| `column_survival` | A source column is missing from the output. Added columns are recorded, not failed. |
| `row_count` | Row counts differ, unless `expect_row_parity=False`. |
| `kind_drift` | A column's kind changed (for example numeric to freetext after hashing). |
| `null_drift` | A column's null rate moved by more than `null_drift_threshold_pp` (default 10 percentage points). |

A failed diagnostic does not stop scoring. It surfaces as report warnings.

## Value-identity fidelity (`quality/fidelity.py`)

`compute_fidelity(source_snapshot, output_snapshot)` returns a similarity in
[0, 1] per column and per joint. Scores are symmetric.

| Kind | Method tag | Formula |
|---|---|---|
| numeric | `quantile_rmse` | `1 - min(1, RMSE(quantile grid) / source_range)` |
| categorical | `tvd` | `1 - TVD` over top-K proportions plus the other lump |
| datetime | `tvd` | `1 - TVD` over `year_bins` proportions |
| freetext | `length_mean_diff` | 1 minus the absolute difference in mean length, over the larger max length |
| empty, kind mismatch | n/a | `comparable: false`, excluded from aggregates |

Aggregation:

- Since engine #438 a column's `similarity` is `value_similarity` (the formula
  above) times `missing_value_similarity` (`1 - |source null rate - output null
  rate|`). For a DP source the missing score is reported and not applied.
- `marginal.score` is the mean of comparable column similarities.
- `pairwise.score` is the mean of comparable joint similarities.
- `overall_score` is the equal-weight mean of the two. If only one exists it
  passes through.

Prior art: SDV `QualityReport` (KSComplement, TVComplement, equal weighting) and
NIST SP 800-188 section 4.

### Goodness-of-fit extras (`quality/_distribution_gof.py`)

Each comparable column entry may carry `extra_metrics`:

| Key | Columns | Method |
|---|---|---|
| `ks_complement` | numeric, datetime (year grid), freetext (length) | `1 - D`, where D is the largest gap between two binned empirical CDFs evaluated on the union of both sides' bin edges. Tag `ks_complement_binned_cdf`. |
| `chi_cramers_v` | categorical, bool | Two-sample chi-square on a common partition, normalized to Cramer's V, reported as `1 - V`. Tag `chi_square_common_partition_cramers_v`. |

Both are computed from snapshot aggregates, not raw samples, so they are not
`scipy.stats.ks_2samp` or `chisquare`. They are hand-rolled to avoid a runtime
dependency and float differences across BLAS builds.

They are additive. They do not feed `similarity`, `overall_score` or the grade.
Each has its own `comparable` flag and `reason`. When a column's primary score
is comparable but its GOF is not, the report emits a `gof_unavailable` warning.

Known limitation (F-DI0-1): chi-square returns
`insufficient_identifiable_support` for fully disjoint category sets and for
identical constant columns. The primary `tvd` scores both cases correctly.

## Shape-only fidelity (`quality/shape_fidelity.py`)

Value-identity scoring treats a hashed column as fully different from its
source. Shape-only scoring compares sorted, normalized frequency vectors, so a
one-to-one transform scores near 1.

| Kind | Method tag |
|---|---|
| categorical | `freq_vector_sorted_tvd` |
| numeric | `bin_counts_sorted_tvd` |
| freetext | `length_bins_sorted_tvd` |

Output is `quality-shape-fidelity/v1`, embedded in the report as
`shape_fidelity` (on by default, `include_shape_fidelity=True`).

## The report (`quality/report.py`)

`quality-report/v1` fields: `schema_version`, `generated_at`, `job_id`,
`source`, `output`, `diagnostic`, `marginal`, `pairwise`, `overall_score`,
`grade`, `sampled`, `sample_size`, `warnings`, `omissions`, `shape_fidelity`.

Grades from `score_to_grade`: A at 0.95, B at 0.85, C at 0.70, D at 0.50, F
below, `unavailable` when the score is `None`.

- `omissions` lists columns and joints whose primary similarity was not
  comparable, with a reason such as `kind_mismatch`.
- `warnings` are sorted by `(source, code)` and include diagnostic failures and
  `gof_unavailable`.
- `now_iso` pins `generated_at` for byte-stable goldens.

The report is label-free. Joint entries name the column pair, not cell keys.

## Policy (`quality/policy.py`)

`apply_quality_policy(report, policy_config, *, strategy_map=None)` returns
`quality-policy/v1`: `verdict`, `mode`, `violations`. `strategy_map` is
keyword-only.

```yaml
mode: warn
thresholds:
  diagnostic: {required: true}
  overall:    {min: 0.90}
  marginal:   {min: 0.90}
  pairwise:   {min: 0.80}
  columns:    {amount: 0.99}        # or [{column: amount, min: 0.99}]
  shape:
    overall:  {min: 0.90}
    marginal: {min: 0.90}
    pairwise: {min: 0.80}
    columns:  {customer_id: 0.95}
strategy_expectations:       {shuffle: 0.90}
shape_strategy_expectations: {hash: 0.95}
```

`mode` is one of `report`, `warn` or `fail`. An unknown mode falls back to
`report`, which never fails.

Per-column threshold resolution: explicit column override, then the column's
strategy expectation from `strategy_map`, then no check.

Default value-identity expectations are tuned to what `compute_fidelity`
actually reports for each strategy: identity 1.00, shuffle 0.85, date_shift
0.50, bucketize 0.30, generate 0.30, hash 0.05, faker 0.05, redact 0.05.

Two behaviors to know:

- In `report` mode violations are recorded and the verdict is still `pass`.
- Unknown top-level keys are ignored, so a mistyped key silently disables that
  check. A `thresholds` value that is not a mapping raises (`AttributeError`).

## Where the report is produced

| Site | Call | Notes |
|---|---|---|
| `execution/_pipeline_finalize.py` | `run_pipeline(fidelity_report=True)` | Mask tables only, marginal only, `expect_row_parity=True`. Result in `quality_metrics["fidelity_reports"]`. Skipped for generate tables. |
| CLI `decoy report fidelity` | Reads two CSVs, calls `compute_quality_report` then `apply_quality_policy` | Type inference happens on the CSV read, so kinds can differ from the in-run report. No date parsing. |
| `testflight/_distribution.py` | Report plus policy plus extra guards | Constant-collapse and real-coarsening guards the policy does not provide. |
| Platform `api/quality/runner_hook.py` | After a job, best-effort | Local CSV, TSV or Parquet, same-name source and target tables. |

## Tests

| Area | Location |
|---|---|
| Snapshot | `tests/unit/quality/test_snapshot.py` |
| Diagnostic, fidelity, GOF, shape | `tests/unit/quality/test_diagnostic.py`, `test_fidelity.py`, `test_distribution_gof.py`, `test_shape_fidelity.py` |
| Report and policy | `tests/unit/quality/test_report.py`, `test_policy.py` |
| No raw values in reports | `tests/privacy/` |
| Platform contract pin | `tests/test_di0_quality_contract.py` in `decoy-platform` |

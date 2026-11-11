# 13. Fit, fidelity and differential privacy

Status: plan

## What it covers

Measuring a table and generating data shaped like it: the distribution snapshot
(`decoy fit`, `compute_distribution_snapshot`), `statistical` generate columns, fitted
fan-out for `reference` columns, the quality report and policy, the fidelity warning,
and the differential-privacy fit (`decoy fit --epsilon`, `fit_dp_snapshot`).

## Expected behaviour

- A snapshot is `distribution-snapshot/v1`: per-column kind, counts and stats, plus
  optional joints. The same frame and arguments give byte-identical JSON.
- A snapshot written for saving withholds the values of columns the profiler flags as
  sensitive (roadmap row 24f).
- A `statistical` column samples from a snapshot pinned at compile time. A plain
  categorical snapshot holds real labels, so it needs `allow_real_categories: true`.
- `condition_on` samples one column given another, and needs a fitted joint.
- `distribution: fitted` on a `reference` column reproduces the source's children-per-
  parent shape from counts only. It never falls back to uniform draws.
- A quality report holds scores and counts only, never a cell value or a label.
- Measurement never fails a job. The fidelity gate warns below
  `fidelity_warn_threshold` (default 0.8). Policy returns a verdict and the caller acts.
- **DP**: approximate (epsilon, delta) DP over single-column numeric and categorical
  marginals and the row count. Not joints, not datetimes, not free text, not masked
  output. Every mechanism comes from OpenDP; composition from `dp_accounting`. A DP fit
  on an uncertified platform or package set fails before any cell is read.
- With `global_settings.dp` set, every statistical column must use a verified DP
  release, and the composed spend must stay inside the declared budget.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| FIT-01 | Fit the same CSV twice. | Byte-identical snapshot files. |
| FIT-02 | Fit a table with an email column, then search the snapshot for a known email. | Absent. The column is marked as withheld. |
| FIT-03 | `statistical` numeric column from a snapshot, 100,000 rows. | The column's fidelity score is at or above 0.8, the default `fidelity_warn_threshold`, and no fidelity warning is logged. Record the score. |
| FIT-04 | `statistical` categorical column without `allow_real_categories`; then with it. | First refused with `plan.statistical_real_categories_not_allowed`. Second emits labels in roughly source proportions. |
| FIT-05 | `source_column` not in the snapshot; a snapshot with the wrong `schema_version`; a missing file. | `plan.statistical_column_not_in_snapshot`; `plan.statistical_snapshot_schema_mismatch`; `plan.statistical_snapshot_unreadable`. |
| FIT-06 | Generate from a snapshot whose sensitive column values were withheld. | Refused with `plan.statistical_snapshot_values_redacted`. No invented categories. |
| FIT-07 | `condition_on` with a fitted joint of 10 cells; then of 60 cells. | First reproduces the joint. For the second, a warning says the joint was truncated at the cap (25 by default); confirm the cap can be raised at fit. |
| FIT-08 | `reference` with `distribution: fitted`, parents and children at the source's own counts. | Child counts per parent equal the source's degree list exactly. No orphans. |
| FIT-09 | `distribution: fitted` with a snapshot fitted without `--fanout`; with `weights` also set; with `unique: true`. | `plan.reference_fanout_missing`; `plan.reference_fanout_conflict`; `config.generate_unique_conflict`. |
| FIT-10 | Quality report on a masked table, then search the report for any source value or label. | None present. Scores and counts only. |
| FIT-11 | Policy with `mode: fail` and `overall: {min: 0.99}` on a low-scoring run; then `mode: report`. | First gives a failing verdict with violations. Second records the violations and still returns `pass`. |
| FIT-12 | `decoy fit --epsilon 1.0` on a default (uncertified) install. | Ends with a plain-language message and a next step, exit code 3, before any cell is read. |
| FIT-13 | On the certified stack: generate under `global_settings.dp` from a non-DP snapshot; with `condition_on`; over the declared budget. | Each rejected at compile. The last with `plan.dp_budget_exceeded`. |

## Open points

- A quality-policy file with a mistyped top-level key silently disables that check, and
  an unknown `mode` falls back to `report`, which never fails. Both make a policy look
  enforced when it is not.
- The fidelity warning is logged through the `decoy_engine` logger, which has only a
  `NullHandler` by default. The docs say the CLI does not surface it in the run summary.
  Check whether row 24o (GEN-CLI) changed that.
- Numeric columns under 100 values, or older snapshots, sample uniformly within 10
  equal-width bins. One outlier stretches the bins (rows 24u, 24v). Record current
  behaviour with a skewed column.
- Only year bins are fitted for datetimes. Month, weekday and time-of-day patterns are
  not reproduced.
- DP utility is not guaranteed and the certified stack is Linux x86-64 on one exact
  CPython and package set. FIT-13 can only run there.

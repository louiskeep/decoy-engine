# Statistical generation

[Back to the developer reference](index.md)

How a `type: statistical` generate column turns a snapshot into values.

## Lifecycle

1. **Fit.** `compute_distribution_snapshot` (exact) or `fit_dp_snapshot` (DP)
   writes a `distribution-snapshot/v1` JSON file.
2. **Compile.** The plan compiler reads each referenced snapshot once, pins its
   bytes and SHA-256 digest, and validates each column with `load_spec`. With
   `global_settings.dp` present, `plan/_checks_dp.py` also verifies the DP
   release and the budget.
3. **Generate.** `_statistical_column.py` derives a column seed and calls
   `sample_column`.
4. **Warn.** `_fidelity_gate.py` scores the generated columns against the
   snapshot and logs a warning below the threshold.

Generation never reopens the snapshot path. It uses the bytes pinned at
compile time.

## Column config

| Key | Required | Meaning |
|---|---|---|
| `type: statistical` | yes | |
| `snapshot_file` | yes | Path to the snapshot JSON. |
| `source_column` | no | Snapshot column to sample. Defaults to `name`. |
| `allow_real_categories` | for exact categorical | Consent to emit real labels. |
| `condition_on` | no | A previously declared column to condition on. Needs a fitted joint. |
| `other_mode` | no | `redistribute` (default) or `emit` (outputs `__other__`). |
| `high_cardinality` | no | Sample from a full-vocabulary categorical fit. Requires `allow_real_categories`. |

## Spec validation (`generation/statistical/_spec.py`)

`load_spec` answers every "is this config coherent with this snapshot"
question and returns a frozen `StatisticalSpec`. Errors are
`StatisticalSpecError` with a reason code.

| Condition | Code |
|---|---|
| Snapshot missing or not JSON | `statistical_snapshot_unreadable` |
| Wrong `schema_version` | `statistical_snapshot_schema_mismatch` |
| `source_column` not in the snapshot | `statistical_column_not_in_snapshot` |
| Exact categorical without consent | `statistical_real_categories_not_allowed` |
| Degenerate histogram or empty `top_values` | `statistical_stats_degenerate` |
| `condition_on` column not yet generated | `statistical_condition_column_unavailable` |

The consent gate has one exemption: `dp_verified=True`, passed in by the
compiler after it verified the snapshot as a `dps-marginal/v3` release. The
spec layer never infers this from the snapshot's own `dp` key.

`carrier` on the spec is `None` for every exact snapshot and `number`, `text`
or `flag` for a verified DP column. The sampler branches on it only to decode
`flag` tokens to Python `bool`.

## Samplers (`generation/statistical/_sample.py`)

| Kind | Method | Source pattern |
|---|---|---|
| numeric | Pick a bin by cumulative `bin_counts`, then uniform within the bin. Integer dtypes are rounded and clamped. | Histogram inverse-CDF (Devroye 1986) |
| categorical | Weighted draw over `top_values`. `other_count` is redistributed or emitted. | |
| datetime | Weighted year from `year_bins`, uniform timestamp in the year, clamped to `[min, max]`. | |
| freetext | Length from the length histogram, filled with lorem tokens. No source tokens exist in the snapshot. | |
| conditional | Draw from the joint row for the parent value; fall back to the marginal when the parent value has no cells. | Sequential conditional sampling (synthpop) |

The numeric sampler uses only `bin_edges` and `bin_counts`. `quantiles`, `mean`
and `std` are not used for sampling.

## Determinism and seeds

One `random.Random` is reseeded per row with `col_seed + start + i`. Row `i`
depends only on `(col_seed, i, spec, parent value)`, so any chunking gives
byte-identical output.

`col_seed` comes from `GenDeriveContext.for_column(...).base_int("np")`. Its
root is derived from `strategy_config_fingerprint(column_config)`: the SHA-256
of the column's canonical config, with `snapshot_file` replaced by the
snapshot's content digest.

### Known defect: identical configs share a seed

`strategy_config_fingerprint` excludes `name` (along with `null_probability`,
`determinism`, `pooled`). Two statistical columns that differ only by `name`
have the same fingerprint, the same `col_seed`, and therefore the same uniform
draw on every row. Each marginal is correct; the columns are rank-locked.

Measured on two independent source columns (correlation 0.02): generated
correlation 0.97. With an explicit, distinct `source_column` on each column the
fingerprints differ and the correlation is 0.02.

The `name` exclusion is deliberate (renaming a column must not change its
values), so the fix is not simply to hash `name`. Options are in
[Extending](extending.md#open-defects).

## The fidelity warn-gate (`generation/_fidelity_gate.py`)

After a generate table builds, `score_generated_fidelity` snapshots the
generated statistical columns with the same snapshot function and compares
them to the fitted snapshot with `compute_fidelity`. `warn_on_low_fidelity`
logs when the score is under `global_settings.fidelity_warn_threshold`
(default 0.8).

It is warn-only by design. It never raises, never mutates the table and never
changes output bytes. A correct sampler can still score low: a `condition_on`
column that falls back to the marginal, or `other_mode: emit`.

The CLI does not currently surface this warning in its run summary.

## Related gates

- `quality/_retention_gate.py`: fit-time warning when a column fell to
  `freetext` at the cutoff, or top-K truncation kept less than
  `categorical_retention_warn_threshold` of the mass.

## What the design cannot represent

| Limit | Reason |
|---|---|
| Sub-bin numeric detail, decimal precision | Uniform draw within 10 equal-width bins. |
| Month, weekday or time-of-day patterns | Only `year_bins` are fitted. |
| Null rates | `null_count` is stored but the samplers do not emit nulls. |
| Categories beyond top-K | Collapsed into `other_count`. |
| Dependence between columns | Only a fitted pair with `condition_on`; one parent per column. |
| Cross-table fan-out | A snapshot describes one frame. `reference` columns do not read fitted key counts. |

## Tests

`tests/unit/generation/test_statistical.py`, `test_fidelity_gate.py`,
`testflight/_distribution_generate.py`, and the CLI's
`tests/e2e/test_fit_command.py`.

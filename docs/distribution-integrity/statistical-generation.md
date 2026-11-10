# Statistical generation

Status: current

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
| numeric | Pick a bin by cumulative `bin_counts`, then uniform within the bin. Integer dtypes are rounded and clamped. With a `cell_format` block, rounded to the fitted `decimal_places`. | Histogram inverse-CDF (Devroye 1986) |
| categorical | Weighted draw over `top_values`. `other_count` is redistributed or emitted. | |
| datetime | Weighted year from `year_bins`, uniform timestamp in the year, clamped to `[min, max]`. With a `cell_format` block, floored to the fitted `resolution` (day resolution emits `datetime.date`). | |
| freetext | Length from the length histogram, filled with lorem tokens. No source tokens exist in the snapshot. | |
| conditional | Draw from the joint row for the parent value; fall back to the marginal when the parent value has no cells. | Sequential conditional sampling (synthpop) |

The numeric sampler uses only `bin_edges` and `bin_counts`. `quantiles`, `mean`
and `std` are not used for sampling.

When a column entry carries `cell_format` (`statistical/_cell_format.py`, engine
#434 and #436), the sampler also draws a null with probability
`null_count / row_count` and, for freetext, a blank with the fitted
`blank_count` share. A column that sets `null_probability` skips the fitted
null draw (`apply_nulls` is false), so one null mechanism acts on a column.
Snapshots without the block, every DP snapshot included, generate the same
bytes as before. `compute_distribution_snapshot(..., cell_format=True)` and
`profiling._snapshot_values.distribution_snapshot_document` write the block;
`decoy fit` in the CLI does not yet (roadmap row 24l).

## Determinism and seeds

One `random.Random` is reseeded per row with `col_seed + start + i`. Row `i`
depends only on `(col_seed, i, spec, parent value)`, so any chunking gives
byte-identical output.

`col_seed` comes from `GenDeriveContext.for_column(...).base_int("np")`. It
derives from the job seed (or master key and label), the strategy config
fingerprint (the SHA-256 of the column's canonical config, with
`snapshot_file` replaced by the snapshot's content digest) and the column
identity, table name plus column name (engine #421). Renaming a column or
moving it to another table changes its values.

### Fixed: identical configs shared a seed

Before engine #421 the identity was not part of the seed, so two columns with
identical settings drew the same values on every row (QA finding C1). A
re-test on engine `5c24a75e` found no repeated rows for statistical or `pyint`
columns and a correlation of -0.01 for two independent statistical columns.

## The fidelity warn-gate (`generation/_fidelity_gate.py`)

After a generate table builds, `score_generated_fidelity` snapshots the
generated statistical columns with the same snapshot function and compares
them to the fitted snapshot with `compute_fidelity`. `warn_on_low_fidelity`
logs when the score is under `global_settings.fidelity_warn_threshold`
(default 0.8).

It is warn-only by design. It never raises, never mutates the table and never
changes output bytes. It logs through the `decoy_engine` logger only. Since
engine #426 there is no default log file and #429 attaches a `NullHandler`, so
the warning is lost unless the host configures logging. A correct sampler can still score low: a `condition_on`
column that falls back to the marginal, or `other_mode: emit`.

The CLI does not currently surface this warning in its run summary.

## Related gates

- `quality/_retention_gate.py`: fit-time warning when a column fell to
  `freetext` at the cutoff, or top-K truncation kept less than
  `categorical_retention_warn_threshold` of the mass.

## What the design cannot represent

| Limit | Reason |
|---|---|
| Sub-bin numeric detail | Uniform draw within 10 equal-width bins. Decimal places are kept only with a `cell_format` block. |
| Month, weekday or time-of-day patterns | Only `year_bins` are fitted. A `cell_format` block keeps the resolution, not the pattern. |
| Null rates without `cell_format` | A snapshot without the block emits no fitted nulls; only `null_probability` does. |
| Categories beyond top-K | Collapsed into `other_count`. |
| Dependence between columns | Only a fitted pair with `condition_on`; one parent per column. |
| Cross-table fan-out | A snapshot describes one frame. `reference` columns do not read fitted key counts. |

Measured consequences of these limits, with the numbers, are in
[Extending](extending.md#open-defects) and in the QA record it cites. The
largest: a single outlier stretches the equal-width bins and ruins a numeric
column, and joint tables are capped at 25 cells with a silent fallback.

## Tests

`tests/unit/generation/test_statistical.py`, `test_fidelity_gate.py`,
`testflight/_distribution_generate.py`, and the CLI's
`tests/e2e/test_fit_command.py`.

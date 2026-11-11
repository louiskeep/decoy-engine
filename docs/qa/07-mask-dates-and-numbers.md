# 07. Date and number strategies

Status: plan

## What it covers

`date_shift`, `bucket_perturb`, `bucketize`, `top_code` and `windowed_date`, plus the
`preserve_values` option for sentinel dates.

## Expected behaviour

- **date_shift**: moves each date by a per-value offset within `min_days` and
  `max_days` (defaults -365 and 365; reversed bounds are swapped). Same date gives the
  same shifted date within a namespace.
- **bucket_perturb**: moves each date to a keyed position inside its own ISO week,
  calendar month or quarter. The date never leaves its bucket.
- **bucketize**: rounds numbers into fixed-width bins. `format` is `lower`, `range` or
  `midpoint`. Presets such as `by_decade` name a width.
- **top_code**: values above a cap become one label (`hipaa_age` preset: over 89 becomes
  `90+`). Optional floor and under-label. In-range values render as canonical strings
  (`67`, never `67.0`), the same on every route.
- **windowed_date**: a date at `anchor + offset`, the offset drawn from
  `[min_days, max_days]` with a `uniform`, `early` or `late` distribution.
- **preserve_values**: listed ISO dates pass through unchanged. `date_format` is
  required with it.
- A non-null cell that cannot be parsed is a row error (`format_error`). The job fails
  by default, or the row is quarantined if that trigger is enabled. The source value is
  never silently kept.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| DATE-01 | `date_shift` with defaults on ISO dates. | Every output is within 365 days of its source. Equal dates shift equally. Rerun is identical. |
| DATE-02 | `date_shift` with `min_days: 30`, `max_days: -30`. | Bounds are swapped. All offsets fall in [-30, 30]. |
| DATE-03 | `date_shift` on a column with one cell `not-a-date`, quarantine off. | Job fails with `RowErrorsFailedError` naming table, column and count. The bad cell is not in any output. |
| DATE-04 | Same, with `quarantine.triggers: [format_error]`. | Job completes. The row is absent from the output and present in the quarantine file. |
| DATE-05 | `bucket_perturb` with `bucket: month` across month ends and a leap-year February. | Every output is in the same calendar month as its source and is a real date. |
| DATE-06 | `bucket_perturb` with `bucket: fortnight`; then `date_format: "%H:%M:%S"`. | First fails with `bucket_perturb_invalid_config` before any row. Second is refused at compile with `bucket_perturb_date_format_unsupported`. |
| DATE-07 | `preserve_values: ["9999-12-31", "2099-01-01"]` on a column holding both. | Both pass through unchanged; other rows are masked. The preserved count is reported with no values. |
| DATE-08 | `preserve_values` with no `date_format`; with `group_by`; on the child side of a relationship. | Each refused at compile (`date_preserve_values_with_group_by_unsupported`, `date_preserve_values_unsupported_fk_shape` for the last two). |
| DATE-09 | `bucketize` with `width: 10` in each `format`, on ages including nulls and a text cell. | 37 becomes `30`, `30-39` or the midpoint. Nulls stay null. The text cell is a row error, not a kept value. |
| DATE-10 | `top_code` with `preset: hipaa_age` on ages 0 to 110. | 0 to 89 render as their own integers; 90 and above render as `90+`. Output is one string type. |
| DATE-11 | `top_code` with an unknown preset; with `floor >= cap`; under a `when:` gate. | Each refused at compile. |
| DATE-12 | `windowed_date` with `min_days: 1`, `max_days: 30` and each distribution. | Every output is 1 to 30 days after its anchor. `early` skews near 1, `late` near 30. Rerun is identical. |

## Open points

- The `date_shift` section of `docs/strategies.md` says "null and unparseable values are
  left as-is", while the row-error section says an unparseable cell now fails or
  quarantines. DATE-03 assumes the second. Fix the stale sentence.
- `bucketize` likewise says "non-numeric and null values pass through"; DATE-09 assumes
  a non-numeric cell is a `format_error`.
- `bucket_perturb` with no detectable date format passes the column through with only a
  log warning. That is unmasked output; confirm whether V1 accepts it.
- `windowed_date` and `top_code` have no section in `docs/strategies.md`. Expectations
  here come from the module docstrings.

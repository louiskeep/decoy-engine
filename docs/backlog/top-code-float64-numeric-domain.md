# top_code operates in the float64 numeric domain (non-integer columns)

Status: known limitation, by design. Not a leak.

## What

`top_code` (HC-3b) is numeric top-/bottom-coding. It coerces the target column
with `pd.to_numeric` and compares against `cap`/`floor`. For an **integer** column
(genuine `int64`, or the nullable `Int64` produced by top_code's lossless FK-safe
ingest) every value is exact at any magnitude, so the comparison and the rendered
string are exact.

For a **non-integer** column -- a genuine float column, or an `object`/string/
`Decimal` column whose values `pd.to_numeric` coerces through `float64` -- values
live in the `float64` numeric domain, which represents integers exactly only up to
`2**53`. Two consequences follow, both inherent to that domain:

1. **Sub-ULP decimal distinctions vanish.** `Decimal("89.0000000000000001")`
   coerces to exactly `89.0` in `float64` -- the seventeenth significant digit is
   below double precision. A `hipaa_age` (cap=89) column therefore treats it as
   in-range (renders `"89"`), not as `"90+"`. This is **not** a leak: the value is
   genuinely equal to `89.0` once it enters the numeric domain top_code operates
   in; no float64-based comparison anywhere could separate them. (Assessed and
   conceded across the Codex R2/R3 cross-model review.)

2. **Values beyond float64's exact-integer range are quarantined, not corrupted.**
   A coerced value with magnitude `>= 2**53` on a non-integer column cannot be
   compared or rendered exactly (distinct source values would collapse onto the
   same double). Rather than emit a corrupted or ambiguous value, the handler
   records a per-row `format_error` `RowError` and leaves the original in place, so
   the quarantine gate removes it (`execution/_strategies/_top_code.py`,
   `inexact_mask`). In practice this only fires on pathological out-of-range
   magnitudes (integer strings beyond `uint64`, or large `Decimal`/float values);
   integer ages -- the HIPAA use case -- never reach it.

## Why not "fix" it

Making sub-ULP decimals or `>2**53` values exact would require running top_code in
an arbitrary-precision `Decimal` domain end to end, including the chunk-safe render
and the pandas/polars parity guarantee. That is a large change for a shape that
does not occur in the motivating use case (integer ages/scores) and whose "wrong"
answer (`89.0000000000000001` -> `"89"`) is not actually a disclosure. The exact
path (integer columns, `|value| < 2**53`) covers every realistic top_code input.

## If this ever matters

The correct route would be to detect a genuinely decimal/high-precision source
column at profile time and either (a) route it through a `Decimal`-domain top_code
variant, or (b) reject it at compile with guidance to pre-round. Neither is built;
both are larger than the HC-3b scope.

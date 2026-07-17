# top_code non-integer column domain (OPEN design decision)

Status: **OPEN** — escalated for a scope decision. The HIPAA age>89 use case
(integer ages) is correct and complete; this concerns applying top_code to
NON-integer columns.

## Context

`top_code` (HC-3b) is numeric top-/bottom-coding. It coerces the target column
with `pd.to_numeric` and compares against `cap`/`floor`. Two column classes
behave differently:

- **Integer columns** (genuine `int64`, or the nullable `Int64` produced by
  top_code's lossless FK-safe ingest): every value is exact at any magnitude, so
  the comparison and the rendered string are exact. This is the HIPAA age path.
- **Non-integer columns** (a genuine `float` column, or an `object`/string/
  `Decimal` column that `pd.to_numeric` coerces through `float64`): values enter
  the `float64` numeric domain, which represents integers exactly only up to
  `2**53` and cannot hold sub-ULP decimal distinctions.

## The open finding (Codex R4 BLOCKER)

For an `object`/`Decimal` column whose SOURCE values are exact, top_code's own
choice to coerce through `float64` is what loses precision. Example:
`Decimal("89.0000000000000001")` with `preset: hipaa_age` (cap=89) is genuinely
`> 89`, but coerces to exactly `89.0` and renders in-range `"89"` instead of the
`"90+"` tail label. Codex classifies this as a disclosure (a true tail value
rendered in-range) because the source was exact until top_code coerced it. The
counter-view: no real dataset stores an age as `"89.0000000000000001"`, and for
a genuine `float64` source the value was never representable as distinct from
`89.0`, so the coercion introduces nothing.

## Decision needed (recommendation: Option A)

- **Option A (recommended) — scope top_code to numeric-typed columns.** Reject
  `object`/string source columns at runtime (fail closed with a coded
  `StrategyError`), so top_code only ever operates on already-typed numeric
  columns. Eliminates the exact-source-lost-to-float64 class by construction
  (an object column of decimal strings is an upstream typing problem the ETL
  should fix). Fully covers the HIPAA integer-age use case. Genuine `float64`
  columns keep working (their sub-ULP identities are inherent to the data, not a
  top_code defect). Cost: an object column of numeric strings that top_code
  currently coerces would now be rejected instead of processed cell-by-cell —
  a behavior change to weigh.
- **Option B — full `Decimal`-domain top_code.** Run the comparison and the
  chunk-safe render in arbitrary-precision `Decimal` for object columns. Exact,
  but a meaningful rewrite that must preserve the pandas/polars parity and
  chunk-byte-identity guarantees, and it still cannot make a genuine `float64`
  source exact.
- **Option C — accept as a documented limitation.** Keep float64 coercion;
  state that top_code operates in the float64 numeric domain and sub-ULP decimal
  distinctions are not preserved. Lowest effort; leaves Codex's BLOCKER standing.

The HC-3(b) spec is "age>89" (integers), so Option A loses nothing for the
stated scope while collapsing the entire float64-domain finding class. Pending
the owner's call before merge.

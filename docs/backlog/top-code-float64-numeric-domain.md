# top_code non-integer column domain — RESOLVED (Option A′)

Status: **RESOLVED** 2026-07-17 via Option A′ (Codex cross-model decision,
product-owner-delegated). Kept here as the rationale record.

## The finding

`top_code` (HC-3b) is numeric top-/bottom-coding. For an INTEGER column (genuine
`int64` or the lossless nullable-`Int64` ingest) every value is exact at any
magnitude. The open concern was NON-integer columns: `top_code` coerced them with
`pd.to_numeric` (float64), and for an `object`/string/`Decimal` column whose
SOURCE values are exact, that coercion could collapse a tail value one ULP above
the cap — `Decimal("89.0000000000000001")` → `89.0` → rendered in-range `"89"`
instead of the `"90+"` tail label (a disclosure introduced by top_code's own
coercion, since the source was exact). A genuine `float64` column was never exact
to begin with, so that case is inherent, not a top_code defect.

## Resolution — Option A′ (`execution/_strategies/_top_code.py`)

Branch on the SOURCE dtype:

- **Integer / float dtype**: unchanged. Integers are exact; a genuine float lives
  in its inherent float domain (a fractional float in-range renders as itself).
- **Object / string / Decimal dtype** → `_classify_object_column`, which parses
  every non-null cell EXACTLY via `Decimal(str(cell))` (never float64):
  - non-numeric cell (`"abc"`, bool, non-finite) → per-row `format_error`
    RowError, original kept (unchanged dirty-column behavior — a mostly-numeric
    integer-age column with a few placeholders still works cell-by-cell);
  - integral value (native int, integral float, integral Decimal, integer-valued
    string, incl. `"89.0"`) → compared exactly against cap/floor and, in-range,
    rendered from its exact Python int (no `.0`, no large-int collapse — a huge
    in-range negative with no floor now renders exactly instead of via a
    float64-collapsed double);
  - **fractional** value (a decimal / non-integral float, e.g.
    `"89.0000000000000001"`) → the WHOLE column fails closed BEFORE any mutation
    with `top_code_non_integral_object_column`. Routing it through float64 is the
    only way top_code could misread a sub-ULP tail value as in-range, so we
    refuse the column rather than take that path. A genuine decimal column
    belongs in a float dtype or should be pre-rounded.

This closes the disclosure by construction, covers the HIPAA integer age>89 use
case, keeps genuine float columns working, and preserves per-cell quarantine for
dirty integer columns — strictly better than plain "reject all object columns".

Compile-time note: `plan/_checks_top_code.py` is config-only (no source data), so
it cannot see column dtype/values; the A′ rejection is necessarily a runtime
fail-closed check. Regression coverage: `TestObjectColumnExactParse` in
`tests/unit/execution/test_top_code.py`.

# Warehouse export fidelity matrix (S0)

Record for the Pattern-E reuse-proof slice (plan:
`docs/plans/2026-10-23-warehouse-s0-pattern-e-fidelity-reuse-proof.md`).

**What this proves.** A cloud warehouse's native Parquet export (Redshift
`UNLOAD`, Snowflake `COPY INTO`), read through Decoy's existing file/S3 Parquet
input, masks through the **unchanged** engine seam. For each representative
export type below, the row's `disposition` is asserted by a test in
`tests/integration/test_warehouse_export_fidelity.py`. So the Pattern-E "reuse
the seam" claim is evidence-backed, not assumed.

**Source of truth / no drift.** This table is generated from
`tests/integration/_warehouse_fixtures.py::WAREHOUSE_TYPES` (one place per
fact). Regenerate after changing that tuple:

```
python -I - <<'PY'
import sys; sys.path.insert(0, "tests")
from integration._warehouse_fixtures import WAREHOUSE_TYPES
for wt in WAREHOUSE_TYPES:
    print(f"| {wt.warehouse} | {wt.source_type} | {wt.encoding} | {wt.disposition} | {wt.confidence} | {wt.note} |")
PY
```

**Dispositions.**
- `faithful` — passthrough output is byte-identical (exact Arrow type AND values).
- `faithful-value` — values (and, for decimals, scale) preserved; the logical
  type metadata is value-derived through the pandas seam (see findings).
- `gap` — not carried losslessly; reject (or warn) at platform admission. No
  representative *passthrough* type is a blanket gap; the one real gap is a
  conditional one (null-bearing integer ≥ 2**53), pinned by its own test.

## Matrix

| Warehouse | Source type | Export encoding | Disposition | Confidence | Note |
|---|---|---|---|---|---|
| redshift | SMALLINT | int16 | faithful-value | encoding-confirmed | null-bearing integer widens to float64 (values preserved below 2**53). |
| redshift | INTEGER | int32 | faithful-value | encoding-confirmed | null-bearing integer widens to float64 (values preserved below 2**53). |
| redshift | BIGINT | int64 | faithful-value | encoding-confirmed | null-bearing integer widens to float64; magnitudes >= 2**53 lose precision (gap); null-free stays int64. |
| redshift | DECIMAL(18,2) | decimal128(18,2) | faithful-value | encoding-confirmed | decimal precision is value-derived through the seam (scale kept). |
| redshift | DECIMAL(38,0) | decimal128(38,0) | faithful-value | encoding-confirmed | decimal precision is value-derived through the seam (scale kept). |
| redshift | REAL | float32 | faithful | encoding-confirmed |  |
| redshift | DOUBLE PRECISION | float64 | faithful | encoding-confirmed |  |
| redshift | BOOLEAN | bool | faithful | encoding-confirmed |  |
| redshift | VARCHAR | string(utf8) | faithful | encoding-confirmed |  |
| redshift | DATE | date32 | faithful | encoding-confirmed |  |
| redshift | TIMESTAMP | timestamp(us, naive) | faithful | encoding-confirmed |  |
| redshift | TIMESTAMPTZ | timestamp(us, UTC) | faithful | encoding-confirmed |  |
| redshift | SUPER | string(json) | faithful | encoding-assumed | SUPER exports as a JSON string; masking INSIDE the JSON is a separate capability (text_mask/nested), out of S0 scope. |
| redshift | VARBYTE | binary | faithful | encoding-assumed |  |
| snowflake | NUMBER(38,0) | decimal128(38,0) | faithful-value | encoding-confirmed | decimal precision is value-derived through the seam (scale kept). |
| snowflake | NUMBER(18,4) | decimal128(18,4) | faithful-value | encoding-confirmed | decimal precision is value-derived through the seam (scale kept). |
| snowflake | FLOAT | float64 | faithful | encoding-confirmed |  |
| snowflake | VARCHAR | string(utf8) | faithful | encoding-confirmed |  |
| snowflake | BOOLEAN | bool | faithful | encoding-confirmed |  |
| snowflake | DATE | date32 | faithful | encoding-confirmed |  |
| snowflake | TIMESTAMP_NTZ | timestamp(ns, naive) | faithful | encoding-assumed | Snowflake TIMESTAMP_NTZ exports at up to ns precision. |
| snowflake | TIMESTAMP_TZ | timestamp(us, UTC) | faithful | encoding-assumed |  |
| snowflake | VARIANT | string(json) | faithful | encoding-assumed | VARIANT/OBJECT/ARRAY serialize to a JSON string on Parquet export. |
| snowflake | BINARY | binary | faithful | encoding-assumed |  |
| snowflake | NUMBER(40,0) | decimal256(40,0) | faithful-value | encoding-confirmed | passthrough preserves values (decimal256 may narrow to decimal128 to fit the data); numeric MASKING of >38 precision to be qualified in S2 (postgres-input pins a 38-digit ceiling). |

## Findings (what S1/S2 must act on)

1. **The reuse win is real.** Every representative Redshift/Snowflake export
   encoding carries through the unchanged seam with values preserved, and FK
   referential integrity is preserved on a relational warehouse-shaped fixture.
   "Warehouse support" via Pattern-E is platform binding/UX, not new engine code.

2. **Null-bearing integers widen to float64** through the pandas seam.
   - Null-free integer columns keep their exact type (`int16/int32/int64`).
   - Null-bearing integers below 2**53: widened to float64, values preserved.
   - **Null-bearing integers ≥ 2**53: float64 silently loses precision — a
     real GAP** (e.g. a nullable BIGINT surrogate key). This matches the known
     engine int+null→float64 issue (platform `v2-candidate-plan.md` items
     11–12). **S1 action:** reject, or loudly warn on, a nullable large-magnitude
     integer warehouse column at admission; do not silently emit it.

3. **Decimal logical precision is value-derived.** Values and scale are
   preserved, but the output's decimal precision narrows to the minimum that
   fits the data (a `DECIMAL(18,2)` of small values emits `decimal128(6,2)`;
   `decimal256` may narrow to `decimal128`). No value is lost. **S1 action:**
   document that the output schema's decimal precision is data-derived so a
   downstream load is not surprised by a tighter precision.

4. **Semi-structured types** (`SUPER`/`VARIANT`/`OBJECT`/`ARRAY`) arrive as JSON
   strings and pass through faithfully as opaque strings. Masking *inside* the
   JSON is a separate capability (`text_mask`/nested), out of S0 scope.

5. **Encoding confidence.** Rows marked `encoding-assumed` reproduce the
   *expected* export encoding but have not been confirmed against a real
   warehouse export. **S2 action:** confirm each against an actual
   `UNLOAD`/`COPY INTO` output and promote to `encoding-confirmed` (or correct).

## Honesty notes (per development-loop VERIFY)

- This slice changed **no `src/`**; it adds tests, fixtures, and this record.
  Test-strength mutation measurement targets changed source units, so it does
  not apply here — there is no production code to grade. The evidence is the
  passing, observation-derived assertions above (83 tests).
- `>38`-precision **numeric masking** (as opposed to passthrough) is not proven
  here; it is deferred to S2.

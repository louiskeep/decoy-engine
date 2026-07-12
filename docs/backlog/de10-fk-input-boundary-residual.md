# DE-10 follow-up: FK input-boundary residual (null-bearing integer FK child key > 2^53)

**Status:** scoped residual, pinned by test; low-probability exposure; deferred pending broader FK-child input refactoring.

## What DE-10 closed

Commit `56725de` (DE-10 adversarial-review remediation) established one lossless FK output-typing contract across all three routes (full-frame, sequential, out-of-core). The problem it solved:

- **Before DE-10:** the full-frame and sequential (pandas) routes assigned masked FK keys to the child frame via raw-list assignment, letting pandas infer the dtype. A matched float parent value beside a preserved/warned orphan integer key beyond exactly-representable float precision (> 2^53) caused pandas to widen the column to float64, silently rounding the key (e.g. `9007199254740993` to `9007199254740992.0`). The out-of-core route already rejected this shape. **Route choice decided whether a large key survived**, and the pandas coercion was the oracle.

- **After DE-10:** the FK output column is built through the same Arrow inference the out-of-core route uses. A matched-float plus orphan-int-past-2^53 mix now fails closed with `ExecutionError(code="out_of_core_fk_key_dtype_unsupported")` across all routes. All-integral columns are materialized `int64`-exact. Route choice no longer changes correctness for the output boundary.

## The residual: input-boundary rounding

A null-bearing integer FK **child** column with a key > 2^53 is rounded at the pandas INPUT boundary, **before FK resolution runs**, so DE-10's output-boundary fix cannot recover the value:

1. `PandasExecutionAdapter.run()` and `run_sequential()` load each source with `pa.Table.to_pandas()`, which widens an `int64`+null column to `float64` (numpy has no null-bearing int type).
2. The `reject_null_bearing_int` guard in the masking path deliberately **exempts FK children** (they are resolved via the FK edge, not masked), so this widening is never gated.
3. By the time `_resolve_fk_node` reads the child FK column to resolve keys, the integer key `9007199254740993` is already `9007199254740992.0` (silent rounding).
4. The out-of-core route never touches pandas and keeps the `int64` value exact, so the routes still diverge for this specific shape (null-bearing int child FK with key > 2^53).

**Result:** route choice changes correctness for the input boundary (pandas/sequential silently round; out-of-core preserves), creating a scoped referential-integrity drift.

## Exposure profile

- **Probability:** low. Requires **both** a null in the FK child column **and** an integer FK key > 2^53. Realistically only 63-bit integer ID systems (e.g. Snowflake AUTOINCREMENT) carry keys in this range; smaller IDs with a null in the same column are harmless.
- **Impact:** high when hit. Silent divergence means the same job produces different child-key values across routes, a data-integrity violation caught only via hash-mismatch audits or cross-run comparison.

## Why deferred

Fixing the input boundary is a broader change than DE-10's output-boundary contract:

1. **Selective per-column lossless loading:** requires routing individual source columns through a lossless-conversion path that `pa.Table.to_pandas()` does not provide. Polars has no null-bearing int type either; a null-bearing FK-child integer column needs special handling in both adapters.
2. **Interaction with `reject_null_bearing_int`:** the guard currently exempts FK children. Fixing the input means either exempting them from both eager-load widening (new config) or removing the exemption and handling nulls in FK resolution (riskier).
3. **Blast radius:** touches source loading, FK-child handling, masking-path gating, and possibly the plan compiler's awareness of FK children.

This is properly sized as a coordinated follow-up, not a band-aid on DE-10.

## Pinned by tests

The current divergence is explicitly pinned so it cannot silently widen:

- **`test_de10_nullable_int_child_key_input_boundary_drift_scoped()`** (`tests/parity/test_out_of_core_fk_parity.py::1151`): parametrized over `OrphanPolicy` (PRESERVE, WARN, REMAP, FAIL). Creates a parent with an integer key `9007199254740993` and a child FK column with `[9007199254740993, None]`. Asserts:
  - Out-of-core preserves the key exactly: `[9007199254740993, None]`.
  - Full-frame and sequential both produce the drifted value: `[9007199254740992.0, None]` (rounding at input).
  - The test will **flip assertions** (and `SEMANTIC_DIFFERENCES.md` will be updated) once the input-boundary fix lands.

- **`tests/parity/SEMANTIC_DIFFERENCES.md` "Scoped residual" note** (section "FK output typing"): documents that DE-10 closed the output boundary but the input boundary remains scoped, and points to the test pin.

## Follow-up roadmap

When implementing the input-boundary fix:

1. **Selective per-column source loading:** add a protocol or adapter method that allows FK-child columns to load through a lossless path (e.g. keep them as Arrow `int64`+null without a pandas round-trip, or load them late after masking so pandas doesn't widen them).
2. **Update `reject_null_bearing_int`:** clarify whether the exemption for FK children is lifted (if they are now lossless-loaded) or preserved (if the conversion happens later).
3. **Flip the test assertions:** `test_de10_nullable_int_child_key_input_boundary_drift_scoped` will assert that all routes produce `[9007199254740993, None]`.
4. **Update SEMANTIC_DIFFERENCES.md:** change the "Scoped residual" note to reflect that the input boundary is now lossless too.

## Coordinated error-code rename opportunity

**Additional recommendation (separate implementation):** The error code `out_of_core_fk_key_dtype_unsupported` is now raised by full-frame and sequential routes too, so the `out_of_core_` prefix is misleading. Consider a coordinated rename to `fk_key_dtype_not_representable` or `fk_key_precision_loss_rejected` (route-neutral) across:
- Approximately 5 `raise` sites (`_pandas_adapter.py`, `_join.py`, `_mask_group_*.py`)
- Approximately 16 test references
- Documentation references (this doc and SEMANTIC_DIFFERENCES.md)

This is a cosmetic change with no behavioral impact and can ship any time after DE-10 is merged.

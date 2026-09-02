# Polars hash: route through the shared Arrow kernel for cross-adapter parity

Status: plan (authored 2026-09-02, Opus; awaiting Codex plan-gate before build).
Held target branch `feat/native-phase3`; merges with the Phase-4 bundle. This is
an ENGINE correctness fix (cross-adapter hash parity), foundational for the
hash-only chunked-FK cascade fix (`2026-09-02-chunked-fk-cascade-safety.md`),
which is unsound until this lands. Greenlit by Cam ("fix Polars hash kernel").

## 1. The bug

The `hash` masking strategy is not byte-identical between the pandas and Polars
execution adapters for sub-microsecond-precision values. Both adapters call the
SAME canonicalizer (`generation/pool/_canonicalize.py::_canonicalize_source`,
re-exported as `kernel/_canonicalize.py::canonicalize_derive_source`); the
divergence is the Python value handed to it:

- **Pandas** (`execution/_strategies/_hash.py:52-53`): `pandas_column_to_kernel_input`
  builds `pa.array(col, from_pandas=True)`, and `hash_array` iterates its
  `.to_pylist()`, which for a `timestamp[ns]` array yields a **`pandas.Timestamp`**
  (a `datetime` subclass that preserves nanoseconds).
- **Polars** (`execution/polars/_strategies/_hash.py:47`): `source.to_list()`
  yields a stdlib **`datetime.datetime`** (microsecond max, nanoseconds
  truncated).

`_canonicalize_source` hits the same `isinstance(value, datetime)` branch
(`_canonicalize.py:98-111`) and calls `.isoformat()` on both, but the
ns-bearing Timestamp emits 9 fractional digits and the truncated datetime only
6, so `derive(...)` gets different bytes and the hash differs. Verified:
`2024-01-01T00:00:00.123456789Z` hashes to `d88a202d...` (pandas) vs
`25aedd1e...` (Polars). The manual chunked FK self-mask route can run
Polars-native while the oracle runs pandas, so a hash-declared `timestamp[ns]`
FK key diverges -- the specific hole that blocks the hash-only cascade fix.

## 2. The fix

Route the Polars `hash` handler through the shared Arrow kernel using
`source.to_arrow()` as the value producer, so the kernel input is byte-identical
to the pandas path. Verified: `pl.Series.to_arrow().to_pylist()[0]` returns the
SAME ns-preserving `pandas.Timestamp` (tz retained) that the pandas path
produces, and `_canonicalize_source` then yields identical bytes.

`execution/polars/_strategies/_hash.py` masking becomes (shape):

```python
from decoy_engine.kernel import hash_array
...
masked = hash_array(
    source.to_arrow(), seed=ctx.mask_key, namespace=plan.namespace,
    truncate=truncate, derive_func=derive,
)
return frame.with_columns(pl.Series(column, masked.to_pylist(), dtype=pl.Utf8)), []
```

`hash_array` (`kernel/_scalar.py:61-77`) accepts `pa.Array | pa.ChunkedArray |
list` and `_array_to_pylist` (`:34-51`) already handles the ChunkedArray
`to_arrow()` may return. Same `seed`/`namespace`/`truncate`/`derive_func` the
pandas path uses. One behavior change toward parity: the kernel's `_is_missing`
folds NaN AND None (`_scalar.py:13-31`), while the current Polars loop checks
only `value is None` (`polars/_hash.py:50`); routing through the kernel folds
NaN->null exactly as the pandas path already does (not a regression).

Hash OUTPUT is always `pa.string()`, and hash INPUT canonicalization is
value-based (not Arrow-type-based), so the accepted string/large_string width
drift the parity harness documents is irrelevant here; there is no residual
dtype divergence through the kernel for the canonicalizer-accepted dtypes.

## 3. Scope

IN:
- `execution/polars/_strategies/_hash.py`: use `source.to_arrow()` + `hash_array`
  (drop the independent `to_list()` + inline canonicalization).
- Cross-adapter hash parity tests across the full dtype matrix (section 5).

SIBLING BUGS (flagged for the plan-gate / Cam -- same or related root cause,
scope decision):
- **`categorical`** (`polars/_strategies/_categorical.py:94`) has the IDENTICAL
  bug: `source.to_list()` then `_canonicalize_source(value)` into `derive_index`,
  vs the pandas sibling iterating the raw Series (ns-preserving). RECOMMEND
  folding the same `to_arrow()` value-source fix into this pass (trivial,
  identical mechanism, same test file). Confirm at the gate.
- **`shuffle`** (`polars/_strategies/_shuffle.py:38,61`) has a RELATED but
  DISTINCT bug: it round-trips values through Python (`to_list()` then rebuilds a
  `pl.Series(..., dtype=source.dtype)`), zeroing nanoseconds on a `timestamp[ns]`
  column -- a value-corruption bug, not a canonicalization one. OUT of this
  slice (different mechanism); flagged as a separate follow-up.

OUT:
- No change to the pandas path, the shared kernel, or the canonicalizer.
- No change to the chunked-FK admission gate (that is the separate cascade plan).

## 4. Behavior contract

- For every canonicalizer-accepted key dtype (string, large_string, int, bool,
  date, `timestamp[ns]`, tz-aware timestamp, Decimal), `hash` produces
  byte-identical output through the pandas adapter and the Polars adapter
  (values, nulls). float and tz-naive datetime hard-error by canonicalizer
  design (`_canonicalize.py:86-95,99-108`); parity there is "both raise",
  preserved.
- hash remains tz-robust: the canonicalizer normalizes tz-aware datetimes via
  `.astimezone(utc).isoformat()` (`_canonicalize.py:109-111`), so the same
  instant hashes identically regardless of source tz on either adapter.
- No change to hash output for the pandas path (already correct) or to hash on
  string/int keys (already parity today).

## 5. Acceptance tests

Extend `tests/parity/test_strategy_substrate_parity.py` (runs the same
`(plan, sources)` through `PandasExecutionAdapter` and `PolarsExecutionAdapter`
and asserts `to_pydict()` equality). Existing hash cases (`:157-176`) cover only
string/int/truncated -- the gap that let this ship.

1. **hash cross-adapter parity across the dtype matrix**: add hash cases for
   `timestamp[ns]`, `timestamp[ns, tz=UTC]`, `date`, `Decimal`, `bool`,
   `large_string` (dtype template: `tests/parity/native/test_keyed_hash_parity.py`
   `_arrow_type_from_fixture` `:112-136`). Each asserts pandas-adapter output ==
   Polars-adapter output. The `timestamp[ns]` case is the direct regression
   (sub-us precision) and MUST fail before the fix, pass after.
2. **null / NaN folding**: a column with nulls (and, for float-adjacent paths, a
   NaN where reachable) hashes identically across adapters, confirming the
   kernel's `_is_missing` fold matches.
3. **hard-error parity preserved**: float and tz-naive datetime keys raise on
   BOTH adapters (the existing `:529-559` pattern), unchanged.
4. **(if categorical folded in)** the same dtype-matrix cross-adapter parity for
   `categorical` deterministic.
5. **No-regression**: the existing substrate-parity suite and
   `tests/parity/native/test_keyed_hash_parity.py` stay green.

VERIFY bar: the cross-adapter parity matrix green (timestamp[ns] case flips
fail->pass) + mutation on the changed `_hash.py` (and `_categorical.py` if
folded) lines; ruff/format/mypy(3.12) clean.

## 6. Tasks

- [ ] A. Fix `polars/_strategies/_hash.py` to route through `hash_array` via
  `source.to_arrow()`. (Gate decides whether to fold `categorical`.)
- [ ] B. Tests #1-#5.
- [ ] C. VERIFY (cross-adapter parity matrix + mutation) -> dennis REVIEW ->
  Codex FINAL gate. HELD, push, no merge.

## 7. Risks / open questions for the plan-gate

- Confirm `source.to_arrow().to_pylist()` yields byte-identical canonicalizer
  input to `pandas_column_to_kernel_input(...).to_pylist()` for EVERY
  canonicalizer-accepted dtype (not only timestamp[ns]) -- esp. tz-aware
  timestamps, Decimal, date, large_string, bool. Name any dtype where the two
  Arrow producers still diverge.
- Scope decision: fold `categorical` (identical bug) into this slice, or keep it
  hash-only per Cam's literal ask and file categorical separately? (Recommend
  fold: same one-line mechanism, same test file, closes an identical latent
  parity bug.)
- Confirm `shuffle`'s ns round-trip is genuinely a separate value-corruption bug
  (not fixable by the same kernel route) and is safe to defer.
- Confirm this fix makes the hash-only cascade admission
  (`chunked-fk-cascade-safety.md`) sound across both adapters for all
  canonicalizer-accepted key dtypes, so the two plans compose to fully close the
  timestamp[ns] FK self-mask hole.

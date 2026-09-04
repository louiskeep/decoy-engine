# Polars hash: cross-adapter parity for the common key dtypes via the shared kernel

Status: plan HANDLER-FIX GATE-CONFIRMED (2026-09-02, Opus). Rounds 1-3 NO-GO ->
Cam right-size; round 4: Codex confirmed the handler fix is a STRICT no-regression
and the exotic divergences are pre-existing (GO on the fix itself). Round-4
findings applied: test #1 parameterized over the full common set; the one BLOCKER
is cross-plan (the cascade plan must carry the exact-dtype restriction, section 6)
and is tracked in the cascade-safety plan's rescope, not here. Ready to build. Held target branch `feat/native-phase3`;
merges with the Phase-4 bundle. ENGINE correctness fix, foundational for the
hash-only chunked-FK cascade fix (`2026-09-02-chunked-fk-cascade-safety.md`).

## Scope decision (Cam, 2026-09-02)

The plan-gate established that a truly all-dtype fix is impossible without adapter
surgery: the Polars adapter's INGESTION step (`pl.from_arrow`,
`_polars_adapter.py:223`) corrupts or rejects EXOTIC Arrow types BEFORE any
strategy handler runs -- `date64` is remapped to tz-naive `timestamp[ms]`,
negative-scale `Decimal` raises `ComputeError`, `decimal256` raises
`PanicException`, fixed-offset-tz timestamps are rejected, and
`dictionary`-wrapped aware timestamps drop their tz. So a handler-level fallback
cannot recover them. Cam's call: RIGHT-SIZE. Fix the common/realistic key dtypes
(which the plan-gate confirmed are cleanly parity-fixable), and let the cascade
fix REFUSE the exotic dtypes as FK keys (they are nonsensical as identifiers).
No adapter-ingestion rework.

## 1. The bug

`hash` is not byte-identical between the pandas and Polars adapters for
sub-microsecond values. Both call the same canonicalizer
(`generation/pool/_canonicalize.py::_canonicalize_source`); the divergence is the
Python value fed to it. Pandas (`execution/_strategies/_hash.py:52-53`) feeds
`pa.array(col, from_pandas=True).to_pylist()` -> ns-preserving `pandas.Timestamp`;
Polars (`execution/polars/_strategies/_hash.py:47`) feeds `source.to_list()` ->
a us-truncated stdlib `datetime`, so `.isoformat()` emits 6 vs 9 fractional
digits and the hashes differ. The manual chunked FK self-mask route can run
Polars-native while the oracle runs pandas, so a hash-declared tz-aware
`timestamp[ns]` FK key diverges -- the hole that blocks the hash-only cascade.

## 2. The fix

Route the Polars `hash` handler through the shared `hash_array` kernel
(`kernel/_scalar.py:61`) via `source.to_arrow()`, UNCONDITIONALLY -- no dtype
predicate, no fallback (the plan-gate proved a handler-level fallback is
impossible because ingestion already ran). The plan-gate's byte-level probes
(pandas 2.3.3 / Polars 1.42.0 / pyarrow 24) confirmed `source.to_arrow()` ->
`hash_array` is byte-identical to the pandas path for every COMMON key dtype and
is a no-regression on the dtypes that were already parity:

```python
from decoy_engine.kernel import hash_array
...
masked = hash_array(source.to_arrow(), seed=ctx.mask_key,
                    namespace=plan.namespace, truncate=truncate, derive_func=derive)
return frame.with_columns(pl.Series(column, masked.to_pylist(), dtype=pl.Utf8)), []
```

`hash_array` accepts `pa.Array | pa.ChunkedArray | list` and folds NaN AND None
via `_is_missing` (`_scalar.py:13-31`), matching the pandas path; the current
Polars loop's `value is None`-only check is replaced (a fix toward parity). This
is a strict improvement: it fixes the sub-us divergence for the common dtypes and
does not regress the dtypes that were already parity. It does NOT try to fix the
exotic dtypes that ingestion already corrupted -- those are handled by the
cascade dtype restriction (section 6), not here.

## 3. Behavior contract

- For the COMMON key dtypes, `hash` is byte-identical through the pandas and
  Polars adapters (confirmed by the plan-gate): string, large_string,
  signed/unsigned integer, bool, `date32`, IANA-zone `timestamp` in s/ms/us/ns,
  and positive-scale `decimal32/64/128`. Values and null-handling match.
- Preserved fail-closed cases (unchanged, both adapters): finite float and
  tz-NAIVE datetime keys raise; hash is tz-robust for IANA zones (canonicalizer
  normalizes via `.astimezone(utc)`, `_canonicalize.py:109-111`), so the same
  instant hashes identically regardless of source IANA tz.
- `[NaN, None]` folds to `[None, None]` on both adapters.
- EXOTIC dtypes (date64, negative-scale/`decimal256`, fixed-offset-tz timestamps,
  `dictionary`-wrapped aware timestamps) are NOT made cross-adapter-parity by this
  fix: the Polars adapter's ingestion corrupts/rejects them before the handler,
  which is a pre-existing adapter-boundary limitation this slice does not change.
  They are refused as FK keys by the cascade fix (section 6). This is documented,
  not silently ignored.
- No change to the pandas path, the shared kernel, or the canonicalizer. Polars
  output stays `Utf8`; ordering unchanged.

## 4. Acceptance tests

Extend `tests/parity/test_strategy_substrate_parity.py` (same `(plan, sources)`
through both adapters, assert output equality). Existing hash cases (`:157-176`)
cover only string/int/truncated.

1. **hash cross-adapter parity, FULL common dtype matrix** (parameterized -- the
   plan-gate flagged a partial matrix as insufficient): `string`, `large_string`,
   EVERY signed width (int8/16/32/64) AND unsigned width (uint8/16/32/64), `bool`,
   `date32`, IANA-zone `timestamp` in ALL FOUR units (s/ms/us/ns, tz=UTC), and
   positive-scale `decimal32`, `decimal64`, `decimal128`. Each asserts
   pandas-adapter output == Polars-adapter output at the `Series.to_arrow()`
   boundary. The `timestamp[ns, tz=UTC]` case with NONZERO sub-microsecond digits
   is the direct regression (fail before, pass after).
1b. **Timezone robustness**: the SAME INSTANT as `timestamp[us, tz=UTC]` and as
   the equivalent `timestamp[us, tz=<IANA non-UTC>]` hashes to the IDENTICAL value.
2. **NaN/None success-to-null**: `[NaN, None]` hashes to `[None, None]`
   identically across adapters (separate from the finite-float case).
3. **Fail-closed parity (unchanged)**: a finite-float key and a tz-NAIVE datetime
   key each RAISE on BOTH adapters.
4. **No-regression**: the existing substrate-parity suite and
   `tests/parity/native/test_keyed_hash_parity.py` stay green.

The exotic dtypes are NOT parity-tested here (they cannot be made parity without
adapter surgery); their FK-key refusal is tested in the cascade plan.

VERIFY bar: the common-dtype cross-adapter matrix green (the tz-aware
`timestamp[ns]` case flips fail->pass) + mutation on the changed `_hash.py` line;
ruff/format/mypy(3.12) clean.

## 5. Deferred siblings (own slices)

- `categorical` (`polars/_strategies/_categorical.py:93`) shares the sub-us bug
  but also has an `is_null()` vs `isna()` NaN divergence and inherits the exotic
  ingestion issues; its own slice.
- `shuffle` (`polars/_strategies/_shuffle.py:38`) is a distinct value-corruption
  bug (and pandas also narrows shuffled timestamps); deferred.

## 6. Composition with the cascade fix (REQUIRED companion change)

This fix makes hash byte-parity across adapters for the COMMON key dtypes only.
So the hash-only cascade fix (`2026-09-02-chunked-fk-cascade-safety.md`) MUST add
an FK-key dtype restriction: admit a hash FK self-mask edge only when the key
dtype is in the confirmed-safe set (string, large_string, integer, bool, date32,
IANA-zone timestamp, positive-scale decimal), refusing exotic dtypes (date64,
negative-scale/`decimal256`, fixed-offset-tz, dictionary-wrapped) fail-closed.
The gate already has a dtype-family condition; this tightens it to the
cross-adapter-safe set. After BOTH land, hash-only FK self-mask is
byte-parity-sound across both adapters for every ADMITTED key dtype.

## 7. Tasks

- [ ] A. Fix `polars/_strategies/_hash.py`: `source.to_arrow()` -> `hash_array`
  (unconditional; drop the `to_list()` loop).
- [ ] B. Tests #1-#4.
- [ ] C. VERIFY (common-dtype parity matrix + mutation) -> dennis REVIEW ->
  Codex FINAL gate. HELD, push, no merge. (The cascade plan carries the FK-key
  dtype restriction, section 6.)

## 8. Risks / open questions for the plan-gate

- Confirm `source.to_arrow()` -> `hash_array` is byte-identical to the pandas
  path for EVERY listed common dtype and a strict no-regression on the
  already-parity dtypes (the round-3 probes confirmed
  string/large_string/int/bool/date32/IANA-tz-timestamp-s..ns/positive-decimal).
- Confirm the exotic-dtype divergences are genuinely pre-existing (present with
  the current `to_list()` code too) and not introduced or worsened by this fix,
  so scoping them out is honest.
- Confirm the cascade plan's FK-key dtype restriction (section 6) is the correct
  and complete companion so that hash-only FK self-mask is sound for every
  ADMITTED key dtype across both adapters.

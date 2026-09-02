# Polars hash: cross-adapter parity via the shared kernel + exotic-dtype pandas port

Status: plan (authored 2026-09-02, Opus; plan-gate rounds 1-2 NO-GO remediated
-- round 2 flipped the denylist to a fail-safe positive allowlist and added the
same-instant tz test; re-gate round 3). Held target branch `feat/native-phase3`; merges with the
Phase-4 bundle. ENGINE correctness fix (cross-adapter hash parity), foundational
for the hash-only chunked-FK cascade fix
(`2026-09-02-chunked-fk-cascade-safety.md`), which is unsound until this lands.
Greenlit by Cam ("fix Polars hash kernel").

## 1. The bug

The `hash` masking strategy is not byte-identical between the pandas and Polars
adapters for sub-microsecond-precision values. Both adapters call the same
canonicalizer (`generation/pool/_canonicalize.py::_canonicalize_source`); the
divergence is the Python value handed to it. Pandas
(`execution/_strategies/_hash.py:52-53`) feeds `pa.array(col,
from_pandas=True).to_pylist()` -> a ns-preserving `pandas.Timestamp`; Polars
(`execution/polars/_strategies/_hash.py:47`) feeds `source.to_list()` -> a
us-truncated stdlib `datetime`, so `.isoformat()` emits 6 vs 9 fractional digits
and the hash differs. The manual chunked FK self-mask route can run Polars-native
while the oracle runs pandas, so a hash-declared `timestamp[ns, tz=...]` FK key
diverges -- the hole that blocks the hash-only cascade fix.

## 2. The fix

Route the Polars `hash` handler through the shared `hash_array` kernel
(`kernel/_scalar.py:61`) so its canonicalizer input matches the pandas path,
with a fallback for the Arrow dtypes a Polars Series cannot losslessly reproduce.
The plan-gate's byte-level probes (HEAD ceaee50d, pandas 2.3.3 / Polars 1.42.0 /
pyarrow 24) established exactly which dtypes each producer agrees on:

- **`source.to_arrow()` -> `hash_array` is byte-identical to the pandas path**
  for: string, large_string, signed/unsigned int, bool, `date32`, tz-AWARE
  timestamps in s/ms/us/ns, positive-scale `decimal32/64/128`, and Arrow
  dictionary/categorical strings. Use it for these.
- **The Polars Arrow producer DIVERGES or cannot ingest** for at least `date64`
  (Polars remaps it), `negative-scale Decimal` / `decimal256` (Polars cannot
  ingest), `time64[ns]`, and dictionary-wrapped versions of these -- and a
  denylist would keep growing. So the fix uses a POSITIVE ALLOWLIST, NOT a
  denylist: only the proven-parity Arrow types (unwrapping `dictionary`
  `value_type`, as `_chunked_fk_dtype.py:48` already does) take the fast
  `to_arrow()` path; EVERY other hash key type is routed to the PANDAS hash
  handler (`execution/_strategies/_hash.py`, the `PandasStrategyPort` mechanism
  the Polars adapter already uses for `date_shift`/`fpe`/`faker`), which is
  byte-identical to the oracle by construction. This is fail-safe: any unproven
  or future Arrow type defaults to the pandas oracle, so there is no "forgot a
  divergent type" hole.

Fix shape for `execution/polars/_strategies/_hash.py` (masking path):

```python
from decoy_engine.kernel import hash_array
...
arrow = source.to_arrow()            # ChunkedArray | Array
if not _hash_polars_native_ok(arrow.type):   # allowlist miss -> pandas oracle
    return _pandas_hash_port(frame, column, plan, ctx)   # existing port path
masked = hash_array(arrow, seed=ctx.mask_key, namespace=plan.namespace,
                    truncate=truncate, derive_func=derive)
return frame.with_columns(pl.Series(column, masked.to_pylist(), dtype=pl.Utf8)), []
```

`_hash_polars_native_ok(t)` returns True only for the ALLOWLIST (unwrapping
`dictionary` to its `value_type` first): `string`, `large_string`, signed/
unsigned integer, `bool`, `date32`, `timestamp` WITH a tz in any unit
(s/ms/us/ns), and positive-scale `decimal128/64/32`. Everything else -- `date64`,
`time64`, negative-scale / `decimal256`, tz-naive timestamps, floats, and any
unrecognized type -- returns False and goes to the pandas port (where the
canonicalizer applies its own accept/raise rules, so tz-naive and float still
raise consistently). `hash_array` accepts `pa.Array | pa.ChunkedArray | list`
and folds NaN AND None via `_is_missing` (`_scalar.py:13-31`), matching the
pandas path; the current Polars loop's `value is None`-only check is replaced (a
fix toward parity). Null-bearing integer keys are already rejected before
dispatch by both adapters (`execution/_guards.py:34`), so they are not a hole.

## 3. Scope

IN:
- `execution/polars/_strategies/_hash.py`: the `to_arrow()` -> `hash_array` route
  + the exotic-dtype pandas-port fallback.
- Cross-adapter hash parity tests across the full dtype matrix (section 5).

DEFERRED (own slices -- NOT folded here):
- **`categorical`** (`polars/_strategies/_categorical.py:93`) has the identical
  sub-us bug, BUT a one-line `to_arrow()` change is INSUFFICIENT: Polars uses
  `is_null()` while pandas uses `isna()` (`:95` vs `_strategies/_categorical.py:169`),
  so NaN still diverges, and it inherits the same date64 issue. It needs its own
  slice (original Arrow values + pandas-equivalent missing handling + a
  fail-before regression). Not required for the hash-only cascade fix. Filed as a
  follow-up.
- **`shuffle`** (`polars/_strategies/_shuffle.py:38`) is a DISTINCT
  value-corruption bug (ns round-trip), and pandas ALSO narrows shuffled
  timestamps to us (documented at `tests/unit/execution/test_shuffle_categorical.py:139`)
  -- a broader shuffle-fidelity issue, not a hash-parity or cascade blocker.
  Deferred.

OUT: no change to the pandas path, the shared kernel, the canonicalizer, or the
chunked-FK admission gate (the separate cascade plan).

## 4. Behavior contract

- For every canonicalizer-accepted key dtype, `hash` is byte-identical through
  the pandas and Polars adapters: the common dtypes via `to_arrow()` ->
  `hash_array`, and `date64` / negative-scale-`Decimal` / `decimal256` via the
  pandas hash port. Values and null-handling match.
- Preserved fail-closed cases (unchanged, on BOTH adapters): finite float keys
  raise, and tz-NAIVE datetimes (incl. a bare `timestamp[ns]` with no tz) raise
  by canonicalizer design (`_canonicalize.py:86-95,99-108`). Only tz-AWARE
  timestamps hash, and hash is tz-robust: the canonicalizer normalizes via
  `.astimezone(utc).isoformat()` (`_canonicalize.py:109-111`), so the same
  instant hashes identically regardless of source tz on either adapter.
- `[NaN, None]` folds to `[None, None]` on both adapters (kernel `_is_missing`).
- No change to hash output for the pandas path, or to hash on string/int keys
  (already parity). Polars output stays `Utf8` (accepted `large_string` at
  egress); ordering unchanged.

## 5. Acceptance tests

Extend `tests/parity/test_strategy_substrate_parity.py` (same `(plan, sources)`
through `PandasExecutionAdapter` and `PolarsExecutionAdapter`, assert output
equality). Existing hash cases (`:157-176`) cover only string/int/truncated.

1. **hash cross-adapter parity matrix**: add cases for `string`, `large_string`,
   `int`, `bool`, `date32`, **`date64`**, **`time64[ns]`**, `timestamp[ns,
   tz=UTC]`, positive-scale `decimal128`, **negative-scale `Decimal`**,
   **`decimal256`**, and a **dictionary-wrapped** case (e.g. `dictionary<date64>`
   or a dictionary string) to exercise the `value_type` unwrap. Each asserts
   pandas-adapter output == Polars-adapter output. The `timestamp[ns, tz=UTC]`
   case with NONZERO sub-microsecond digits is the direct regression (fail before
   the fix, pass after). `date64` / `time64` / the exotic decimals /
   dictionary-wrapped exotics exercise the pandas-port allowlist-miss path.
1b. **Timezone robustness**: the SAME INSTANT represented as `timestamp[us,
   tz=UTC]` and as the equivalent `timestamp[us, tz=<non-UTC>]` hashes to the
   IDENTICAL value (not merely "both adapters agree on one tz") -- proving the
   `.astimezone(utc)` normalization on both adapters.
2. **NaN/None success-to-null**: a column with `[NaN, None]` hashes to
   `[None, None]` identically across adapters (SEPARATE from the finite-float
   case).
3. **Fail-closed parity (unchanged)**: a FINITE-float key and a tz-NAIVE datetime
   key (incl. bare `timestamp[ns]`) each RAISE on BOTH adapters
   (`:529-559` pattern).
4. **No-regression**: the existing substrate-parity suite and
   `tests/parity/native/test_keyed_hash_parity.py` stay green.

VERIFY bar: the cross-adapter parity matrix green (the tz-aware `timestamp[ns]`
case flips fail->pass; date64/exotic-decimal covered by the fallback) + mutation
on the changed `_hash.py` lines (the type predicate + the port branch);
ruff/format/mypy(3.12) clean.

## 6. Tasks

- [ ] A. Fix `polars/_strategies/_hash.py`: a positive-allowlist
  `_hash_polars_native_ok` predicate (dictionary-unwrapped) gates the fast
  `to_arrow()` -> `hash_array` path; every allowlist miss routes to the pandas
  hash port.
- [ ] B. Tests #1-#4.
- [ ] C. VERIFY (cross-adapter parity matrix + mutation) -> dennis REVIEW ->
  Codex FINAL gate. HELD, push, no merge.

## 7. Risks / open questions for the plan-gate

- Confirm the `_hash_polars_native_ok` ALLOWLIST is correct: every listed type
  (string/large_string/int/bool/date32/tz-aware-timestamp-s..ns/positive-decimal,
  dictionary-unwrapped) is proven byte-parity through `to_arrow()`, and NOTHING
  outside it silently takes the native path. The allowlist is fail-safe by
  construction (misses go to pandas), so the risk is an over-broad entry, not a
  missing one -- name any listed type that is NOT actually parity-safe.
- Confirm the pandas hash port path is reachable and byte-identical to the oracle
  for those exotic dtypes (it runs the real pandas handler), and that routing a
  single column to it inside the Polars adapter composes cleanly with the frame
  return.
- Confirm deferring `categorical` and `shuffle` is right: neither blocks the
  hash-only cascade, and folding the incomplete categorical one-liner would be a
  latent NaN/date64 divergence.
- Confirm that after this lands, hash-only FK self-mask is byte-parity-sound
  across both adapters for every admitted key dtype, so the two plans fully close
  the timestamp[ns] FK self-mask hole.

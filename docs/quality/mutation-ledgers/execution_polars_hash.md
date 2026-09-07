# Mutation grading: Polars hash cross-adapter parity fix (`polars/_strategies/_hash.py`)

Scope: the CHANGED unit is the masking path of `PolarsHashStrategyHandler.run`
-- the `to_list()` per-value loop replaced by
`hash_array(source.to_arrow(), ...)`. Graded via
`scripts/native-testing/python_mutation_pilot.py`, selection
`tests/parity/test_strategy_substrate_parity.py`.

## Numbers

**40 mutants: 25 killed, 15 survived, 0 true-timeout (62.50% logic).** All 15
survivors are in `PolarsHashStrategyHandler.run`. **0 unresolved
correctness-critical logic on the CHANGED masking path.**

## Adjudication

- **Changed path (the fix), 3 survivors, all output-equivalent:**
  - `derive_func=derive` dropped from the `hash_array(...)` call: output-identical
    because `derive` IS the kernel's default derive_func (the pandas path passes
    it explicitly too, defensively); the cross-adapter parity matrix survives
    either way.
  - Two on the output `pl.Series(column, masked.to_pylist(), dtype=pl.Utf8)`
    construction: Polars infers `Utf8` from the string list, so the dtype hint is
    redundant -- output-equivalent.
  The correctness of the change (cross-adapter byte-parity for the common key
  dtypes) is proven by test #1's 19-case parameterized matrix and the
  demonstrated `timestamp[ns, tz=UTC]` regression flip (fail before -> pass after).

- **Pre-existing code NOT touched by this fix, 12 survivors (out of scope):** the
  `hash_requires_namespace` guard's `code=`/`strategy=`/`message=` prose (mutants
  2-11) and the `truncate` config parsing (`raw_truncate > 0`, mutants 20-21).
  These predate the fix (the fix only replaced the value-production loop); the
  machine-consumed `code` on the raise path is covered by the strategy's own
  namespace tests, and truncate behavior by the existing truncated-hash case. Not
  a regression introduced here.

## Regenerate

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/polars/_strategies/_hash.py \
  --tests tests/parity/test_strategy_substrate_parity.py \
  --timeout 60
```

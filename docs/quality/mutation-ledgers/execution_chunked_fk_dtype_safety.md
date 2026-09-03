# Mutation grading: chunked-FK cascade-safety predicate (`_chunked_fk_dtype_safety.py`)

Scope: the CHANGED unit is the new
`src/decoy_engine/execution/_chunked_fk_dtype_safety.py` -- predicate 12's exact
cross-adapter-safe FK-key dtype predicate. Two entry points:
`arrow_type_is_fk_hash_safe` (real per-chunk Arrow type) and
`declared_fk_hash_dtype_is_safe` (compile-time operator-declared string, which
parses then judges through the same real-type predicate). Graded via
`scripts/native-testing/python_mutation_pilot.py` (mutmut + standalone-pytest
readjudication), selection `test_chunked_fk_dtype_safety.py` +
`test_chunked_fk_gate_kills.py` + `test_de10_chunked_fk_declared_dtype.py`.

## Numbers

**89 mutants: 87 killed, 2 survived, 0 true-timeout (97.75% logic).**
**0 unresolved correctness-critical logic on the changed unit** -- both
survivors are output-equivalent (proof below).

This is the post-pinning figure. The pre-pinning run (gate-kill + de10 selection
only) scored 82.02% (73/89) with 16 survivors, all on this predicate's own
admit/reject branches -- the behavioral consumers exercised the predicate
indirectly but did not pin its every branch. A direct exhaustive unit test
(`test_chunked_fk_dtype_safety.py`, every admit branch, every reject branch,
every declared-parse path) closed 14 of the 16.

## Why the reject branches had to be pinned

This predicate is the whole cross-adapter byte-parity guard for hash-only FK
self-masking: a reject branch silently flipping to admit re-opens exactly the
divergence the cascade-safety fix closes (admitting `date64`-as-safe,
`decimal256`-as-safe, a fixed-offset tz as IANA, or any unrecognized type via
the catch-all). Every `return False -> return True` mutation on a reject branch
is therefore a fail-OPEN hazard, not benign; each is now killed by a direct
assertion that the corresponding real type (or declared string) is rejected:

- `arrow_type_is_fk_hash_safe`: dictionary, `date64`, tz-naive timestamp,
  fixed-offset / unresolvable-tz timestamp, `decimal256` (incl. scale 0),
  negative-scale decimal, and the catch-all (`time64`/`time32`/binary/float/null)
  each have a pinning reject case.
- `declared_fk_hash_dtype_is_safe`: the explicit-width decimal branch, the
  bare-width decimal branch (both scale-honored: bare `decimal(10, -1)` /
  `numeric(8, -2)` must reject -- this killed mutant 54, which dropped the scale
  argument and would have force-admitted a negative-scale bare decimal),
  precision-overflow construction failure, tz-naive/fixed-offset declared
  timestamps, and the final unrecognized reject.

## Accepted survivors (both output-equivalent, on `declared_fk_hash_dtype_is_safe`)

- **mutmut_25** -- the tz-absent else branch mutated
  `pa.timestamp(unit)` -> `pa.timestamp(None)`. The else branch is reached ONLY
  for a tz-naive declared timestamp (`timestamp[us]` with no `tz=`). The original
  builds `pa.timestamp("us")`, which the shared predicate rejects as tz-naive
  (returns False); the mutant calls `pa.timestamp(None)`, which raises, hits the
  `except`, and also returns False. No tz-naive timestamp is ever safe, so both
  paths return False for every input that reaches this branch -- equivalent, not
  killable.
- **mutmut_26** -- the construction `except` mutated `return False` ->
  `return True`. The `except` is defensive-only: `pa.timestamp` accepts any of
  the regex-matched units (`s|ms|us|ns`) with an arbitrary tz string without
  raising (tz is stored verbatim; ZoneInfo validation happens later inside the
  shared predicate). No regex-matched declared string reaches this `except`, so
  the mutation is unreachable -- equivalent.

Neither survivor changes what dtype is admitted vs. rejected on any reachable
input.

## Regenerate

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/_chunked_fk_dtype_safety.py \
  --tests tests/unit/execution/test_chunked_fk_dtype_safety.py \
          tests/unit/execution/test_chunked_fk_gate_kills.py \
          tests/unit/execution/test_de10_chunked_fk_declared_dtype.py \
  --timeout 60
```

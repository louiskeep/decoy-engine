# Mutation grading: `execution/_strategies/_hash.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_hash.py` is a thin StrategyHandler (60 LOC)
for the joinability-preserving deterministic hash (engine-v2 S9): it fails closed
when a hash column has no namespace, resolves the optional `truncate` config, and
delegates the actual keyed hashing to `kernel.hash_array` (which is graded with the
crypto/determinism primitives, not here).

**Grade scope: FOCUSED selection only.** mutmut ran against `_hash.py` with the
test selection restricted to `tests/unit/execution/test_hash_bucketize.py`.

## Numbers

**34 mutants: 26 killed (76% baseline), 8 survived -> 31 killed after this pass,
3 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **5 LOGIC survivors killed** by 3 new tests (`TestHashHandlerContract`). 0 bugs.
- **3 EQUIVALENT survivors** left alive (2 message prose/default, 1 default-arg
  no-op). All verified behavior-preserving.

## LOGIC (5): killed by new tests

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_3` | namespace-guard `StrategyError(strategy="hash")` -> `strategy=None` | `test_missing_namespace_error_carries_strategy_and_code` (asserts `.strategy == "hash"` on the raised StrategyError) |
| `run__mutmut_10`, `11` | `strategy="hash"` -> `"XXhashXX"` / `"HASH"` | same |
| `run__mutmut_20` | `truncate = raw if isinstance(int) and raw > 0` -> `raw >= 0` (config `truncate: 0` would truncate to the empty string instead of "no truncation") | `test_truncate_zero_is_ignored_not_applied` (config `truncate=0`; asserts output equals the full untruncated token, length > 1) |
| `run__mutmut_21` | `raw > 0` -> `raw > 1` (config `truncate: 1` would silently stop truncating) | `test_truncate_one_truncates_to_one_char` (config `truncate=1`; asserts output is the 1-char prefix) |

## EQUIVALENT (3)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_4` | namespace-guard `message=f"..."` -> `message=None` | `StrategyError.message` defaults to `""` and is prose (never machine-consumed); `code`/`strategy` are asserted. `None` is falsy, so the rendered form is identical to the default. |
| `run__mutmut_7` | `message=` kwarg removed | Same default; the raise still carries the asserted `code`/`strategy`. |
| `run__mutmut_32` | `hash_array(..., derive_func=derive)` kwarg removed | `hash_array`'s signature already defaults `derive_func=derive` (the same module-level function), so dropping the explicit pass-through is byte-identical. |

## Gate

Dennis batch gate (_hash + _bucketize): **PASS**, 0 P0 / 0 P1 / 0 P2. All
EQUIVALENT classifications verified behavior-preserving against source; all kills
confirmed genuine.

## Regenerate (any shell)

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_strategies/_hash.py` and the test selection to
`tests/unit/execution/test_hash_bucketize.py`, then `rm -rf mutants && python -m
mutmut run`.

# Mutation grading: `transforms/group_key.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `transforms/group_key.py` (176 LOC) derives a
stable pseudonymous key per group value: for each distinct value in the group
column it computes `prefix + derive(seed, namespace, str(value).utf-8)[:n_bytes].hex()`,
caching per value so equal group values map to the same key (join-preserving).

**Grade scope: FOCUSED selection** (`tests/unit/transforms/test_group_key.py`, 18 tests).

## Numbers

**23 mutants: 22 killed (96% baseline), 1 survived -> 22 killed, 1 EQUIVALENT.**
LOGIC-mutant score 100%. 0 timeouts. No new tests required (the sole survivor is
equivalent). High baseline: the existing suite covers key stability, prefix,
byte-length truncation, namespace/seed keying, and null handling.

## EQUIVALENT (1)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `apply_group_key_12` | `str(raw_val).encode("utf-8")` -> `encode("UTF-8")` | Python codec names are case-insensitive and normalize to the same codec, so `"utf-8"` and `"UTF-8"` produce byte-identical output. The derived key is unchanged for every input. |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/group_key.py`, selection to
`tests/unit/transforms/test_group_key.py`, then `rm -rf mutants && python -m mutmut run`.

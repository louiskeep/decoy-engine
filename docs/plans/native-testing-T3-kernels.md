Status: record

# T3 scalar + keyed kernels: measurement, fill, and adjudication

- **Plan:** `docs/plans/2026-08-29-native-efficiency-test-plan.md`, batch T3 (section 4) and the
  method in section 3.
- **Scope:** `src/decoy_engine/execution/native/_kernels_scalar.py` (`native_passthrough`,
  `native_redact`, `native_truncate`) and `_kernels_keyed.py` (`native_keyed_hash`). Both modules
  are thin Arrow-native wrappers over shared, already-tested logic (`kernel/_scalar.py`'s
  functions and the compiled/reference keyed-derivation kernel); this batch grades only the
  wrapper's own branches (validation order, type stabilization, config resolution), not the
  delegated logic those other modules already own.
- **Branch:** `docs/native-efficiency-test-plan`, worktree `.claude/worktrees/native-test-plan`.
- **Harness reused, not re-derived:** `scripts/native-testing/python_mutation_pilot.py` (T0).
  `_kernels_keyed.py` is keyed-hash (crypto-adjacent), so its run used `--readjudicate-killed`
  per T0's carry-forward; `_kernels_scalar.py` has no crypto surface, matching T0's own pilot run
  on this exact module, so it ran without that flag.

## Method: measure first

Ran branch coverage and the mutation pilot against the existing test selection before writing any
test, per section 3 rule 1:
- `_kernels_scalar.py` against `tests/native/test_kernels_scalar.py` (the only file targeting it).
- `_kernels_keyed.py` against `tests/native/test_kernels_keyed.py` plus
  `tests/parity/native/test_keyed_hash_parity.py` (its in-process contract tests and its
  cross-process/cross-mutation parity harness).

The Rust companion was installed in this environment, so the companion-gated keyed tests ran for
real, not skipped.

## BEFORE / AFTER: coverage

Both modules were already at 100% branch coverage before this batch (their wrapper logic is small
and every branch is exercised by the existing example tests); coverage is unchanged by this batch.

```
coverage run --branch -m pytest -q tests/native/test_kernels_scalar.py
coverage report --include=*/execution/native/_kernels_scalar.py -m
# 21 stmts, 8 branches, 0 missed, 100%

coverage run --branch -m pytest -q tests/native/test_kernels_keyed.py tests/parity/native/test_keyed_hash_parity.py
coverage report --include=*/execution/native/_kernels_keyed.py -m
# 12 stmts, 2 branches, 0 missed, 100%
```

This batch's gaps are mutation gaps, not coverage gaps: every line and branch already ran, but two
shapes of mutant survived anyway (an unexercised default-argument value, and message-text-only
mutations of raised exceptions), the exact pattern the plan's method section anticipates
(coverage measures reachability, not assertion strength).

## `_kernels_scalar.py`: BEFORE mutation tally

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_kernels_scalar.py \
  --tests tests/native/test_kernels_scalar.py --timeout 60
```

68 mutants total: 58 killed, 10 survived, 0 unresolved. Reproduces T0's own pilot run on this
module exactly (T0 recorded the identical 58/10 split). All 10 survivors are inside
`native_truncate`; `native_passthrough` (1 mutant) and `native_redact` (10 mutants) start this
batch already at zero survivors.

The 10 survivors split into two shapes (per T0's carry-forward, confirmed by inspecting each
mutant body in `mutants/src/.../_kernels_scalar.py`):
1. **Two default-`keep` mutants** (`mutmut_1`: `keep: str = "XXheadXX"`, `mutmut_2`:
   `keep: str = "HEAD"`). Every existing test passes `keep=` explicitly, so the default's value is
   never exercised: a genuine untested-default gap, not equivalent.
2. **Eight message-text mutants** (`mutmut_9/12/17/25/28/41/44/49`), each mutating only the
   `message=` argument (or the `type(...)` inside its f-string) of one of `native_truncate`'s three
   `StrategyError` raises (`truncate_length_invalid`, `truncate_keep_invalid`,
   `truncate_mask_char_invalid`) to `None`, a dropped kwarg (falls back to `StrategyError`'s own
   `message: str = ""` default), or an upper/lower-cased variant.

## `_kernels_scalar.py`: fill

One test closes the default-`keep` gap, matching the plan's stated fix exactly:
`test_native_truncate_default_keep_truncates_from_head` in `tests/native/test_kernels_scalar.py`.
It calls `native_truncate(array, length=3, mask_char=None)` (omitting `keep` for the first time in
this file), asserts parity against `TruncateHandler`'s own default-`keep` output, and pins the
literal result `["hel", "wor", None]` so the assertion cannot pass by coincidence if the default
silently changed to some other truncation. Non-vacuous: it fails if `keep`'s default is anything
other than `"head"` (confirmed live against the two default-mutating mutants below), and it is a
real parity assertion against the shipped handler, not a self-check.

No production code touched.

## `_kernels_scalar.py`: AFTER mutation tally

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_kernels_scalar.py \
  --tests tests/native/test_kernels_scalar.py --timeout 60
```

68 mutants total: 60 killed (+2), 8 survived, 0 unresolved. Both default-`keep` mutants
(`mutmut_1`, `mutmut_2`) are gone; the 8 message-text survivors are unchanged, at the same mutant
indices as BEFORE.

## `_kernels_scalar.py`: five-field adjudication

| Function | Total mutants | Killed | Equivalent | Unreachable-by-contract | Tool-excluded |
|---|---:|---:|---:|---:|---:|
| `native_passthrough` | 1 | 1 | 0 | 0 | 0 |
| `native_redact` | 10 | 10 | 0 | 0 | 0 |
| `native_truncate` | 57 | 49 (2 new) | 8 | 0 | 0 |
| **Total** | 68 | 60 (2 new) | 8 | 0 | 0 |

Coverage per function is 100% (module-level 100%, no lines outside these three functions).

### Equivalent mutants (with reason)

**`native_truncate`, mutants 9/12/17/25/28/41/44/49 (8 survivors):** each mutates only the
`message=` text (or the `type(...)` name embedded in that f-string) passed to one of the three
`StrategyError` raises. Every test asserting these raises (`test_native_truncate_invalid_*`)
checks `.code` and `.strategy` only, matching the sibling `TruncateHandler` tests; no test, and no
part of the module's documented contract, asserts `.message`. Equivalent by contract, the same
adjudication T0's own pilot made for this exact module and the same reasoning T2 used for
`_crypto_ext.py`'s non-contractual message strings.

### Unreachable-by-contract / tool-excluded

None. Both non-crypto kernel wrappers admit values across their whole declared parameter space
(no branch is behind a type limit this scope doesn't already test), and neither has a PyO3/panic
boundary to exclude.

### Pinned pandas-artifact divergences: unaffected

`native_redact`'s type-stabilization branch (`isinstance(redact_with, str) and result.type !=
pa.string()`) is inside the 10 mutants already fully killed before this batch, confirming the
existing divergence-pinning tests (`test_native_redact_all_null_column_type_diverges_from_pandas_
oracle`, `..._empty_column_...`, `..._non_string_redact_with_...`, and the batch-schema-stability
test) already catch a mutation of that handling. Nothing in this batch touched or weakened those
tests.

## `_kernels_keyed.py`: BEFORE mutation tally (with `--readjudicate-killed`)

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_kernels_keyed.py \
  --tests tests/native/test_kernels_keyed.py tests/parity/native/test_keyed_hash_parity.py \
  --readjudicate-killed --timeout 60
```

27 mutants total. mutmut's own run: 23 killed, 4 survived. The mandatory `--readjudicate-killed`
pass re-ran all 23 killed mutants standalone and found **one flaky-kill**: `mutant_12` (mutmut
marked it killed; the standalone rerun marked it survived). Corrected tally: **22 killed, 5
survived, 0 unresolved.**

Every one of the 5 real survivors (mutants 4, 7, 12, 13, 14) mutates only the `message=` argument
of the single `StrategyError` raise in `native_keyed_hash` (the `hash_requires_namespace` guard on
a `None` namespace) -- to `None`, a dropped kwarg (falls back to the class's own `message: str =
""` default), a marker-wrapped string, or an upper/lower-cased variant. This is the function's
ONLY raise statement, so there is no other branch for these operators to land on. No value, type,
or exception-TYPE mutant survived anywhere in the function: `resolved_truncate`'s resolution
(`isinstance(truncate, int) and truncate > 0`), the fail-closed namespace check's `is None`
comparison, and the `kernel.derive_batch(...)` call's four keyword arguments are all covered by
the existing parametrized `test_truncate_resolves_exactly_like_the_handler` (10 raw-truncate
shapes including `0`, `-1`, `True`, `False`, `"16"`, `1.5`), the KAT/generated-corpus parity tests,
and the three partition-invariance tests.

## `_kernels_keyed.py`: fill

None. The bar (kill every value/type/exception mutant) is already met by the existing test suite;
the 5 survivors are all message-text-only, the one shape the plan explicitly allows to be
adjudicated equivalent rather than chased. No test added, no production code touched.

## `_kernels_keyed.py`: five-field adjudication

| Function | Total mutants | Killed | Equivalent | Unreachable-by-contract | Tool-excluded |
|---|---:|---:|---:|---:|---:|
| `native_keyed_hash` | 27 | 22 | 5 | 0 | 0 |

Coverage: 100% (module-level, unchanged).

### Equivalent mutants (with reason)

**`native_keyed_hash`, mutants 4/7/12/13/14 (5 survivors, including the 1 flaky-kill):** each
mutates only the `message=` string on the `hash_requires_namespace` `StrategyError` raise (`None`,
dropped kwarg, marker-wrapped, upper-cased, lower-cased). The existing test
`test_missing_namespace_fails_closed_without_calling_the_kernel` asserts `exc_info.value.code ==
"hash_requires_namespace"` and `exc_info.value.strategy == "hash"` -- never `.message`, and no
other test or documented contract on this surface checks it. Equivalent by contract, the same
reasoning as `_kernels_scalar.py`'s message-text survivors above and T2's `_crypto_ext.py`
adjudications.

### Unreachable-by-contract / tool-excluded

None. `native_keyed_hash` has no PyO3 boundary of its own (it calls into the already-loaded
compiled kernel through `_crypto_ext.py`, out of this batch's scope) and no branch behind an
inadmissible type this scope doesn't already exercise.

## Readjudicate-killed result

`--readjudicate-killed` was run on `_kernels_keyed.py` only, per the plan (keyed hash is
crypto-adjacent; `_kernels_scalar.py` has no crypto surface and T0 already ran this exact module
without the flag). Result: **1 flaky-kill** (`mutant_12`, described above), already folded into the
corrected 22-killed/5-survived tally; the flaky-kill is itself a message-text mutant, so it changes
which bucket counts it (killed vs. equivalent-survivor) but not the bar outcome -- the bar cares
about value/type/exception mutants, and none of the 5 (flaky or not) are that shape.

## Production changes

None. The one demonstrated gap (`native_truncate`'s default `keep`) was closed with a test; no
logic in either module changed.

## Gates

- `ruff check` on the changed test file and both graded source files: clean.
- `ruff format --check` on the same: clean.
- `mypy src/decoy_engine testflight`: clean (428 source files).
- `pytest tests/native/ tests/parity/native/ -q`: 448 passed, 1 skipped (pre-existing: the shared
  KAT fixture file is not generated in this environment), 59 xfailed (pre-existing, unrelated).

## Bar

Kill every mutant that changes an output value, an output Arrow type, or a raised exception
type/code: met on both modules. `_kernels_scalar.py` closed its one real gap (the default-`keep`
value); `_kernels_keyed.py` needed no fill, its bar was already met. Every remaining survivor on
both modules (8 + 5 = 13) is message-text-only and adjudicated equivalent by contract above.

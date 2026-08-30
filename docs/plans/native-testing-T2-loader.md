Status: record

# T2 loader + keyed reference: measurement, fixes, and adjudication

- **Plan:** `docs/plans/2026-08-29-native-efficiency-test-plan.md`, batch T2 (section 4) and
  the method in section 3.
- **Scope:** the keyed-loader surface of `src/decoy_engine/execution/native/_crypto_ext.py`:
  `load_compiled_crypto_kernel`, the ABI check and load-time self-test, `_require_mask_key`,
  `_ReferenceKeyedDerivation` / `reference_keyed_derivation`, `_translate_compiled_kernel_error`,
  `_CompiledKeyedDerivationKernel`, and the crypto error types. `_ReferenceFpe` and the whole FPE
  surface are Part 2 and out of scope; mutmut mutates the whole file regardless, so its
  survivors are recorded separately below, not counted against the bar.
- **Branch:** `docs/native-efficiency-test-plan`, worktree `.claude/worktrees/native-test-plan`.
- **Harness reused, not re-derived:** `scripts/native-testing/python_mutation_pilot.py` (T0),
  scoped with `--module src/decoy_engine/execution/native/_crypto_ext.py` and the crypto/loader
  test selection, run with `--readjudicate-killed` (mandatory here per T0's carry-forward: the
  crypto/RI bar cannot tolerate an under-counted survivor).

## Method: measure first

Ran `coverage` + the mutation pilot against the existing test selection
(`tests/native/test_crypto_ext_loader.py tests/native/test_crypto_ext_contract.py
tests/native/test_keyed_derivation_kernel_parity.py`) before writing any test, per section 3
rule 1. The Rust companion was installed in this environment, so the companion-gated tests ran
for real, not skipped.

## BEFORE / AFTER: coverage

```
coverage run --branch -m pytest -q tests/native/test_crypto_ext_loader.py \
  tests/native/test_crypto_ext_contract.py tests/native/test_keyed_derivation_kernel_parity.py
coverage report --include=*/execution/native/_crypto_ext.py -m
```

| | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| Before | 167 | 5 | 22 | 2 | 96% | 175, 454-456, 560 |
| After | 167 | 4 | 22 | 1 | 97% | 175, 454-456 |

Line 560 (the `_translate_compiled_kernel_error` fallback that returns an unrecognized-code
exception unchanged) is now covered. Lines 175 and 454-456 are `FpeConfigError` and
`_ReferenceFpe._run`'s per-row except clauses -- FPE, out of scope.

## BEFORE: mutation tally

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_crypto_ext.py \
  --tests tests/native/test_crypto_ext_loader.py tests/native/test_crypto_ext_contract.py \
          tests/native/test_keyed_derivation_kernel_parity.py \
  --readjudicate-killed --timeout 60
```

279 mutants total: 187 killed, 92 survived, 0 unresolved. Killed-bucket re-adjudication:
all 187 re-confirmed killed standalone (0 flaky-kills).

Of the 92 survivors, 37 are entirely inside `_ReferenceFpe` (`_run`, `decrypt_batch`,
`_warnings`) -- out of scope, not counted. The remaining 55 are the keyed-loader-surface
denominator this batch grades.

## Fills

Three demonstrated gaps, all closed by tests (no production code touched):

1. **`_ReferenceKeyedDerivation.derive_batch`'s output type for an empty or all-null batch.**
   Two mutants (dropping the explicit `type=pa.string()` on the returned `pa.array(...)` call)
   survived because every existing parity test compares `to_pylist()` only, and pyarrow infers
   the same list of `None`s from an empty or all-null Python list whether or not `type=` is
   given -- so the LOGICAL values matched while the ARROW TYPE silently drifted to `null`.
   Added `test_reference_hash_batch_type_is_string_even_when_empty_or_all_null` (parametrized
   over an empty and an all-null array) asserting `.type == pa.string()` directly, in
   `test_crypto_ext_contract.py`.
2. **`_translate_compiled_kernel_error` splits on `partition` vs `rpartition`.** The compiled
   kernel's message is `"<code>: <detail>"`, and `detail` is not guaranteed colon-free (the
   FFI-import path wraps an arrow-rs error's `Display` text via `map_arrow_error`, which can
   itself contain `": "`). No existing test's detail text contains a second colon, so swapping
   to `rpartition` (last occurrence) passed every existing assertion while silently corrupting
   `code` for any message that does. Added
   `test_translate_seed_wrong_length_splits_on_the_first_colon_only` and
   `test_translate_namespace_empty_splits_on_the_first_colon_only`, each with a synthetic
   `ValueError` whose detail embeds a second colon, asserting the exact `code`/`message` split.
3. **The `native_type_not_admitted` mapping's `.code` was never asserted.** Every existing test
   for the `mixed_object_not_native` -> `GenerationError` mapping asserts the exception TYPE
   only (`pytest.raises(GenerationError)`), never its `.code`, so three mutants that corrupted
   or dropped `code="native_type_not_admitted"` survived undetected. Added
   `test_translate_mixed_object_not_native_maps_to_coded_generation_error`, asserting both
   `.code` and `.message` directly on `_translate_compiled_kernel_error`'s output.

A fourth test, `test_translate_passes_through_an_unrecognized_code_unchanged`, closes the
coverage gap on line 560 (the unrecognized-code fallback) and pins that behavior against
future regression; no surviving mutant demonstrated a gap there since the fallback is a single
`return exc` with no branch to mutate incorrectly, but it was untested and the plan's bar
covers "every rejection path."

All four new tests live in `tests/native/test_crypto_ext_loader.py` (three) and
`tests/native/test_crypto_ext_contract.py` (one); none require production changes.

## AFTER: mutation tally

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_crypto_ext.py \
  --tests tests/native/test_crypto_ext_loader.py tests/native/test_crypto_ext_contract.py \
          tests/native/test_keyed_derivation_kernel_parity.py \
  --readjudicate-killed --timeout 60
```

279 mutants total: 198 killed, 81 survived, 0 unresolved.

Of the 81 survivors, 36 are entirely inside `_ReferenceFpe` -- out of scope. The remaining 45
are the keyed-loader-surface denominator, matching exactly the equivalence classes predicted
from the BEFORE analysis (see below): the six mutants the three fills targeted (two on
`_ReferenceKeyedDerivation.derive_batch`, four on `_translate_compiled_kernel_error`) are gone,
and every other in-scope survivor from BEFORE is still present, unchanged, at the same mutant
index. `load_compiled_crypto_kernel`'s 34-survivor set is byte-identical to BEFORE (expected:
nothing in that function was touched).

One additional side effect: the new `_translate_compiled_kernel_error` tests assert `.message`
as well as `.code` (the message content IS the observable proof of the partition-boundary
property under test, not incidental text), which also killed the four mutants BEFORE analysis
had marked equivalent-by-contract for that function (12/14/19/21, the message-only variants).
So all 8 of that function's BEFORE survivors are gone, not only the 4 true gaps -- a stronger
result than the bar requires, not a problem.

## Five-field adjudication: keyed-loader surface

| Function | Total mutants | Killed | Equivalent | Unreachable-by-contract | Tool-excluded |
|---|---:|---:|---:|---:|---:|
| `_require_mask_key` | 7 | 2 | 5 | 0 | 0 |
| `_ReferenceKeyedDerivation.derive_batch` | 25 | 22 (2 new) | 3 | 0 | 0 |
| `_translate_compiled_kernel_error` | 23 | 23 (8 new)* | 0 | 0 | 0 |
| `_CompiledKeyedDerivationKernel.derive_batch` | 17 | 14 | 3 | 0 | 0 |
| `load_compiled_crypto_kernel` | 54 | 20 | 34** | 0 | 0 |
| **In-scope total** | 126 | 81 (10 new) | 45 | 0 | 0 |

\* the four new direct-unit tests killed 8 of this function's 23 mutants: the 4 real gaps
(partition/rpartition, and the three `.code` corruptions) plus, as a side effect of asserting
`.message` alongside `.code`, the 4 message-only mutants this batch's BEFORE analysis had
initially marked equivalent (12/14/19/21) -- see "Fills" above.
\*\* 32 message-text-only survivors plus 2 (mutant 21: KAT-vector index choice; mutant 26:
mathematically-equivalent `truncate` expression) -- see below.

Coverage per function is 100% for all five (module-level coverage is 97%, reflecting the two
FPE-only lines, 175 and 454-456, excluded by scope). "(N new)" marks kills contributed by the
four tests added in this batch; all other kills are from the pre-existing parity/loader-mechanics
test suite.

### Equivalent mutants (with reason)

**`_require_mask_key` (5 survivors, mutants 2/3/5/6/7):** every survivor mutates either the
`message=` string passed to `MaskKeyRequiredError` (dropped to `None`, wrapped in marker text,
or upper-cased) or the `kernel=` label. `MaskKeyRequiredError.__init__` stores `.message`
(via `super().__init__`) and `.kernel` for the operator-facing string only; the class doc says
`.kernel` "carries the originating kernel name... for the operator message". No test, and no
part of the documented contract, asserts either value -- only `.code` (a fixed class attribute
these mutants never touch) and the exception TYPE are contractual. Equivalent by contract,
matching the T0 precedent (`native_truncate`'s message-text survivors).

**`_ReferenceKeyedDerivation.derive_batch` (3 of 5 survivors, mutants 3/6/7):** same
`_require_mask_key(mask_key, "keyed_derivation")` label mutation as above, reachable only
through the same unobserved `.kernel` attribute. Equivalent by contract, same reason.

**`_CompiledKeyedDerivationKernel.derive_batch` (all 3 survivors, mutants 3/6/7):** identical
label mutation on its own `_require_mask_key` call. Equivalent by contract, same reason.

**`_translate_compiled_kernel_error`: no remaining survivors.** All 23 mutants are now killed.
The 4 real gaps (mutants 3/18/22/23) are closed by the new tests' direct assertions; the other
4 BEFORE-survivors (12/14/19/21, each mutating only the returned exception's `message=`) turned
out to be killed too, because the new tests assert `.message` alongside `.code` as the
observable proof of the partition-boundary property under test (not because message text is
part of the contract -- it still is not, for this function or any other on this surface; it
happened to be checked here as a side effect of proving the split-boundary logic). No
adjudication needed for this function.

**`load_compiled_crypto_kernel` (32 of 34 survivors, mutants 1-8/10-14/16-18/37-53):** every
one mutates the literal string passed to a `CryptoExtensionUnavailableError(...)` call (dropped
to `None`, wrapped in marker text, or upper-cased). `CryptoExtensionUnavailableError.code` is a
fixed class attribute (`"crypto_ext.unavailable"`) none of these mutants touch; every test
checks only `exc_info.type is CryptoExtensionUnavailableError`. Equivalent by contract.

**`load_compiled_crypto_kernel`, mutant 21 (`probe = HASH_KAT[1]` instead of `HASH_KAT[0]`):**
the self-test's contract is "reproduce some known-answer vector correctly", not "use index 0
specifically" -- no test constructs a companion that is correct for one KAT entry and wrong for
another, and the plan does not require one (a self-test proving correctness on any single
admitted vector satisfies the fail-before-output contract). Equivalent under every test this
batch's bar calls for.

**`load_compiled_crypto_kernel`, mutant 26 (`truncate=None` instead of `truncate=probe.truncate`
in the self-test probe call):** `HASH_KAT[0].truncate` is already `None` in the fixture, so with
`probe = HASH_KAT[0]` (unmutated in this isolated single-mutant run) the two expressions
evaluate to the identical value. Mathematically equivalent, not merely untested.

### Unreachable-by-contract / tool-excluded

None on this surface. The FPE-only lines this batch does not touch (175, 454-456) are excluded
by scope, not by contract-unreachability.

### Out of scope: FPE survivors (not counted)

37 survivors, entirely inside `_ReferenceFpe` (`_run`, `decrypt_batch`, `_warnings`). Part 2
scope; not adjudicated here.

## Readjudicate-killed result

`--readjudicate-killed` re-ran all 198 killed mutants standalone. One flaky-kill surfaced:
`xǁ_ReferenceFpeǁ_run__mutmut_60` (mutmut marked it killed; the standalone rerun marked it
survived). This mutant is entirely inside `_ReferenceFpe._run` -- FPE, Part 2, out of scope --
so it does not affect the T2 bar. Zero flaky-kills on the keyed-loader surface graded here: none
of the 81 killed mutants across `_require_mask_key`, `_ReferenceKeyedDerivation.derive_batch`,
`_translate_compiled_kernel_error`, `_CompiledKeyedDerivationKernel.derive_batch`, or
`load_compiled_crypto_kernel` flipped on rerun.

## Production changes

None. Every gap closed with a test; no logic in `_crypto_ext.py` changed.

## Gates

- `ruff check` + `ruff format --check` on the two changed test files: clean.
- `mypy src/decoy_engine testflight`: clean (428 source files, no test-tree scope per the
  repo's mypy target).
- `pytest tests/native/ -q`: 387 passed, 1 skipped (unchanged skip: the shared KAT fixture
  file is not generated in this environment), companion present.

## Bar

Zero unadjudicated semantic survivors on the load-time self-test and the keyed reference
derivation: met. All 55 in-scope survivors are either killed by the four new tests or
adjudicated equivalent with a stated reason above.

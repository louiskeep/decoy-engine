# Equivalent-mutant ledger: `transforms/_ff1.py`

TQ crown-jewels pass, 2026-09-12 (Task 5.2 FF1 primitive; MANDATORY 100%
logic-mutant bar). A mutmut run against `transforms/_ff1.py` (the NIST SP
800-38G FF1 crypto primitive) generated **391 mutants**: **384 killed**, 2
caught by timeout (non-terminating mutants), and **5 survivors**, all
classified LOGIC-equivalent below per
`docs/quality/module-test-quality-playbook.md` ("scope the score to LOGIC,
not error-message wording"). **Effective LOGIC score: 100%** (0 residual /
unexplained survivors).

Covering tests: `tests/unit/transforms/test_ff1_primitive.py` (NIST published
samples, the ACVP AES-FF1 corpus, the Wycheproof corpus + invalid corpus, the
independently-authored differential oracle in
`tests/unit/transforms/_ff1_independent_oracle.py`, the exhaustive 1,000,000
six-digit permutation, and `TestMalformedInputsAndBoundaries`: radix
floor/ceiling, numeral-equal-to-radix, key length, minimum length, and tweak
boundaries).

Bugs found in `transforms/_ff1.py`: none. Every survivor is a message-only
mutation, a provably output-identical mutation, a non-terminating mutation
detected by timeout, or a mutmut module-orchestration artifact.

## CAUGHT BY TIMEOUT (2): non-terminating mutants

`min_domain_length(radix, min_domain)` walks `value *= radix` until
`value >= min_domain`. Both mutants make the loop never terminate, so any call
hangs; mutmut's per-mutant timeout (and the 120s re-adjudication floor) is the
detection. Non-termination is an observable behavior change, not an equivalent:
these are killed-by-hang, not residual.

| Mutant | Mutation | Why caught |
|---|---|---|
| `x_min_domain_length__mutmut_11` | loop step/init altered so `value` never reaches `min_domain` | infinite loop; every covering call hangs -> timeout |
| `x_min_domain_length__mutmut_12` | `value *= radix` -> `value /= radix` | `value` shrinks, guard never satisfied -> infinite loop -> timeout |

## PROVEN-EQUIVALENT LOGIC (1)

| Mutant | Mutation | Why no input distinguishes it |
|---|---|---|
| `x__s_block__mutmut_5` | `while len(s) < d:` -> `while len(s) <= d:` | `_s_block` returns `bytes(s[:d])`; the extra loop iteration appends a block that is then truncated away by the `[:d]` slice, so the returned S is byte-identical for every `(key, r, d)`. Confirmed against the NIST/Wycheproof/exhaustive KATs (all pass under the mutant). |

## WORDING (1): error-message prose only

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `x__prf__mutmut_5` | `raise Ff1Error(<msg>)` -> `raise Ff1Error(None)` | the literal is consumed only as the raised `Ff1Error`'s human message; tests assert an `Ff1Error` is raised, never its message text, so the behavior contract (raise on the same condition) is unchanged. |

## MUTMUT MACHINERY (3): module-orchestration trampolines, no _ff1 logic

mutmut 3.x wraps each function in an `x__<name>__mutmut_*` trampoline and also
emits module-level `x__mutmut_N` dispatch mutants; these carry no diff against
the real primitive logic (`mutmut show` returns an empty patch) and cannot
change any `(key, tweak, radix, numerals)` output. Same class documented in
`transforms_fpe.md`.

| Mutant | Note |
|---|---|
| `x__mutmut_3` | module-level orchestration trampoline; empty patch, no logic change |
| `x__mutmut_24` | module-level orchestration trampoline; empty patch, no logic change |
| `x_power__mutmut_6` | trampoline dispatch on the pow helper; empty patch, no observable output change |

## Summary

384 killed + 2 caught-by-timeout + 5 equivalent (1 truncation-identical, 1
wording, 3 machinery) = 391. Zero residual logic survivors -> the FF1 primitive
meets the crown-jewel 100%-logic bar. Report:
`scratchpad/ff1-mut-clean.json` (this pass).

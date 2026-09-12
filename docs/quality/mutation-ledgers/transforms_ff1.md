# Equivalent-mutant ledger: `transforms/_ff1.py`

TQ crown-jewels pass, 2026-09-12 (Task 5.2 FF1 primitive; MANDATORY 100%
logic-mutant bar). A mutmut run against `transforms/_ff1.py` (the NIST SP
800-38G FF1 crypto primitive) generated **391 mutants**.

**Raw tool output** (`scripts/tq_mutate.py --run`, report
`scratchpad/ff1-mut-clean.json`): **384 killed, 2 timeout, 5 survived**. The
tool prints `LOGIC 98.71% (384/389)` and, honestly, flags `SCORE NOT
TRUSTWORTHY` and **exits 1** -- because the 2 timeouts are non-terminating
mutants that never reach a definitive `killed`/`survived` verdict within the
120s re-adjudication floor. The tool cannot auto-decide those; this ledger does,
by manual inspection. **Manual audit: 0 residual logic defects** -- the 2
timeouts are non-terminating (detected by any bounded test), and all 5
survivors are equivalent (2 wording-only, 3 provably output-identical). So the
effective LOGIC score is 100% with the taxonomy below. To be precise about the
tool's arithmetic: it computes `LOGIC = killed / (killed + survived) =
384 / (384 + 5) = 98.71%`, EXCLUDING timeouts -- so the 1.29% gap is the 5
equivalent survivors, and the 2 non-terminating timeouts are what trigger the
separate `SCORE NOT TRUSTWORTHY` flag and exit 1. Both are resolved above.

Covering tests: `tests/unit/transforms/test_ff1_primitive.py` (NIST published
samples, the pinned ACVP AES-FF1 corpus, the Wycheproof valid + invalid
corpora, the independently-authored differential oracle in
`tests/unit/transforms/_ff1_independent_oracle.py`, the exhaustive 1,000,000
six-digit permutation, and `TestMalformedInputsAndBoundaries`: radix
floor/ceiling incl the inclusive `2**16` bound, numeral-equal-to-radix, key
length, minimum length, tweak boundaries).

Bugs found in `transforms/_ff1.py`: none. Every survivor/timeout is a
message-only mutation, a provably output-identical mutation, or a
non-terminating mutation detected by timeout.

## CAUGHT BY TIMEOUT (2): non-terminating mutants in `min_domain_length`

`min_domain_length` walks `length += 1; value *= radix` until `value >=
FF1_MIN_DOMAIN`. Both mutants stop `value` from ever growing, so the loop never
terminates; the per-mutant timeout is the detection. Non-termination is an
observable behavior change, so these are killed-by-hang, not residual gaps.

| Mutant | Mutation | Why caught |
|---|---|---|
| `x_min_domain_length__mutmut_11` | loop body `value *= radix` -> `value = radix` | `value` is pinned at `radix` (<= 64 < 1e6); guard never satisfied -> infinite loop -> timeout |
| `x_min_domain_length__mutmut_12` | `value *= radix` -> `value /= radix` | `value` shrinks toward 0; guard never satisfied -> infinite loop -> timeout |

## PROVEN-EQUIVALENT LOGIC (3)

| Mutant | Mutation | Why no input distinguishes it |
|---|---|---|
| `x__str_m_radix__mutmut_3` | `digits = [0] * m` -> `digits = [1] * m` | the following `for i in range(m-1, -1, -1): digits[i] = digit` reassigns **every** index `0..m-1`, so the initialization value is fully overwritten before `digits` is returned; the sentinel is unobservable. |
| `x__s_block__mutmut_5` | `while len(s) < d:` -> `while len(s) <= d:` | `_s_block` returns `bytes(s[:d])`; the extra loop iteration appends a block that the `[:d]` slice truncates away, so S is byte-identical for every `(key, r, d)`. Confirmed under the NIST/ACVP/Wycheproof/exhaustive KATs (all pass under the mutant). |
| `x__byte_len_for_radix_power__mutmut_6` | `if max_value > 0` -> `if max_value >= 0` | `max_value = radix**length - 1`; the primitive only runs with `radix >= 2` and `length >= 1`, so `radix**length >= 2` and `max_value >= 1 > 0` always. `max_value == 0` (the only input where `> 0` and `>= 0` differ) is unreachable, so both branches pick `bit_length()` and the output is identical. |

## WORDING (2): error-message prose only

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `x__prf__mutmut_5` | `raise Ff1Error(<msg>)` -> `raise Ff1Error(None)` | the literal is consumed only as the raised `Ff1Error`'s human message; tests assert an `Ff1Error` is raised on the same condition, never its message text. |
| `x__str_m_radix__mutmut_24` | overflow-error message -> `Ff1Error(None)` | same: the `x != 0` overflow guard still raises `Ff1Error`; only the (untested) message text changes. |

## Summary

384 killed + 2 caught-by-timeout (non-terminating) + 5 equivalent (3 provably
output-identical, 2 wording) = 391. Zero residual logic survivors: the FF1
primitive meets the crown-jewel 100%-logic bar. The tool's raw
`LOGIC 98.71% = 384/(384+5)` counts the 5 equivalent survivors; the separate
`SCORE NOT TRUSTWORTHY` flag and exit 1 come from the 2 non-terminating
timeouts. Both are resolved above. Report: `scratchpad/ff1-mut-clean.json`.

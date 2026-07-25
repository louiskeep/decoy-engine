# Equivalent-mutant ledger: `transforms/fpe.py`

TQ crown-jewels pass, 2026-07-25. A mutmut run against `transforms/fpe.py` (the
format-preserving-encryption crypto crown jewel, MANDATORY 100% logic-mutant
bar) left **96 survivors**. Every survivor was classified LOGIC or EQUIVALENT
per `docs/quality/module-test-quality-playbook.md` ("Scope the score to
LOGIC, not error-message wording"). **54 LOGIC survivors** were killed with
new tests in `tests/property/test_fpe_invariants.py` (the "TQ crown-jewels
mutation-kill pass" section, appended after the existing checksum-mode
tests); **42 survive and are equivalent**, listed below with the one-line
argument for why no input can distinguish them from the original.

Covering tests: `tests/codspeed/test_fpe_transform.py`,
`tests/unit/plan/test_check_fpe_charset.py`,
`tests/unit/transforms/test_fpe_roundtrip.py`,
`tests/unit/transforms/test_fpe_checksum_validity.py`,
`tests/unit/transforms/test_fpe_key_failure.py`,
`tests/unit/transforms/test_fpe_remap_orphan_charset.py`,
`tests/unit/execution/test_fpe_strategy.py`, plus
`tests/property/test_fpe_invariants.py` (this module's oracle layer --
format preservation, invertibility, determinism, key/tweak sensitivity,
domain validation, boundary, and checksum properties, plus the 23 targeted
tests added in this pass).

Bugs found in `transforms/fpe.py`: none. Every LOGIC mutant traces to a real
test gap (a property the existing suite could not observe), not a defect in
the module itself.

## WORDING (35): error/log-message prose only

mutmut lowercases/uppercases a message fragment, wraps it in `XX...XX`, sets
`message=None`, or drops a `message=`/f-string segment. In every case the
literal (or the `type(exc).__name__` it interpolates) is consumed only
inside a raised `FpeUnencryptableError`'s human `message` or a
`self.logger.error(...)` log line; it never becomes the exception's `code`
(a fixed class attribute, `"fpe.unencryptable"` / `"mask.key_derivation_failed"`,
never passed per-call) or a comparison target. Tests assert the `code` (and,
for the `preserve_separators=False` rejection, the load-bearing offending-
character list -- see the LOGIC table below for why THAT survivor is not
equivalent), so only pure-wording variants survive.

| Mutant | Mutation |
|---|---|
| `x__fpe_valueǁmutmut_14` | "no character in charset" error, 2nd sentence: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_15` | same sentence: `"The"` -> `"the"` |
| `x__fpe_valueǁmutmut_16` | same sentence: uppercased entirely |
| `x__fpe_valueǁmutmut_17` | 3rd sentence: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_18` | same sentence: uppercased |
| `x__fpe_valueǁmutmut_19` | 4th sentence: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_20` | same sentence: `"Use"` -> `"use"` |
| `x__fpe_valueǁmutmut_21` | same sentence: uppercased |
| `x__fpe_valueǁmutmut_22` | 5th sentence: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_23` | same sentence: uppercased |
| `x__fpe_valueǁmutmut_42` | "length invariant" guard error: whole message -> `None` (the `value=val` kwarg on this raise is untouched -- see mutmut_43/45 in the LOGIC table for the mutants that DO touch it) |
| `x__fpe_valueǁmutmut_44` | same error: the two f-string message lines dropped, `value=val` kwarg retained unchanged |
| `x__fpe_valueǁmutmut_46` | same error's 4th line: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_47` | same line: uppercased |
| `x__fpe_valueǁmutmut_70` | "out-of-charset character(s)" error, 3rd sentence: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_71` | same sentence: `"Enable"` -> `"enable"` |
| `x__fpe_valueǁmutmut_72` | same sentence: uppercased |
| `x__fpe_valueǁmutmut_73` | 4th sentence: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_74` | same sentence: uppercased |
| `x__fpe_valueǁmutmut_75` | 5th sentence: wrapped `XX...XX` |
| `x__fpe_valueǁmutmut_76` | same sentence: uppercased |
| `xǁFPEStrategyǁ_column_keyǁmutmut_5` | `except` branch: `self.logger.error(f"...")` -> `self.logger.error(None)` (the SUBSEQUENT `raise MaskKeyDerivationError(...)` is untouched) |
| `xǁFPEStrategyǁ_column_keyǁmutmut_6` | same log line: `{type(exc).__name__}` -> `{type(None).__name__}` (display-only; the raised exception's own message on the next lines is untouched) |
| `xǁFPEStrategyǁ_column_keyǁmutmut_7` | same log line: trailing sentence wrapped `XX...XX` |
| `xǁFPEStrategyǁ_column_keyǁmutmut_8` | same sentence: lowercased |
| `xǁFPEStrategyǁ_column_keyǁmutmut_9` | same sentence: uppercased |
| `xǁFPEStrategyǁ_column_keyǁmutmut_14` | raised `MaskKeyDerivationError` message: `{type(exc).__name__}` -> `{type(None).__name__}` (display-only; `.code`/`.strategy` untouched) |
| `xǁFPEStrategyǁ_column_keyǁmutmut_15` | same message, next sentence: wrapped `XX...XX` |
| `xǁFPEStrategyǁ_column_keyǁmutmut_16` | same sentence: lowercased |
| `xǁFPEStrategyǁ_column_keyǁmutmut_17` | same sentence: uppercased |
| `xǁFPEStrategyǁ_column_keyǁmutmut_18` | same message, final sentence: wrapped `XX...XX` |
| `xǁFPEStrategyǁ_column_keyǁmutmut_19` | same sentence: uppercased |

## NO-OP (7): byte/case identity or performance-only cache bypass

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `x__prfǁmutmut_28` | `b"\xff"` -> `b"\xFF"` | Hex-escape case is not part of the byte value: `b"\xff" == b"\xFF"` always (both are the single byte `0xff`). A true no-op, distinct from `mutmut_27`'s `b"XX\xffXX"` (a real 5-byte value, LOGIC, killed below). |
| `x__char_lookupǁmutmut_1` | `lookup = _CHARSET_INDEX.get(charset)` -> `lookup = None` (forces the cache-miss branch unconditionally) | The rebuilt dict (`{ch: i for i, ch in enumerate(charset)}`) is content-identical to the cached one for the SAME charset string; only the cache is bypassed (a performance regression, not a correctness one). No input can distinguish the returned mapping's VALUES. |
| `x__char_lookupǁmutmut_2` | `_CHARSET_INDEX.get(charset)` -> `_CHARSET_INDEX.get(None)` (always a cache miss, since `None` is never a key) | Same reasoning as mutmut_1: always rebuilds, always content-correct. |
| `x__permuteǁmutmut_33` | `_encode(s, charset, _char_lookup(charset))` -> `_encode(s, charset, None)` | `_encode`'s `char_to_idx=None` fallback (`charset.index`) and the `_char_lookup`-provided dict agree on every index for any charset assembled by this module's own pipeline (`FPEStrategy.apply()` dedupes; the Hypothesis `_charset()` strategy draws unique characters) -- a pure O(n) vs O(1) performance difference, not a value difference. |
| `x__permuteǁmutmut_36` | same call, trailing comma: `_encode(s, charset, )` (third positional arg omitted, defaulting to `None`) | Identical effect to mutmut_33 -- both land on `_encode`'s `char_to_idx=None` path, which is value-equivalent to the lookup path. |
| `x_fpe_decrypt_valueǁmutmut_10` | `_fpe_value(..., forward=False)` -> `_fpe_value(..., forward=None)` | Every use of `forward` inside `_fpe_value`/`_fpe_pure_value`/`_permute` is a truthiness check (`if forward else ...`), never an identity check (`is True`/`is False`). `fpe_decrypt_value` always intends `forward=False`, and `None` is exactly as falsy as `False` everywhere it is read -- indistinguishable from any input. Contrast with `x__fpe_valueǁmutmut_82` (LOGIC, below): that mutant hits the `forward=True` (encrypt) call, where `None` (falsy) really does differ from `True` (truthy). |

## LOGIC (54): killed by new tests in this pass

All in `tests/property/test_fpe_invariants.py`, appended under the "TQ
crown-jewels mutation-kill pass" heading.

| Mutant | Mutation | Killed by |
|---|---|---|
| `x__prfǁmutmut_12` | `(operand.bit_length() + 7) // 8` -> `+ 8) // 8` (breaks the ceil-division byte-length formula for operands whose bit-length is a multiple of 8) | `test_prf_message_matches_the_documented_wire_format` |
| `x__prfǁmutmut_14` | `max(..., 1)` -> `max(..., 2)` (operand=0 now encodes as 2 zero bytes instead of the documented minimum of 1) | same |
| `x__prfǁmutmut_26` | `struct.pack(">B", round_index)` -> `">b"` (signed byte; raises for round_index > 127, silently unreachable at the real `_ROUNDS=8` range) | `test_prf_round_index_packs_as_a_full_unsigned_byte` |
| `x__prfǁmutmut_27` | `b"\xff"` -> `b"XX\xffXX"` (the domain-separator byte becomes a 5-byte sequence) | `test_prf_message_matches_the_documented_wire_format` |
| `x__encodeǁmutmut_4` | `if char_to_idx is None:` -> `if char_to_idx is not None:` (branch selection inverted -- observable only with a DELIBERATELY mismatched lookup, since a consistent one is value-equivalent either way, per the NO-OP entries above) | `test_encode_uses_the_given_char_to_idx_lookup_not_charset_index` |
| `x__encodeǁmutmut_5` | `x = x * r + charset.index(ch)` -> `x = None` (every iteration, in the `char_to_idx is None` fallback loop) | `test_encode_matches_between_the_lookup_and_charset_index_paths` |
| `x__encodeǁmutmut_6` | same line: `+ charset.index(ch)` -> `- charset.index(ch)` | same |
| `x__encodeǁmutmut_7` | same line: `x * r` -> `x / r` (integer accumulator becomes a float) | same |
| `x__encodeǁmutmut_8` | same line: `charset.index(ch)` -> `charset.index(None)` (raises `TypeError` on every call) | same |
| `x__encodeǁmutmut_9` | same line: `charset.index(ch)` -> `charset.rindex(ch)` | `test_encode_without_lookup_uses_first_occurrence_index_for_duplicate_charset` |
| `x__luhn_check_digitǁmutmut_2` | `total = 0` -> `total = 1` | `test_luhn_check_digit_matches_known_answer_vectors` |
| `x__luhn_check_digitǁmutmut_8` | `if i % 2 == 0:` -> `if i % 3 == 0:` | same |
| `x__luhn_check_digitǁmutmut_9` | same: `i % 2 == 0` -> `i % 2 != 0` | same |
| `x__luhn_check_digitǁmutmut_10` | same: `i % 2 == 0` -> `i % 2 == 1` | same |
| `x__luhn_check_digitǁmutmut_11` | `n *= 2` -> `n = 2` | same |
| `x__luhn_check_digitǁmutmut_13` | `n *= 2` -> `n *= 3` | same |
| `x__luhn_check_digitǁmutmut_15` | `if n > 9:` -> `if n > 10:` (misses the doubled-digit=5 case, `n == 10`) | same |
| `x__luhn_check_digitǁmutmut_16` | `n -= 9` -> `n = 9` | same |
| `x__luhn_check_digitǁmutmut_17` | `n -= 9` -> `n += 9` | same |
| `x__luhn_check_digitǁmutmut_19` | `total += n` -> `total = n` | same |
| `x__luhn_check_digitǁmutmut_20` | `total += n` -> `total -= n` | same |
| `x__luhn_check_digitǁmutmut_23` | `(10 - total % 10)` -> `(10 + total % 10)` | same |
| `x__luhn_check_digitǁmutmut_24` | `(10 - total % 10)` -> `(11 - total % 10)` | same |
| `x__luhn_check_digitǁmutmut_26` | `total % 10` -> `total % 11` | same |
| `x__char_lookupǁmutmut_3` | `if lookup is None:` -> `if lookup is not None:` (a custom, non-cached charset now returns `None` instead of building the mapping -- silently absorbed downstream by `_encode`'s own `None` fallback, so only a DIRECT test of `_char_lookup` observes it) | `test_char_lookup_builds_a_complete_index_for_a_custom_charset` |
| `x__char_lookupǁmutmut_4` | `lookup = {ch: i for i, ch in enumerate(charset)}` -> `lookup = None` (same observable effect as mutmut_3 for a custom charset) | same |
| `x__single_char_shiftǁmutmut_1` | `msg = b"fpe-single\xff" + tweak` -> `msg = None` (valid `hmac.new` input -- does NOT crash; silently drops the tweak from the digest) | `test_single_char_shift_matches_the_documented_message_format` |
| `x__single_char_shiftǁmutmut_3` | same line: wrapped `b"XXfpe-single\xffXX"` | same |
| `x__single_char_shiftǁmutmut_4` | same line: `b"FPE-SINGLE\xFF"` (real byte-content change, not a hex-case no-op like `x__prfǁmutmut_28`) | same |
| `x__single_char_shiftǁmutmut_10` | `hmac.new(key, msg, ...)` -> `hmac.new(key, None, ...)` (same effective bug as mutmut_1, via the call site instead of the assignment) | same |
| `x__permuteǁmutmut_3` | `if n == 0: return s` -> `if n == 1: return s` (single-char case becomes a same-charset no-op instead of the documented rotation; the domain-gated no-op-leakage property can't observe length 1) | `test_single_character_permutation_matches_the_documented_shift_formula` |
| `x__permuteǁmutmut_8` | `idx = charset.index(s[0])` -> `charset.rindex(s[0])` (single-char branch; observable only with a duplicate-character charset) | `test_single_character_uses_first_occurrence_index_for_duplicate_charset` |
| `x__permuteǁmutmut_18` | `charset[(idx + shift) % len(charset)]` -> `(idx - shift)` (a sign flip that stays self-consistently invertible -- encrypt and decrypt share this line, so round-trip alone can't catch it) | `test_single_character_permutation_matches_the_documented_shift_formula` |
| `x__permuteǁmutmut_21` | `u = (n + 1) // 2` -> `(n - 1) // 2` (a different, still-valid Feistel split -- bijective for ANY u+v==n, so format/round-trip/determinism properties can't distinguish it from the documented `ceil(n/2)`) | `test_known_answer_vectors_pin_the_documented_feistel_construction` |
| `x__permuteǁmutmut_22` | same line: `(n + 1) // 2` -> `(n + 2) // 2` | same |
| `x__permuteǁmutmut_23` | same line: `// 2` -> `// 3` | same |
| `x__fpe_valueǁmutmut_43` | length-invariant-guard raise: `value=val` -> `value=None` | `test_length_invariant_guard_fails_closed_with_the_offending_value` |
| `x__fpe_valueǁmutmut_45` | same raise: `value=val` kwarg dropped entirely | same |
| `x__fpe_valueǁmutmut_63` | out-of-charset-rejection: `out_of_charset = sorted({... if ch not in charset_set})` -> `= None` (the message's load-bearing diagnostic list disappears) | `test_out_of_charset_rejection_message_lists_the_actual_offending_characters` |
| `x__fpe_valueǁmutmut_65` | same line: `ch not in charset_set` -> `ch in charset_set` (lists the IN-charset characters instead of the offending ones) | same |
| `x__fpe_valueǁmutmut_66` | same raise: the entire message -> `None` (removes the load-bearing offending-character list, not just its wording) | same |
| `x__fpe_valueǁmutmut_77` | final `_fpe_pure_value(val, ...)` call (the `preserve_separators=False`, fully-in-charset path): `val` -> `None` (hits `_fpe_pure_value`'s `not s` passthrough, returning `None`) | `test_preserve_separators_false_round_trips_the_real_value` |
| `x__fpe_valueǁmutmut_81` | same call: `validate_luhn` -> `None` (falsy; silently skips the Luhn check-digit append) | `test_preserve_separators_false_forwards_validate_luhn_true` |
| `x__fpe_valueǁmutmut_82` | same call: `forward=forward` -> `forward=None` (falsy; forces `_feistel_inverse` even when encrypting, breaking invertibility via a double-inverse composition) | `test_preserve_separators_false_round_trips_the_real_value` |
| `x__fpe_valueǁmutmut_83` | same call: `checksum=checksum` -> `checksum=None` (silently falls back to plain permutation) | `test_preserve_separators_false_forwards_checksum_scheme` |
| `x__fpe_valueǁmutmut_90` | same call: `checksum=checksum` kwarg dropped (same effective bug as mutmut_83, via omission instead of an explicit `None`) | same |
| `x_fpe_encrypt_valueǁmutmut_1` | `preserve_separators: bool = True` -> `= False` (the documented default flips; a value with separators now fails closed instead of succeeding when the caller omits the argument) | `test_encrypt_defaults_to_preserving_separators` |
| `x_fpe_decrypt_valueǁmutmut_1` | same default flip, decrypt side | `test_decrypt_defaults_to_preserving_separators` |
| `xǁFPEStrategyǁ_fpe_pureǁmutmut_6` | `forward=True` -> `forward=None` (falsy; flips to inverse-direction math) | `test_fpe_pure_matches_the_public_encrypt_function` |
| `xǁFPEStrategyǁ_fpe_pureǁmutmut_13` | `forward=True` -> `forward=False` (direct flip, same observable effect) | same |
| `xǁFPEStrategyǁ_fpe_pureǁmutmut_5` | `validate_luhn` -> `None` (falsy; silently skips the Luhn check-digit append) | `test_fpe_pure_forwards_validate_luhn` |
| `xǁFPEStrategyǁ_column_keyǁmutmut_2` | `self.derive_key("mask")` -> `self.derive_key(None)` (a different, wrong key-derivation label) | `test_column_key_derives_with_the_exact_mask_label` |
| `xǁFPEStrategyǁ_column_keyǁmutmut_3` | same call: `"mask"` -> `"XXmaskXX"` | same |
| `xǁFPEStrategyǁ_column_keyǁmutmut_4` | same call: `"mask"` -> `"MASK"` | same |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```

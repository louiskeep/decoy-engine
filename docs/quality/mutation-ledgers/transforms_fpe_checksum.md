# Mutation grading: `transforms/_fpe_checksum.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_fpe_checksum.py` (199 LOC) is the
checksum-scheme body permutation for the FPE mask strategy (split out of
`transforms/fpe.py`): `_validate_scheme_length` fails closed on an invalid length
per scheme, and `_fpe_checksum_permute` permutes the non-check-digit body (reusing
`transforms.fpe._permute`) and recomputes the scheme's check digit so the output
is valid-by-construction. Schemes: npi, isbn13, ean13, gtin (four legal lengths),
vin, luhn (variable). Crypto-adjacent -> 100% LOGIC bar.

**Grade scope: FOCUSED selection only** (`tests/unit/transforms/test_fpe_checksum_validity.py`).

## Numbers

**193 mutants: 131 killed (68% baseline), 62 survived -> 153 killed after this
pass, 40 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **22 LOGIC survivors killed** with 13 new tests. 0 product bugs.
- **40 EQUIVALENT survivors** (message-prose, an IBAN reroute preserving all
  machine fields, and an unreachable defensive raise). All verified.

## LOGIC (22): killed by new tests

All in `tests/unit/transforms/test_fpe_checksum_validity.py`.

### Direction-flip + corrupted-alphabet (round-trip oracle)

The `forward=None` mutants and a duplicate-char permutation alphabet still produce
a checksum-valid output, so validity-only tests miss them. `TestFpeChecksumRoundTrip`
asserts `decrypt(encrypt(x)) == x` byte-exactly per scheme:

| Mutants | Mutation | Killed by |
|---|---|---|
| `permute_54` | luhn/ean13/gtin `forward=None` | `test_luhn_roundtrip` / `_ean13_` / `_gtin_` |
| `permute_77` | npi `forward=None` | `test_npi_roundtrip` |
| `permute_104` | isbn13 `forward=None` | `test_isbn13_roundtrip` |
| `permute_140` | vin `forward=None` | `test_vin_roundtrip` |
| `permute_126` | vin `"XXXX".join` -> duplicate-char alphabet (valid but not invertible) | `test_vin_roundtrip` |

### Error machine-field corruption

`TestFpeChecksumErrorFields` asserts the raised `FpeChecksumError`'s `.scheme` and
`.code`: `validate_13/15` (exact-length scheme), `validate_29/30/31` (min-length
scheme None / dropped-message TypeError / dropped scheme), `permute_3/5`
(unknown-scheme), `permute_17/19/34/35` (IBAN raise).

### Length-guard boundary / bypass

`TestFpeChecksumLengthBoundary` + the min-length error test: `validate_23/24`
(`minimum=None` / `.get(None)` -> luhn min-length check skipped), `validate_27`
(`<` -> `<=` rejects the shortest legal luhn), `permute_128` (vin `< 2` -> `<= 2`
collapses a 2-symbol alphabet to the digit fallback; pinned by
`test_vin_two_char_charset_used_verbatim`).

### VIN charset fallback (main-loop re-verify catch)

`permute_130` (vin fallback `vin_charset = _DIGITS_ONLY` -> `None`): reached when
the caller's charset filters to fewer than two VIN-alphabet chars. The classify
agent's VIN tests used a 2-char charset and never entered the fallback, so this
survived the first pass; the main-loop mutmut re-verify surfaced it. Killed by
`test_fpe_vin_narrow_charset_falls_back_to_digits` (a degenerate `"0" * 17` value
with charset `"0"` triggers the fallback; real code produces a valid 17-char VIN,
the mutant passes `None` to `_permute` -> TypeError).

## EQUIVALENT (40)

- **Message/prose-only (34)** -- machine fields (type + `scheme` + `code`) are now
  asserted on every raise path, so text mutations are unobservable:
  `validate` 6/8/9/10 (`expected` string), 16-22 (exact-length message), 28/32/33
  (min-length message); `permute` 7-12 (unknown-scheme message), 20-33
  (IBAN message).
- **IBAN reroute (2)** -- `permute_14/15` (`"iban"` -> `"XXibanXX"`/`"IBAN"`):
  `"iban"` stops matching the dedicated branch but is still in `_KNOWN_SCHEMES`, so
  it falls through to the final unhandled-scheme raise, which still produces
  `FpeChecksumError(scheme="iban", <same code>)`. Fail-closed and all machine
  fields preserved; only the message differs.
- **Unreachable defensive raise (4)** -- `permute_157/158/159/160`: the trailing
  raise after every scheme branch is dead code (all non-IBAN `_KNOWN_SCHEMES`
  members are handled above and IBAN raises earlier), so no input reaches it.

## Gate

Dennis batch gate: **initially FAILED** (P0) on a real coverage gap the mutation
score is blind to. mutmut only mutates inside functions, NOT the module-level
`_EXACT_LENGTHS` set (line 33 stays `frozenset({8, 12, 13, 14})` in the mutants
copy), so it never generates the "drop a legal GTIN length" mutant -- yet the
suite exercised GTIN only at length 14, leaving GTIN-8/12/13 and the exact-length
guard's firing uncovered. So logic-100% held on mutmut's own mutants, but the
scheme had a genuine blind spot. REMEDIATED: added GTIN-8/12/13/14 validity +
round-trip coverage and an illegal-length (9-char) fail-closed test
(`TestFpeGtin`), pinning every element of the length set and that the guard fires.
These add real coverage without changing the mutmut counts (they kill no
mutmut-generated mutant, because mutmut does not mutate the constant). The
single-length schemes (npi/isbn13/ean13/vin) already had canonical-length tests.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/_fpe_checksum.py`, selection
to `tests/unit/transforms/test_fpe_checksum_validity.py`, then
`rm -rf mutants && python -m mutmut run`.

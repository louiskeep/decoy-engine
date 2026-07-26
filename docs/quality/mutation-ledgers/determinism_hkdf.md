# Equivalent-mutant ledger: `determinism/_hkdf.py`

TQ crown-jewels pass, 2026-07-25. Mutation run: 60 mutants, 53 killed,
**7 survived**, all equivalent (raw score 88.3%, logic-mutant score **100%**).
Covering tests: `tests/unit/determinism/test_hkdf.py` (RFC 5869 reference
vectors) + `tests/property/test_hkdf_invariants.py` (27 tests: determinism,
domain separation, output-length bound, prefix/composition metamorphic
relations, empty-salt rejection).

Baseline (existing unit tests only, before the property layer): raw score not
separately recorded -- the property file was already committed alongside the
oracle-suite pass (`38858ab`) before this grading pass began, so this ledger
reports the combined-suite mutation run only, per the playbook's per-module
loop applied here as a grading (not first-authorship) pass.

Bugs found in `_hkdf.py`: none. The module is correct; every logic mutant is
killed, so the crypto 100% bar is met on logic mutants.

## WORDING (7): error-message prose only, `hkdf_extract`'s empty-salt guard

All 7 survivors mutate `ValueError("hkdf_extract: salt must be non-empty. "
"Pass an application-specific context string. If you genuinely want the RFC "
"5869 §2.2 all-zero default, pass b'\\x00' * 32 explicitly.")` at
`_hkdf.py:53-57` -- mutmut lowercases/uppercases a fragment or wraps it in
`XX...XX`. The literal is consumed only inside the raised `ValueError`'s human
message; it never becomes the exception type, a `code`, a `path`, or any
returned/compared value.
`test_extract_rejects_empty_salt_for_any_ikm` asserts
`pytest.raises(ValueError, match="salt must be non-empty")`, the load-bearing
substring; only pure-wording variants beyond that substring survive.

| Mutant | Mutation |
|---|---|
| `hkdf_extract__mutmut_3` | wraps `"hkdf_extract: salt must be non-empty. Pass an application-"` in `XX...XX` |
| `hkdf_extract__mutmut_4` | lowercases `Pass` to `pass` mid-sentence |
| `hkdf_extract__mutmut_6` | wraps `"specific context string. If you genuinely want the RFC 5869 "` in `XX...XX` |
| `hkdf_extract__mutmut_7` | lowercases `If you genuinely want the RFC 5869` |
| `hkdf_extract__mutmut_8` | uppercases the whole `"specific context string..."` fragment |
| `hkdf_extract__mutmut_9` | wraps `"§2.2 all-zero default, pass b'\\x00' * 32 explicitly."` in `XX...XX` |
| `hkdf_extract__mutmut_10` | uppercases `"§2.2 all-zero default, pass b'\\x00' * 32 explicitly."` |

Note: the sibling `hkdf_expand` length-bound error
(`f"HKDF length {length} exceeds RFC 5869 maximum {255 * _HASH_LEN}"`,
`_hkdf.py:80`) produced two mutants during grading iteration
(`255 * _HASH_LEN` -> `255 / _HASH_LEN` and `-> 256 * _HASH_LEN`). These were
NOT treated as equivalent: the interpolated bound is a load-bearing value (the
actual limit reported to the caller), so `test_expand_rejects_length_over_max`
was hardened to assert the exact computed bound
(`match=f"exceeds RFC 5869 maximum {_MAX_LENGTH}"`), which kills both. Zero
survivors remain on `hkdf_expand`.

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```

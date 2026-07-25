# Equivalent-mutant ledger: `determinism/_derive.py`

TQ crown-jewels pass, 2026-07-25. Mutation run: 98 mutants, 95 killed,
**3 survived**, all equivalent (raw score 96.9%, logic-mutant score **100%**).
Covering tests: `tests/unit/determinism/test_derive.py` (example-based
fixed-vector suite) + `tests/property/test_derive_invariants.py` (23 tests:
determinism, domain separation, injective concatenation, `derive_index`
bounds, `derive_value`/`DeriveContext` composition, validation).

Bugs found in `_derive.py`: none. Every logic mutant is killed, so the
crypto 100% bar is met on logic mutants.

During grading, 9 of the original 12 survivors were killed by hardening
tests rather than left as equivalent:

- `derive__mutmut_3/5/10/12` and `derive_index__mutmut_3/5/11/13` dropped a
  `DeterminismError`'s `message=f"..."` to `message=None` or omitted it. The
  offending value (bad seed length, over/under pool size) is load-bearing
  diagnostic content, not decorative prose, so these are LOGIC mutants, not
  wording. Killed by asserting the offending value's string form appears in
  `excinfo.value.message`.
- `derive_index__mutmut_1` weakened the overflow guard from
  `pool_size > _POOL_SIZE_MAX` to `pool_size >= _POOL_SIZE_MAX`, which would
  wrongly reject the boundary value itself. Killed by a new explicit test,
  `test_derive_index_accepts_pool_size_at_max_boundary`, that pins
  `pool_size == _POOL_SIZE_MAX` and asserts it does NOT raise.

## WORDING (2): `namespace_empty` message case only

`derive__mutmut_15` (wraps `"namespace must be non-empty"` in `XX...XX`) and
`derive__mutmut_16` (uppercases it) touch only the human message of the
`namespace_empty` `DeterminismError`. `test_empty_namespace_raises_
regardless_of_seed_and_source` asserts `excinfo.value.code == "namespace_
empty"` and that `excinfo.value.message` is truthy (present), which both
mutants satisfy; only the exact casing/wrapping differs, and nothing
downstream compares or serializes that string.

| Mutant | Mutation |
|---|---|
| `derive__mutmut_15` | wraps `"namespace must be non-empty"` in `XX...XX` |
| `derive__mutmut_16` | uppercases `"namespace must be non-empty"` |

## DEFAULT (1): codec name case, not a distinct codec

`derive__mutmut_20` changes `namespace.encode("utf-8")` to
`namespace.encode("UTF-8")`. Python's codec lookup is case-insensitive for
standard encoding names (`"utf-8" == "UTF-8"` as codec identifiers per the
`codecs` module's normalization), so `str.encode` produces byte-identical
output either way -- verified directly: `"abc".encode("utf-8") ==
"abc".encode("UTF-8")` is `True`. No input can distinguish the two spellings.

| Mutant | Mutation |
|---|---|
| `derive__mutmut_20` | `namespace.encode("utf-8")` -> `namespace.encode("UTF-8")` |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```

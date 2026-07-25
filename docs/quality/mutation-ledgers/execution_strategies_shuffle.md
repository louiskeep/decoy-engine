# Mutation grading: `execution/_strategies/_shuffle.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-25. `_shuffle` permutes the non-null values of a column
(multiset + null positions preserved). The permutation rng is seeded from
`derive(job_seed, namespace, column_name)` so a deterministic shuffle is
byte-stable across runs and two columns in one namespace draw distinct
permutations; non-deterministic mode uses an unseeded rng.

Graded with the FOCUSED selection `tests/unit/execution/test_shuffle_categorical.py`
(the `TestShuffle` class; ~0.3s). Conservative lower bound.

**54 mutants: 50 killed, 4 survived.** Baseline was 41 killed (76%); this pass
killed the 9 LOGIC survivors, leaving 4 equivalent. LOGIC-mutant score 100%.

## LOGIC killed this pass (4 new tests + 1 strengthened)

| Mutants | Mutation | Killed by |
|---|---|---|
| `StrategyError` `strategy=` field (3: None / `XX...XX` / uppercased) | wrong strategy attribution on the requires-namespace raise | `test_deterministic_requires_namespace` strengthened with `assert exc.value.strategy == "shuffle"` |
| non-deterministic `rng = np.random.default_rng()` -> `rng = None` | the unseeded branch was untested; `None.permutation` crashes | `test_non_deterministic_shuffle_runs_and_preserves_multiset` |
| `derive(...)[:8]` seed slice -> `[:9]`, and the deterministic `rng` seed mutants | a different seed yields a different permutation | `test_deterministic_permutation_is_the_pinned_known_answer` (pins `abcde -> d,e,c,a,b`) |
| output `pd.Series(out, dtype=object, index=df.index)` `dtype=` drop / `dtype=None` | Q13: without the explicit object dtype the int+null assignment re-infers float64 | `test_output_keeps_object_dtype_and_null_not_nan` (direct handler, asserts `dtype == object`) |
| output Series `index=df.index` -> `index=None` / dropped | a RangeIndex misaligns against a non-default-index frame and blanks every row to NaN | `test_non_default_index_preserved_and_aligned` (direct handler, non-default index) |

## EQUIVALENT (4)
| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_8` | `source.to_numpy(dtype=object)` -> `dtype=None` | shuffle only PERMUTES values (never transforms them), and the output is forced to an object-dtype Series; `dtype=None` changes only the boxed scalar type (numpy vs python), which the object Series + the Arrow boundary normalize. No value, order, or dtype difference is observable. |
| `run__mutmut_13` | `message=None` | consumed only as `StrategyError.message`; tests assert `.code`/`.strategy`. |
| `run__mutmut_16` | `message=` kwarg dropped | `StrategyError.message` defaults to `""` (only `code`/`strategy` are required), so the raise still carries the right machine fields. |
| `run__mutmut_34` | `encode("utf-8")` -> `encode("UTF-8")` | Python codec names are case-insensitive; byte-identical, same derived seed. |

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to this module + selection
`tests/unit/execution/test_shuffle_categorical.py`, then `rm -rf mutants && python -m mutmut run`.

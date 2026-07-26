# Mutation grading: `execution/_strategies/_redact.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_redact.py` (43 LOC) replaces every non-null
value with a constant (`redact_with`, default `"REDACTED"`), preserving nulls. It
tries the kernel path (`redact_array` on a pyarrow array) and falls back to a
pandas `.where()` rewrite when the column cannot convert to Arrow
(`pa.ArrowException`), dropping an extension dtype to object first so the string
writes cleanly.

**Grade scope: FOCUSED selection only** (`tests/unit/execution/test_redact_dtype_paths.py`).

## Numbers

**29 mutants: 21 killed (72% baseline), 8 survived -> 26 killed after this pass,
3 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **5 LOGIC survivors killed** by 2 new tests. 0 bugs.
- **3 EQUIVALENT survivors** (all `from_pandas` variants, output-invariant).

## LOGIC (5): killed by new tests

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_5`, `7` | `cfg.get("redact_with", _DEFAULT_REDACT_WITH)` -> default `None` / dropped (redact_with becomes None, which nulls every non-null value instead of writing "REDACTED") | `test_default_redact_with_is_the_default_constant` (no `redact_with` config; asserts output is "REDACTED", not None) |
| `run__mutmut_22` | fallback `is_extension_array_dtype(col.dtype)` -> `is_extension_array_dtype(None)` (always False -> skips the `astype(object)`, so `.where()` runs on the raw extension dtype) | `test_extension_dtype_that_forces_fallback_is_dropped_to_object` |
| `run__mutmut_23` | fallback `col = col.astype(object)` -> `col = None` (`None.where(...)` -> AttributeError) | same |
| `run__mutmut_24` | fallback `col.astype(object)` -> `col.astype(None)` | same |

The fallback-branch kills (22/23/24) required an input that BOTH forces the
`pa.ArrowException` fallback AND is an extension dtype: a mixed-type Categorical
(`pd.Categorical(["a", 5, None])`) does both (a plain mixed-object column forces
the fallback but is not an extension dtype, so it never enters that branch). Real
code redacts it to `["X", "X", <null>]`; each mutant crashes or mis-writes.

## EQUIVALENT (3)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_17` | `pa.array(col, from_pandas=True)` -> `from_pandas=None` | `col` is always a pandas Series, so `from_pandas=None` (auto-detect) resolves to pandas semantics -- identical to `True`. Verified output-identical across object, float64+NaN, Int64, string[pyarrow], and datetime columns. |
| `run__mutmut_19` | `from_pandas=True` -> kwarg dropped (defaults to `None`) | Same as mut_17: the default is auto-detect, which resolves to pandas semantics for a Series. |
| `run__mutmut_20` | `from_pandas=True` -> `from_pandas=False` | Even where `False` would change whether Arrow conversion raises (routing kernel vs fallback), the two paths are observationally equal by design (the parametrized dual-path parity test), so redact output is unchanged. Verified output-identical across the same five dtypes. |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `_redact.py`, selection to
`test_redact_dtype_paths.py`, then `rm -rf mutants && python -m mutmut run`.

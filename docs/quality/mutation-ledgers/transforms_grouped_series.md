# Mutation grading: `transforms/grouped_series.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `transforms/grouped_series.py` (281 LOC)
generates per-group ordered series: `_apply_cumcount` (consecutive count within
each group) and `_apply_monotone_walk` (a per-group seeded non-decreasing walk),
both preserving original row position via a sort/restore, resetting state at each
group boundary.

**Grade scope: FOCUSED selection** (`tests/unit/transforms/test_grouped_series.py`).

## Numbers

**136 mutants: 98 killed (72% baseline), 38 survived -> 110 killed after this
pass, 26 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **12 LOGIC survivors killed** with 8 new tests + a finding-#9 defaults test. 0 bugs.
- **26 EQUIVALENT survivors** (pandas-ignores-`kind=`, dead sentinel-reset inits,
  dtype inference, codec alias). All verified.

## LOGIC (12): killed by new tests

| Mutants | Mutation | Killed by |
|---|---|---|
| `apply_5` | `n == 0` -> `n == 1` (a single row swallowed) | `test_single_row_produces_one_element` |
| `apply_7`, `9` | empty `dtype=int` -> None/dropped (object dtype) | `test_empty_input_has_integer_dtype` |
| `monotone_11` | reset sentinel `object()` -> `None` | `test_null_group_key_still_walks` |
| `monotone_34`, `38`, `39` | encode `errors="replace"` dropped / `"XXreplaceXX"` / `"REPLACE"` (invalid handler on a surrogate label) | `test_surrogate_group_label_does_not_crash` |
| `monotone_35` | `str(g)` -> `str(None)` (all groups share one seed) | `test_distinct_group_labels_seed_independently`, golden vector |
| `monotone_40` | seed slice `[:8]` -> `[:9]` | `test_golden_vector_pins_step_stream` |
| `monotone_51` | `integers(step, max+1)` -> `integers(max+1)` (low collapses to 0) | golden vector + `test_step_equals_max_step_is_exact_arithmetic` |
| `monotone_53`, `54` | `max_step+1` -> `max_step-1` / `+2` (wrong walk range) | golden vector |

The golden-vector test pins the exact step stream `[1, 4, 5, 1, 5, 14]`, verified
by simulating each mutation.

## EQUIVALENT (26)

| Mutants | Category | Why equivalent |
|---|---|---|
| cumcount `3/5/6/7`, monotone `3/5/6/7` | `sort_values(kind=...)` variants | pandas uses `lexsort_indexer` for multi-column sorts and IGNORES `kind` entirely (even a bogus value runs and returns identical order). Verified empirically. |
| cumcount `10`, monotone `10` | preallocated `[0]*n` -> `[1]*n` | the list is fully overwritten (every `0..n-1` position assigned once). |
| cumcount `11`, `12`, `13`; monotone `12`, `13` | boundary-reset sentinel / init-value mutations | the `object()` sentinel forces a reset on the first row before the init value is read, so the init is dead. |
| monotone `33` | drop `encode()` arg | defaults to utf-8. |
| monotone `37` | `"utf-8"` -> `"UTF-8"` | case-insensitive codec alias, byte-identical. |
| cumcount `28/30`, monotone `58/60` | result `dtype=int` dropped | the result is always a non-empty list of Python ints (n>0 guaranteed by the early return), so int64 is inferred anyway. |
| apply `6`, `8` | empty `pd.Series(None/no-arg, dtype=int)` | zero-length int64, byte-identical to `pd.Series([], dtype=int)`. |
| apply `10`, `11`, `12` | internal `pos_col` label renamed | a consistently-used internal position-column label never observed in output. |

(Finding #9: `TestGroupedSeriesDefaults` pins the module-level `_MAX_STEP_DEFAULT`
(max_step==10), default step==1, and per-generator default start (cumcount 0,
monotone 1) -- none reachable by expression mutation.)

## Gate

Dennis batch gate (group_key + grouped_series): **PASS**, 0 P0 / 0 P1 / 0 P2.
All equivalents verified behavior-preserving (golden-vector KAT reproduced exactly
with per-mutant simulation; multi-column `sort_values` confirmed to ignore `kind`;
sentinel-reset inits confirmed dead). Two out-of-scope pre-existing observations
logged in tq-findings (#10 RangeIndex; dead `_MIN_STEP`).

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/grouped_series.py`, selection
to `tests/unit/transforms/test_grouped_series.py`, then `rm -rf mutants && python -m mutmut run`.

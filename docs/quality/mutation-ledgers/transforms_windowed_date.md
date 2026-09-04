# Mutation grading: `transforms/windowed_date.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-25. `windowed_date` generates a per-row date within
`[anchor + min_days, anchor + max_days]`, offset sampled from a seeded per-row rng
(HKDF-SHA256 `derive(seed, namespace, row_index)`), with uniform / early / late
distributions.

Graded with the FOCUSED selection `tests/unit/transforms/test_windowed_date.py`
(30 tests, ~0.4s). Conservative lower bound (the strategy-handler wrapper and
pipeline tests are not counted).

**76 mutants: 75 killed, 1 survived.** Baseline was 65 killed (86%); this pass
killed the 10 LOGIC survivors, leaving 1 equivalent. LOGIC-mutant score 100%.

## Phase 4 slice 1 re-grade (2026-08-31, DGRN -> `windowed_date`)

`apply_windowed_date` gained a `row_offset: int = 0` param
(`docs/plans/2026-08-31-p4-slice1-dgrn-windowed-date.md`): the enumeration line changed from
`enumerate(anchor_series)` to `enumerate(anchor_series, start=row_offset)`, the single
semantic change the slice makes. Re-graded via
`scripts/native-testing/python_mutation_pilot.py` + `scripts/tq_mutate.py` (standalone-
pytest-per-mutant), selection extended to also include
`tests/unit/execution/test_dgrn_windowed_date.py` (the offset-boundary + DGRN-domain-oracle
suite).

**80 mutants (up from 76 with the new param): 77 killed, 3 proven equivalent. LOGIC-100%,
0 unresolved.** The pre-existing `_sample_offset` equivalent (`span = None`, see above) is
still equivalent under the new selection. Two NEW equivalents are Python-version artifacts
of the same call sites the offset threading touches:
`int.from_bytes(derive(...)[:8], "big")` -> dropped second positional arg, and
`i.to_bytes(8, "big")` -> dropped second positional arg. Both are equivalent only because
Python 3.11+ defaults `byteorder` to `"big"` for both `int.from_bytes`/`int.to_bytes` (this
repo's pinned interpreter is 3.13); on an earlier Python the dropped argument would raise
`TypeError` rather than pass silently, so this equivalence is environment-pinned -- re-verify
if the minimum supported Python version ever drops below 3.11.

Regenerate: extend the selection above to
`tests/unit/transforms/test_windowed_date.py tests/unit/execution/test_dgrn_windowed_date.py`.

## LOGIC killed this pass (6 new tests)

All in `test_windowed_date.py::TestWindowedDateBounds`.

| Mutants | Mutation | Killed by |
|---|---|---|
| per-row seed `i.to_bytes(8)` -> `to_bytes(9)`, `[:8]` -> `[:9]`; uniform `a` draw | different seed / draw -> different offsets | `test_deterministic_output_is_the_pinned_known_answer` (the existing determinism test only checked run1==run2, which a self-consistent seed change survives) |
| `distribution == "uniform"` -> `XX...XX` / uppercased | a uniform config would fall through to the late branch | same uniform KAT (sequence changes) |
| `b` draw `rng.integers(min, max+1)` -> `max-1` / `max+2` | wrong upper bound (drops max, or emits max+1 out of window) | `test_late_distribution_output_is_the_pinned_known_answer` (uniform never draws b; late does) |
| `b` draw min_days dropped `rng.integers(max+1)` | draws from `[0, max]` not `[min, max]` | `test_late_distribution_nonzero_min_known_answer` (min=20; the min=0 KAT can't see it since `integers(0, max+1) == integers(min, max+1)` there) |
| `span == 0` -> `span == 1` | pins an adjacent window to min_days | `test_adjacent_window_reaches_both_days` |
| `span = max_days - min_days` -> `max_days + min_days` | collapses a symmetric window to min_days | `test_symmetric_window_is_not_collapsed_to_min` |
| sampling range endpoints unreachable | endpoint dropped while staying in window | `test_both_window_endpoints_are_reachable` |

## EQUIVALENT (1)
| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_sample_offset__mutmut_1` | `span = max_days - min_days` -> `span = None` | `span` is used only in `if span == 0: return min_days`. `None == 0` is False, so the guard is skipped and the code samples `rng.integers(min, max+1)`. The only input reaching that guard is `min == max`, where `integers(m, m+1)` returns `m == min_days` -- the identical result. No input distinguishes them. |

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to this module + selection
`tests/unit/transforms/test_windowed_date.py`, then `rm -rf mutants && python -m mutmut run`.

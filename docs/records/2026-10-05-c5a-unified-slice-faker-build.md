# C5a unified-slice pooled Faker: build record

Status: record

Date: 2026-10-05. Plan: `docs/plans/2026-10-05-c5a-unified-slice-faker.md` revision 4.1. Branch `feat/c5a-unified-faker` off engine main `e24200ed`. Built tests-first. Not yet through dennis or the Codex final gate; not pushed.

## What shipped

A deterministic, reuse-mode `faker` column (allowlisted provider, explicit namespace and pool_size, `string` source, no `when` or `vault`, no FK) runs on the unified slice and returns output identical to the pandas full-frame route. The unified lane also now receives `native_threads` (plan 3g), which fixes every compiled unified operator, not only Faker. Plan section 3h (lane-wide route evidence) stays out; the node evidence dict is unchanged.

| Plan item | Change |
|---|---|
| 3a | `FAKER_OPERATOR_ID` added to `ALLOWED_OPERATOR_IDS`, `_COMPANION_DEPENDENT_OPERATOR_IDS`, `_OPERATOR_REQUIRED_KERNEL` (`index`), and `_ADMITTED_RESIDENT_TYPES["faker"] = {string}`. `large_string` stays declined. |
| 3b | `ShadowCoordinator(ctx=ctx, registry=inputs.registry, ...)`. |
| 3c | `run_pipeline` creates one `PoolCache()` per job after the sequential and out-of-core early returns, passes it to the lane and, through `run_generate_and_mask_steps`, to the full-frame `adapter.run`. The coordinator takes an optional injected `pool_cache`, wraps the pool build and the cache insert in `PoolBuildFailed`, and touches `pool_cache.get(identity)` on a pinned hit. The lane re-raises the original exception for `PoolBuildFailed` and returns None (reroute) for exactly `faker-pool-non-string-output`. |
| 3e | `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS = {hash, faker}`. |
| 3g | `native_threads` threaded through `maybe_run_unified_slice`, the forwarding census, `_execute_admitted`, and `ShadowContext.from_key_provider`. |

Files outside the plan's named set, all mechanical:

- `physical/_shadow_diff_codes.py` holds `PoolBuildFailed`. `_shadow_coordinator.py` has a hard 600-line cap test (`test_shadow_full_frame.py`) and the class pushed it over, so the carrier lives next to `ShadowDifference`.
- `tests/sentry/test_module_size.py` census: `_pipeline.py` 679 to 684, `_unified_slice.py` 614 to 650, `_unified_slice_admission.py` 629 to 637.
- Three bookkeeping tests pin `_pipeline.py` at exactly 679 (`test_auto_chunk_bookkeeping.py`, `test_b6a_bookkeeping.py`, `test_b6b_bookkeeping.py`); the pins moved to 684 with the census.

## Red before, green after

Tests were written and committed first (`e0b331ac`). Against the unmodified lane, 25 tests failed:

- 22 in `tests/physical/test_unified_slice_faker.py`: tests 1 to 5 (including the all-null and zero-row cases), 6 (50,001-row and ragged-chunk cases), 7 (both thread tests), 8 (locale and both pool sizes), 9, 11 (rebound string provider, other-coded-difference), 12, 12b, 13, 13b, 14.
- 2 in `test_unified_slice_admission.py` (the Faker admission-table membership test and `test_faker_string_source_admits`) and 1 in `test_unified_slice_parity.py` (the positive-kernel-evidence case).

Most of these fail because the lane declines Faker, so the poisoned oracle runs. The categorical thread test fails for a different reason: it sees `native_threads` of `None` where 4 was passed, which is the 3g gap on an operator that was already admitted.

Tests that pass before the implementation, because the lane declines outright, and are guards whose strength comes after admission: the coordinator-seam batch sizes (the coordinator already ran Faker), every test 10 decline case, the non-string reroute, the V2 custom-function override, the no-injected-cache case, and the admission cases for `large_string`, unencodable namespace and a missing index kernel. The mutants below show the guards bite.

Green after (companion venv `/home/cam/.cache/decoy-native-venv`):

- New and edited acceptance files: `test_unified_slice_faker.py` 40 passed, 1 skipped (the skipped test only runs where the companion is absent); admission and parity files 76 passed.
- `tests/sentry`, `tests/physical`, `tests/native/test_sample_faker_array.py`, `tests/parity/native/test_c1_faker_parity.py`: 3629 passed, 2 skipped.
- `tests/unit/execution`, `tests/integration`, `tests/native`, `tests/parity`: 12066 passed, 9 failed. Three failures were mine (`test_multi_table_gates.py` compared every `adapter.run` kwarg by equality, and the per-job `pool_cache` instance differs between two jobs; the spy now checks that kwarg is a `PoolCache` and a distinct instance per job). Six are pre-existing and fail identically on `a33d58a6`: four isolated-child-process tests (`test_b6a_split_worker`, two in `test_engine_transforms_routes`, `test_isolated_run::TestMemCapOom`; the child imports the companion venv's editable `decoy_engine`, which points at another worktree) and two in `test_auto_chunk_units.py` (an unrelated `first_schema` kwarg).
- Companion-absent clean venv (`/home/cam/vscode/decoy-engine/.venv`, no companion): faker, admission and parity files 83 passed, 34 skipped, 0 failed.
- Lint: `ruff check`, `ruff format --check` on `src` and the touched test directories clean; `mypy` clean on the six changed source modules.

## Mutation

Each plan mutant was applied alone to the source, the three acceptance files were run, and the change was reverted. All 12 were killed.

| Mutant | First failing test |
|---|---|
| Drop Faker from `ALLOWED_OPERATOR_IDS` | test 1 |
| Drop Faker from `_COMPANION_DEPENDENT_OPERATOR_IDS` | test 10 (kernel unavailable) |
| Add `large_string` to the Faker type matrix | admission table test |
| Omit `registry=` | test 1 |
| Fresh cache instead of the shared one in the lane | test 11 (single build) |
| Broad reroute in place of re-raising `PoolBuildFailed` | test 13 |
| `put` outside the wrapper | test 13 |
| Drop the LRU touch on pinned hits | test 12b |
| Widen the reroute to all coded differences | test 11 (other coded difference) |
| Drop Faker from the D7 set | test 12 |
| Drop the `native_threads` forwarding | test 7 |
| Swap `mask_key` for `job_seed` in selection | test 1 |

## Judgment calls and findings

- Test 10 "missing namespace" cannot reach the lane. `deterministic: true` without a namespace is rejected by the namespace registry (`NamespaceConfigError`) on both routes before routing. The test asserts both routes raise the same error.
- Test 10 "FK-participating table" runs the real two-table config. A relationship job takes the sequential route, so the lane is never consulted; the test pins that the output equals the lane-off run and no activation leaf appears.
- Superseded by the dennis remediation below: the coordinator now visits nodes in the oracle's `order_work` order, so this residual no longer exists. (It was: the oracle visits work nodes in sorted-name order while the coordinator visited plan order, so LRU state matched only when column names sorted in config order.)
- Test 13b needed no wrapper plan amendment. The oracle surfaces a provider `ProviderError` unwrapped (`FakerStrategyHandler.run` calls `builder.build` directly and `run_with_when_gate` does not catch), so the lane's re-raise of the original matches by type, code and message.
- The non-string reroute, rebound-string, other-coded-difference, and capacity tests pass trivially before the implementation, because the lane declines Faker outright. They are regression guards whose strength comes after admission; the mutants below show they bite.
- The coordinator now wraps every pool build and cache insert in `PoolBuildFailed`, including for the dormant shadow coordinator callers. Existing shadow suites stay green; no caller depended on the unwrapped type.

## Platform consumers

A read-only grep of `decoy-platform` finds no caller of the unified lane's internals (`maybe_run_unified_slice`, `run_from_pipeline_locals`, `_execute_admitted`, `ShadowCoordinator`). The platform calls `run_pipeline(..., unified_slice_enabled=..., native_threads=...)` in `api/jobs/v2_runner.py` and the public keywords are unchanged. The platform grant (up to `native_threads_max`, default 4) now reaches the unified lane; output is thread-invariant (test 7).

## Dennis remediation

The dennis gate returned NO-GO. Fixes, in commit order.

| Finding | Fix | Red before |
|---|---|---|
| HIGH 1: the coordinator visited nodes in config order; the oracle runs `order_work` (sorted) serially | `_runner.in_work_order` (built on `work_order_key`, which `WorkNode.key` also uses) sorts a table's nodes; the coordinator loop calls it. Output assembly stays in source-schema order. Applies to every unified operator (timings, warnings and row_errors order now match the oracle too) | 12b (reverse-sorted config), new 12c (two stateful columns, reverse-sorted), new 12d (two failing columns: identical type, code, message) all failed before the fix |
| HIGH 2: `raise failed.original from None` wiped `__cause__` | The handler only captures `pool_failure`; it is raised after the try/except, with no `from`, so `__cause__`, `__context__` and `__suppress_context__` stay as the provider set them | 13b extended with a `raise ... from ValueError("root cause", 7)` chain; asserts type, args of `__cause__` and `__suppress_context__` equal lane-on vs lane-off. Failed before (`__cause__` was None) |
| MEDIUM 1 | (b) done as a pure move: `_ADMITTED_RESIDENT_TYPES` and `_group_key_sibling_admitted` now live in `_unified_slice_resident_types.py` (admission 637 -> 564, under the 600 goal so its census entry is deleted). (a) not done: `_unified_slice.py` no longer contains separate admission-source helpers to move (the alignment lives inside `cheap_admission`), so its census entry was bumped to the actual size (658) instead | n/a |
| MEDIUM 2 | Test 11 asserts the `unified_slice_faker_non_string_pool_reroute` log and the absence of the generic reroute log | Already green (a pin, not a bug) |
| LOW 1 | Stale docstring in `test_shadow_faker_lifecycle.py` updated | n/a |
| LOW 2 | `_assert_same_adapter_call` checks `isinstance(value, PoolCache)` and `value is not` the other call's cache | n/a |
| LOW 3 | `_PoolBuildFailed` renamed `PoolBuildFailed` | n/a |

Existing tests changed:

- `test_lifecycle_counters_under_a_shared_and_distinct_identity_a_b_a_order` pinned plan order (colA, colB, colA2 resolved as shared, b, shared). Under the oracle's order the sorted names would reorder that. The columns are renamed `col1`, `col2`, `col3` and the plan lists them reversed, so the visit order is still A, B, A2 and every assertion is unchanged. No parity assertion was loosened.
- Test 12b: `_four_column_abac` now names columns `a` to `d` and declares them `d, c, b, a`; the assertion is unchanged.
- `_shadow_coordinator.py` is at 599 lines against the 600-line cap tests, so the ordering helper lives in `_runner.py`.
- `tests/unit/execution/test_auto_chunk_units.py`: the two `first_schema` failures seen at the original branch base were fixed on main (PR #202); after the rebase onto `2fb84b6c` the file is green.

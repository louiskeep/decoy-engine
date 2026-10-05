# C5a unified-slice pooled Faker: build record

Status: record

Date: 2026-10-05. Plan: `docs/plans/2026-10-05-c5a-unified-slice-faker.md` revision 4.1. Branch `feat/c5a-unified-faker` off engine main `e24200ed`. Built tests-first. Not yet through dennis or the Codex final gate; not pushed.

## What shipped

A deterministic, reuse-mode `faker` column (allowlisted provider, explicit namespace and pool_size, `string` source, no `when` or `vault`, no FK) runs on the unified slice and returns output identical to the pandas full-frame route. The unified lane also now receives `native_threads` (plan 3g), which fixes every compiled unified operator, not only Faker. Plan section 3h (lane-wide route evidence) stays out; the node evidence dict is unchanged.

| Plan item | Change |
|---|---|
| 3a | `FAKER_OPERATOR_ID` added to `ALLOWED_OPERATOR_IDS`, `_COMPANION_DEPENDENT_OPERATOR_IDS`, `_OPERATOR_REQUIRED_KERNEL` (`index`), and `_ADMITTED_RESIDENT_TYPES["faker"] = {string}`. `large_string` stays declined. |
| 3b | `ShadowCoordinator(ctx=ctx, registry=inputs.registry, ...)`. |
| 3c | `run_pipeline` creates one `PoolCache()` per job after the sequential and out-of-core early returns, passes it to the lane and, through `run_generate_and_mask_steps`, to the full-frame `adapter.run`. The coordinator takes an optional injected `pool_cache`, wraps the pool build and the cache insert in `_PoolBuildFailed`, and touches `pool_cache.get(identity)` on a pinned hit. The lane re-raises the original exception for `_PoolBuildFailed` and returns None (reroute) for exactly `faker-pool-non-string-output`. |
| 3e | `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS = {hash, faker}`. |
| 3g | `native_threads` threaded through `maybe_run_unified_slice`, the forwarding census, `_execute_admitted`, and `ShadowContext.from_key_provider`. |

Files outside the plan's named set, all mechanical:

- `physical/_shadow_diff_codes.py` holds `_PoolBuildFailed`. `_shadow_coordinator.py` has a hard 600-line cap test (`test_shadow_full_frame.py`) and the class pushed it over, so the carrier lives next to `ShadowDifference`.
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
- `tests/unit/execution`, `tests/integration`, `tests/native`, `tests/parity`: 12066 passed, 9 failed. Three failures were mine (`test_multi_table_gates.py` compared every `adapter.run` kwarg by equality, and the per-job `pool_cache` instance differs between two jobs; the spy now compares that kwarg by type). Six are pre-existing and fail identically on `a33d58a6`: four isolated-child-process tests (`test_b6a_split_worker`, two in `test_engine_transforms_routes`, `test_isolated_run::TestMemCapOom`; the child imports the companion venv's editable `decoy_engine`, which points at another worktree) and two in `test_auto_chunk_units.py` (an unrelated `first_schema` kwarg).
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
| Broad reroute in place of re-raising `_PoolBuildFailed` | test 13 |
| `put` outside the wrapper | test 13 |
| Drop the LRU touch on pinned hits | test 12b |
| Widen the reroute to all coded differences | test 11 (other coded difference) |
| Drop Faker from the D7 set | test 12 |
| Drop the `native_threads` forwarding | test 7 |
| Swap `mask_key` for `job_seed` in selection | test 1 |

## Judgment calls and findings

- Test 10 "missing namespace" cannot reach the lane. `deterministic: true` without a namespace is rejected by the namespace registry (`NamespaceConfigError`) on both routes before routing. The test asserts both routes raise the same error.
- Test 10 "FK-participating table" runs the real two-table config. A relationship job takes the sequential route, so the lane is never consulted; the test pins that the output equals the lane-off run and no activation leaf appears.
- The oracle visits work nodes in sorted-name order (`order_work`), while the coordinator visits nodes in plan order. LRU state therefore matches the oracle only when column names sort in config order. Test 12b names the columns `c1` to `c4` for that reason. This is a refinement of the 3c residual: it only matters when the lane hands off to the oracle after a non-string reroute, and only for an impure provider under an aggregate over-budget cache.
- Test 13b needed no wrapper plan amendment. The oracle surfaces a provider `ProviderError` unwrapped (`FakerStrategyHandler.run` calls `builder.build` directly and `run_with_when_gate` does not catch), so the lane's re-raise of the original matches by type, code and message.
- The non-string reroute, rebound-string, other-coded-difference, and capacity tests pass trivially before the implementation, because the lane declines Faker outright. They are regression guards whose strength comes after admission; the mutants below show they bite.
- The coordinator now wraps every pool build and cache insert in `_PoolBuildFailed`, including for the dormant shadow coordinator callers. Existing shadow suites stay green; no caller depended on the unwrapped type.

## Platform consumers

A read-only grep of `decoy-platform` finds no caller of the unified lane's internals (`maybe_run_unified_slice`, `run_from_pipeline_locals`, `_execute_admitted`, `ShadowCoordinator`). The platform calls `run_pipeline(..., unified_slice_enabled=..., native_threads=...)` in `api/jobs/v2_runner.py` and the public keywords are unchanged. The platform grant (up to `native_threads_max`, default 4) now reaches the unified lane; output is thread-invariant (test 7).

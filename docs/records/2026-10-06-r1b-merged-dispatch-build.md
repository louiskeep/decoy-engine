Status: record (R1b build, behavior-preserving refactor; branch `feat/r1b-merged-dispatch`, off engine main `d44465c9`)

# R1b build record: one kernel step per operator, shared by both routes

Plan: `docs/plans/2026-10-06-r1b-merged-dispatch.md` rev 3 (Codex plan gate GO). Risk R2. Not merged; awaiting dennis and the Codex final gate. The plan was written against `a71800f3`; main moved to `d44465c9` with a perf-test-only commit, so no source line the plan cites changed.

## What changed, per plan item

- **3a, parameter objects.** New leaf module `execution/native/_operator_params.py`: nine frozen parameter classes, the `OperatorParams` union, `resolve_operator_params` (every default of plan section 2.1, once) and `resolve_params_by_column`. `_resolve_truncate_keep` moved in; `_dispatch` imports it from there so the test re-export still resolves.
- **3b, `ExecutionBinding`.** The eleven per-operator fields became one `params: OperatorParams | None`. `needs_index_kernel`, `categorical_deterministic` and the new `group_key_sibling` are field-sensitive properties. The coordinator (two reads) and unified admission's resident-type check (`_unified_slice_resident_types.py`) read the sibling through `group_key_sibling`. `execution_binding_for_slice_node` keeps every `return None` guard, then calls the resolver; categorical goes through `prepare_categorical`. `resolved_config` is unchanged (truncate's `keep` now comes from `params.keep`). Each keyed operator's `KeyBinding` namespace is read from `params.namespace`.
- **3c, kernel step.** New leaf module `execution/native/_operator_step.py`: `StepResult`, `run_kernel_step` (one `isinstance` dispatch, the one `derive_calls` reduction, the `or ""` rule), and `sample_faker_array` moved in verbatim. It does not catch `CryptoExtensionUnavailableError`, touch evidence or know a route.
- **3d, adapters.** Unified `run_operator` keeps its name and signature. Its per-operator guards moved to `_bound_params` with today's messages in today's order; it then calls the step inside one `try/except CryptoExtensionUnavailableError` (hash and group_key only, each with its own detail), passes `raw_hex_kernel=None`, sets `compiled_kernel_executed` when `ran`, and builds the batch-local `RowError`s. `_run_date_shift` is gone. Chunked `_mask_chunk_native` takes `params_by_column` (built once per table in `_native_route`) and keeps `unconfigured`/`stored_index`, timing, `counted`, the raw-hex assertion, the group_key sibling from `raw_chunk`, the bucket_perturb `pa.nulls` cast, the faker `pool_select_*` writes, `kernel_idle`, and the `format_errors` channel check. `_mask_bucket_perturb`, `_mask_date_shift` and `_mask_group_key` are removed.
- **3e, patch re-pointing.** Seven positive spies and the negative sentinel now patch `_operator_step`. Listed below.
- **3f, sentry and size.** `GUARDED_MODULES` and the permitted-diff list in `tests/sentry/test_physical_seam_disconnection.py` gain the two new modules. No census entry was added or removed.

## Fowler steps (one commit each, see `git log d44465c9..HEAD`)

1. `4a6704ac` baseline tests (tests 1 to 4 and 9), green before any source change.
2. `13c992a2` red tests (tests 6, 7, 10, 11).
3. `4ff33aa0` Introduce Parameter Object (`OperatorParams`) and Extract Function (`run_kernel_step`), chunked route; Move Function (`sample_faker_array`, `_resolve_truncate_keep`); spy re-pointing.
4. `4a9e4e90` Introduce Parameter Object on `ExecutionBinding` (Replace Type Code with Subclasses), Extract Function (`_bound_params`), unified route through the step; test edits of 3b.
5. `7db25604`, `db40f441` two cases added to my own new tests after the first mutation run (see Mutation); changelog.

The intermediate commits were not each run in full; the full runs below are at the head of step 4 and the later commits only add tests and the changelog.

## Baseline green-before evidence

Commands use `/home/cam/bin/pytest-one`, Python 3.11 = `~/.cache/decoy-native-venv` (has the companion), Python 3.10 = `~/.cache/decoy-ci-mirror-venv` (no companion).

- First run on unmodified source, before the commit `4a6704ac`: `tests/native/test_r1b_cross_route_kwargs.py tests/native/test_r1b_characterization.py tests/physical/test_r1b_unified_group_key.py` on 3.11: 58 passed. On 3.10: 42 passed, 16 skipped (the ran-signal matrix and the group_key end-to-end test need the companion).
- Re-run at the end against a pristine `d44465c9` checkout (scratch worktree, removed afterwards), using the committed baseline support module and the final test files: 3.11: 66 passed; 3.10: 47 passed, 19 skipped. So every baseline case, including the three added after the first mutation run, passes on unmodified main.
- Whole-directory baseline on 3.11 with the baseline tests present and no source change: `tests/sentry` 2330 passed, 1 skipped; `tests/native` 5079 passed, 1 skipped; `tests/physical` 1420 passed, 1 skipped; `tests/unit/execution` 6375 passed, 4 skipped; `tests/parity/native` 104 passed, 59 xfailed.
- Mutation sanity on the baseline test (before the refactor): changing the chunked bucket default to `"week"` failed the Hypothesis test and the `bucket_default` snapshot case, then reverted.

Test 1 and 2 use recording stubs in place of the kernels, so they need no companion and run on both interpreters. The stub for each kernel is installed in every candidate call-site module (`_shadow_operators`, `_chunk_masking`, `_operator_step`) with `mock.patch.object`, only where the attribute exists, and one shared recorder counts each call once. Because stubs replace the kernels, plan constraint (b), capturing the original kernels, does not apply. Tests 4 and 9 and the real-kernel part of test 7 need the companion and skip on 3.10.

## Red-before evidence (against unmodified source, commit `13c992a2`)

- Test 6 (`test_r1b_resolver_single_source.py`): 3 failed. `_shadow_operators.py` still holds the `"REDACTED"` literal (line 166); `_shadow_bindings.py` and `_chunk_masking.py` each hold ten offences (the `_resolve_truncate_keep` import and call, `DEFAULT_MIN_DAYS`/`DEFAULT_MAX_DAYS`, `"month"`, `16`, the `group_key/` f-string).
- Test 11 (`GUARDED_MODULES` entries): `test_guarded_modules_exist` and `test_no_production_module_imports_the_physical_seam` failed (`FileNotFoundError`, the two modules do not exist).
- Tests 7 and 10: collection errors, `ModuleNotFoundError: decoy_engine.execution.native._operator_params`.
- Recorded in the scratchpad `red_before.txt` of the build session; the commands are the ones in the first bullet of "Baseline" with the file names above.

## Edited existing test cases

All edits follow plan 3b and 3e. No assertion, expected value, expected message or call count was changed. Line numbers are in the edited files.

Construction sites (3b i):

| File:line | Old | New | Unchanged expectation |
|---|---|---|---|
| `physical/test_shadow_coordinator.py:51-66` (`_binding`) | per-operator fields absent | optional `params` kwarg forwarded to `ExecutionBinding` | n/a, helper |
| `physical/test_shadow_coordinator.py:71-85` (`_plan_with_one_node`) | `resolved_config` only | optional `params` forwarded | n/a, helper |
| `physical/test_shadow_coordinator.py:114,193,321,443,467,497` | `_plan_with_one_node("passthrough", "native_passthrough")` | adds `params=PassthroughParams()` | duplicate-node code; planned-vs-actual diff; batches_run; one timing record; no clock calls |
| `physical/test_shadow_coordinator.py:159` | hash plan, `key_binding` swapped in by `replace` | adds `params=HashParams("n", None)` | evidence executed and compiled, 0 output rows |
| `physical/test_shadow_coordinator.py:284` | `_binding("native_truncate", resolved_config=(("length", 3),))` | adds `params=resolve_operator_params("truncate", ..., {"length": 3})` | output `["abc"]`, no row errors |
| `physical/test_shadow_coordinator.py:307` | `_binding("native_truncate", resolved_config=())` | adds `params=resolve_operator_params("truncate", ..., {})` | `StrategyError` matching `truncate_length_invalid` |
| `physical/test_shadow_coordinator.py:421` | faker `ExecutionBinding(...)` | adds `params=FakerParams("ns_int_override")` | declines with the faker non-string code |
| `physical/test_shadow_diff_catalog.py:83` (`_hash_binding`) | no params | `params=HashParams(namespace, None)` | companion-unavailable code, no evidence; native_threads forwarded |
| `physical/test_shadow_faker_lifecycle.py:68` (`_faker_binding`) | no params | `params=FakerParams(namespace)` | lifecycle counters; no module-global cache |
| `physical/test_unified_route_evidence.py:485` | `group_key_group_by="gb", group_key_length=16, group_key_prefix=""` | `params=GroupKeyParams("gb", 16, "", "group_key/gk")` | monotonic compiled flag across calls |
| `physical/test_shadow_group_key.py:756` (`_binding`) | `group_key_group_by=_GB, group_key_length=16, group_key_prefix=""` | `params=GroupKeyParams(_GB, 16, "", f"group_key/{_TARGET}")` | guard message `no KeyBinding/group_by/length`; empty vs populated compiled flag; loader-raises decline |
| `physical/test_shadow_bucket_perturb.py:497` (`_binding`) | `bucket_perturb_bucket="month", bucket_perturb_date_format="%Y-%m-%d"` | `params=_FULL_PARAMS` (`BucketPerturbParams("month", "%Y-%m-%d", "ns")`) | guard messages |
| `physical/test_shadow_date_shift.py:983` (`_binding`) | three `date_shift_*` fields | `params=_FULL_PARAMS` (`DateShiftParams(_FMT, -365, 365, "ns")`) | row errors, guard messages |

Malformed-binding overrides, mapped literally (3b iii):

| File:line | Old override | New override | Unchanged expectation |
|---|---|---|---|
| `physical/test_shadow_bucket_perturb.py:518` | `_binding(bucket_perturb_date_format=None)` | `_binding(params=dataclasses.replace(_FULL_PARAMS, date_format=None))` | `AssertionError` matching `no resolved bucket/date_format` |
| `physical/test_shadow_date_shift.py:1005` | `_binding(date_shift_date_format=None).needs_index_kernel is False` | `_binding(params=dataclasses.replace(_FULL_PARAMS, date_format=None)).needs_index_kernel is False` | `False` |
| `physical/test_shadow_date_shift.py:1127` (table, `date_format`) | `{"date_shift_date_format": None}` | `{"params": replace(_FULL_PARAMS, date_format=None)}` | exact message `date_shift node reached run_operator with no resolved date_format/min_days/max_days` |
| `physical/test_shadow_date_shift.py:1132` (table, `min_days`) | `{"date_shift_min_days": None}` | `{"params": replace(_FULL_PARAMS, min_days=None)}` | same message |
| `physical/test_shadow_date_shift.py:1137` (table, `max_days`) | `{"date_shift_max_days": None}` | `{"params": replace(_FULL_PARAMS, max_days=None)}` | same message |

The `key_binding`, `index_kernel` and `column` rows of that table, `test_run_operator_requires_target_column` (the unusable `object()` kernel, guard fires before the call) and `_with_binding` (`dataclasses.replace(n.execution, **changes)`, now `:748`) needed no edit: its callers only replace `diagnostic_obligations` and `required_prepasses`, none of the removed fields.

Determinism assertion (3b iv): `physical/test_shadow_categorical.py:278`, `categorical_deterministic=False, categorical_categories=("a","b"), categorical_cdf=None` became `params=CategoricalParams(PreparedCategorical(("a","b"), None, positional=True), "ns")`. Expectation unchanged: `AssertionError` matching `categorical_deterministic=False`.

Patch re-pointing (3e): module object in `monkeypatch.setattr` and the matching `real = ...` line only, plus the import line.

| File:line | Kernel | Call-count assertion kept |
|---|---|---|
| `native/test_chunked_categorical_admission.py:470,476` | `native_categorical` | `seen == [threads] * len(chunks)` |
| `native/test_chunked_group_key_admission.py:569` | `native_group_key` | `calls == [0]` |
| `native/test_chunked_group_key_admission.py:610` | `native_group_key` | `seen[0] > 0 and seen[1] == 0 and seen[2] > 0` |
| `native/test_chunked_group_key_parity.py:287` | `native_group_key` | `len(calls) == 2` |
| `native/test_chunked_bucket_perturb_admission.py:478,484` | `native_bucket_perturb` | `len(seen) == len(chunks)` |
| `native/test_chunked_date_shift_admission.py:442,448` | `native_date_shift` | `len(seen) == len(chunks)` |
| `native/test_chunked_nondet_categorical_parity.py:335,341` | `native_categorical_positional` | `seen == [(threads, 10), (threads, 12), (threads, 14)]` |
| `native/test_dispatch_faker.py:1391` (negative) | `native_keyed_hash` | none added; it still never runs |
| `native/test_sample_faker_array.py:18` | import line only | n/a |

Every one of the eight spy tests runs the chunked entry (`run_one`/`run_mask_chunked`) only. The exact call counts above held after the move, so none of them also saw a unified call; the sentinel still passes.

## Mutation check (test 12)

One mutant at a time, applied to the committed source, run against the new test files (`tests/native/test_r1b_*.py`, `tests/physical/test_r1b_*.py`), then reverted. First failing test shown.

| # | Mutant | Result, first failing test |
|---|---|---|
| M1 | bucket default `"month"` to `"week"` | killed, `test_each_route_passes_the_literal_kernel_arguments[bucket_default]` |
| M2 | group_key length default 16 to 18 | killed, `[group_key_default]` |
| M3 | redact default to `"REDACTD"` | killed, `[redact_default]` |
| M4 | date_shift min default `DEFAULT_MIN_DAYS + 1` | killed, `[date_shift_default_bounds]` |
| M5 | date_shift max default `DEFAULT_MAX_DAYS - 1` | killed, `[date_shift_default_bounds]` |
| M6 | truncate length coercion `else 0` to `else 1` | first run survived; killed after adding `test_a_length_that_is_not_an_int_reaches_the_truncate_kernel_as_zero` (admission rejects such configs, so no admitted example reaches the coercion) |
| M7 | truncate `from_end` default flipped | killed, `[truncate_defaults]` (case added after the first run noticed no snapshot omitted both `keep` and `from_end`) |
| M8 | group_key prefix `str()` dropped | killed, `[group_key_none_prefix]` |
| M9 | group_key namespace suffix | killed, `[group_key_default]` |
| M10 | step bucket_perturb `or ""` dropped | killed, `test_a_missing_namespace_reaches_the_kernel_as_the_documented_value[bucket_perturb]` |
| M11 | step date_shift `or ""` dropped | killed, same test `[date_shift]` |
| M12 | step categorical `or ""` dropped | killed, same test `[categorical]` |
| M13 | step hash gains `or ""` (None must stay None) | killed, same test `[hash]` |
| M14 | step group_key `ran` threshold `> 1` | first run survived (no one-row sibling); killed after adding the `one_row` source, `test_ran_signal_matrix[group_key-one_row]` |
| M15 | step positional zero-row `ran` True | killed, `test_a_positional_categorical_on_zero_rows_makes_no_kernel_call` |
| M16 | step drops date_shift positions | killed, `test_date_shift_reports_the_batch_local_positions_of_unparseable_values` |
| M17 | step hash `ran` None | killed, `test_ran_is_none_...[hash]` |

17 of 17 killed after two test additions. Neither addition weakens anything; both are new cases in my own tests, and both also pass on pristine main (see Baseline). Mutation of the resolver and step was by hand with a script; no coverage or mutmut measurement was run.

## Module LOC before and after (`wc -l`)

| Module | Before | After |
|---|---|---|
| `native/_chunk_masking.py` | 559 | 244 |
| `native/_chunked_entry.py` | 596 | 600 |
| `native/_dispatch.py` | 574 | 576 |
| `physical/_shadow_operators.py` | 362 | 334 |
| `physical/_shadow_bindings.py` | 360 | 306 |
| `physical/_plan.py` | 248 | 233 |
| `physical/_shadow_coordinator.py` | 599 | 599 |
| `_unified_slice_resident_types.py` | 82 | 82 |
| `native/_operator_params.py` (new) | 0 | 218 |
| `native/_operator_step.py` (new) | 0 | 287 |

The two adapters lose about 410 lines; the two new modules add 505, so production code grows by about 90 lines in total. The gain is that each default and each kernel call exists once. `_chunked_entry.py` sits at exactly 600 (the goal), so no census entry; none of the changed modules had one to remove.

## Final test counts

Python 3.11 (`decoy-native-venv`, companion present), at the head of the branch except the last row:

| Directory | Result | Baseline (3.11, before the change) |
|---|---|---|
| `tests/sentry` | 2339 passed, 1 skipped (2338 before this record was added; a docs sentry counts it) | 2330 passed, 1 skipped |
| `tests/native` | 5120 passed, 1 skipped | 5079 passed, 1 skipped |
| `tests/physical` | 1443 passed, 1 skipped | 1420 passed, 1 skipped |
| `tests/unit/execution` | 6375 passed, 4 skipped | 6375 passed, 4 skipped |
| `tests/parity/native` | 104 passed, 59 xfailed | 104 passed, 59 xfailed |

`tests/unit/execution` and `tests/parity/native` were run at `4a9e4e90`; no file under `src/` changed after that commit (`git diff 4a9e4e90 HEAD -- src` is empty), only tests and the changelog. The sentry, physical and native rows were re-run at the head.

Python 3.10 (`decoy-ci-mirror-venv`, no companion, so companion tests skip): `tests/sentry` 2338 passed, 1 skipped. The new test files plus every edited test file: 502 passed, 839 skipped (the skips are the companion-gated tests; no failures). `tests/native`, `tests/physical`, `tests/unit/execution` and `tests/parity/native` were not run in full on 3.10, because the brief asked for sentry on both interpreters and every new test on both.

Lint at the head: `ruff check src tests`, `ruff format --check src tests`, `mypy src` (the 3.10 mirror tools): clean.

## Judgment calls

1. `_operator_params` imports the operator registry to skip strategies that are not native operators (`seed.strategy not in OPERATORS`). The plan called the module registry-free; a second hand-written list of the nine strategies would break "one place per fact". The registry is itself a leaf.
2. `resolve_params_by_column` lives in `_operator_params` and is called from `_native_route` (where `col_seed_by_name` already exists) rather than from `_run_chunked`. That keeps `_native_route`'s signature and puts `_chunked_entry.py` at exactly 600. The prepared categoricals are the existing `_prepared_categoricals` result, untouched.
3. The step's index-kernel guards carry one generic message built from the parameter class name. The chunked adapter's old per-operator index-kernel messages (all `# pragma: no cover`, unreachable after preflight) are therefore gone, and a missing params entry raises one generic `AssertionError` instead of the old per-branch ones. The unified adapter (`_bound_params`) keeps every REACHABLE guard message and order; the date_shift malformed-binding table and the missing-column test pin them. Two unreachable `# pragma: no cover` categorical guards changed: the "no resolved categories" message is gone (a `CategoricalParams` always carries its prepared categories), and the index_kernel check now runs before the params check.
4. The two truncate tests that probe `run_operator`'s own defaults (`test_run_operator_truncate_defaults_keep_to_head_when_config_omits_it`, `..._falls_back_to_length_zero_which_fails_closed`) build their params through `resolve_operator_params` from the same omitted config, not from a hand-written `TruncateParams`. `run_operator` no longer holds those defaults, so a hand-written object would make the tests vacuous. Both keep their exact expectations.
5. Plan section 2.3 missed two source-grep tests that read `_shadow_operators`: `native/test_chunked_nondet_categorical_admission.py:377` and `unit/execution/test_c1b_i_route_regression.py:369-370` pin the literal `if not binding.categorical_deterministic:` and the message `categorical node reached run_operator with categorical_deterministic=False`. I kept that literal in `_bound_params` (using the property the plan defines) so both pass unedited. Not a divergence, but a plan gap worth recording.
6. The cross-route tests use recording stubs rather than wrapped real kernels, so tests 1 to 3 run on both interpreters. The real-kernel tests (4, 9, part of 7) skip on 3.10.
7. Binding guards that validated resolved values (`group_key` length is an int, `date_shift` bounds are ints) now run after the resolver on the resolved fields, so the defaults are not re-stated in `_shadow_bindings.py`. Guards that need raw config (`date_format` present and a string, `group_by` present) run before it, because the resolver indexes those keys.
8. Faker params carry `namespace: str | None` per the plan; the step raises a `# pragma: no cover` `AssertionError` when it is `None` instead of casting.

## Follow-ups (not done)

- Durable docs: the decoy-platform roadmap and shipped log (outside this checkout) need the R1b item moved to shipped once the branch merges.
- `physical/_shadow_operators.py` is still 334 lines because `_bound_params` keeps nine fail-closed guard chains with their exact messages. They are wiring-bug guards that this refactor was told to preserve; thinning them is a separate decision.
- Plan 2.3 should list the two source-grep tests of judgment call 5.

## dennis gate (round 1: GO, 0 BLOCKER / 0 HIGH / 1 MEDIUM / 3 LOW), fixed by the plan author

- MEDIUM, plan §5 test 8(b) was missing (the cross-route tests strip `raw_hex_kernel` before comparing). Added `tests/physical/test_shadow_group_key.py::test_full_frame_operator_lets_the_kernel_load_its_own_raw_hex_companion`, which spies `native_group_key` at the step and asserts the unified route passes `raw_hex_kernel=None`. Mutation check: passing a preloaded compiled kernel from `_shadow_operators` fails it.
- LOW, dead `_UNKEYED_PARAMS` in `_shadow_operators.py`: `_bound_params` now uses it instead of an inline copy.
- LOW, the guard-order claim above was overstated; corrected.
- LOW, companion-unavailable detail strings were unpinned (pre-existing gap): `test_shadow_diff_catalog.py` and the group_key decline test now assert the hash and raw-hex detail text.

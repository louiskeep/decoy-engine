# bucket_perturb date_format guard: build record

Status: record

Date: 2026-10-07. Plan: `docs/plans/2026-10-07-bucket-perturb-format-guard.md` rev 3 (Codex plan-gate GO, round 3). Branch `fix/bucket-perturb-format-guard` off engine main `7ee20b55`. Not pushed, not merged.

## What shipped

A `bucket_perturb` `date_format` that cannot write a date back is now rejected instead of overwriting every parsed date with the format text.

| Plan item | Change |
|---|---|
| 2a, the one rule | `transforms/bucket_perturb.py`: `bucket_perturb_date_format_problem`. Whole-string scan, `%%` consumed, recognized-directive table (DATE `YyGmbBdjUWVaAuwxc`, TIME/other `HIMSpfzZ`), at least one DATE directive required. `None` and `""` mean autodetect. `validate_bucket_perturb_config` applies it to the raw value, so a falsy non-string such as `0` cannot slide into autodetect. |
| 2b, compile | `plan/_checks_bucket_perturb.py` (new) registered next to `check_top_code_config` in `compile_plan` and `run_config_only_checks`, plus the `bucket_perturb_config` entry in all three reporting tuples. Code `bucket_perturb_date_format_unsupported`. |
| 2c-0, preflight | `BucketPerturbStrategyHandler.preflight`, sharing `_validated_config` with `run`, so a zero-match `when:` gate still rejects. |
| 2c-1, nested | The compile check reads `strategy: nested` children (path `...provider_config.strategy_config.date_format`). `NestedStrategyHandler.run` calls the child's `preflight` right after resolving it, before any empty, all-null or zero-match early return. |
| 2c, backstop | Oracle handler (through the validator), out-of-core kernel `_bucket_perturb_array` and `group_c_output_type`. The out-of-core kernel now validates before its autodetect check so a falsy non-string gets the format error. |
| 2d, native | `bucket_perturb_config_rejection` applies the rule after its own declines (non-string or empty, timezone). Reason `bucket_perturb_date_format_unsupported:<col>`. |

Error text names the column and asks for a concrete pattern such as `%Y-%m-%d`. It never carries a cell value.

## Inventory (taken before implementing)

Searched `tests/`, `scripts/`, `testflight/`, `docs/` for bucket_perturb configs and every `date_format` value.

- Every existing bucket_perturb config in tests, scripts and the testflight manifests uses a date-bearing format (`%Y-%m-%d`, `%d/%m/%Y`, `%m/%d/%Y`, `%Y%m%d`, `%m/%d/%y`, `%Y-%m-%dT%H:%M:%S`, `%d-%b-%Y`, `%j-%Y`) or none (autodetect: the healthcare and HR testflight columns, shadow tests). None changes.
- No existing test pinned the destroyed literal output (`'mixed'`, `'ISO8601'`) on the base tree.
- Tests that expected `%Q` to raise a bare `ValueError` mid-run (2): changed, see below.
- Pinned `checks_passed` tuples (3): changed, see below.
- Module-size census for `plan/_compile.py` (1).
- C8-iii-a goldens (`tests/native/_c8_iii_a_main_goldens.json`): not on the base `7ee20b55`, but present on local main `4a08f570` (PR #223 merged after the plan was written). See "Drift against main".

## Test changes (reason and migration note)

| Test | Change | Reason | Migration note |
|---|---|---|---|
| `tests/unit/execution/test_bucket_perturb_chunked.py::..._raises_equivalently_on_both_routes` | expects `PlanCompileError(bucket_perturb_date_format_unsupported)` on both routes instead of `ValueError("bad directive")` | `%Q` is an unknown directive; the rule moves the failure from mid-run to up front | still asserts both routes raise the same coded error; write a real pattern such as `%Y-%m-%d` to run |
| `tests/native/test_chunked_bucket_perturb_admission.py::test_an_invalid_but_truthy_format_raises_the_same_error_on_both_legs` | both legs expect the same compile error; the native-admitted route-evidence assertions are replaced by "no route evidence recorded" | compile now rejects before either leg routes | as above |
| `tests/unit/plan/test_compile_s2_refactor.py` | tuple, count 31 to 32, negative indexes before the new entry shift by one | the new check is registered right after `top_code_config` | position is pinned, not loosened |
| `tests/unit/plan/test_compile_basic.py` | set gains `bucket_perturb_config` | same | none |
| `tests/unit/plan/test_checks_non_poolable.py` | `run_config_only_checks` tuple gains `bucket_perturb_config` | same | none |
| `tests/sentry/test_physical_seam_disconnection.py` | three files added to the permitted non-physical edits (`_strategies/_bucket_perturb.py`, `_strategies/_nested.py`, `out_of_core/_mask_group_c.py`) with a reason | the seam sentry lists every execution file changed against main | none |
| `tests/sentry/test_module_size.py` | `_compile.py` census 703 to 682 | comment-only trims in `_compile.py` paid for the registration; the file is now below the 700 max and is an ordinary dense entry at its exact LOC | none |

No planned or existing assertion was weakened. No existing test other than those in the table above changed.

## Tests first

The new tests (169 cases collected from three files, plus a fourth file for the rule itself) were written and run against an exported base tree (`git archive 7ee20b55` plus the new test files): 150 failed, 19 passed, and the rule file failed at collection (`ImportError`, the function does not exist). The 19 passes are the negative controls (valid formats, other strategies, autodetect). Failure reasons were `DID NOT RAISE`, the new check name missing from `checks_passed`, and the native gate returning `None`; none was a fixture error.

After the fix: 282 pass across those four files and the three tuple-pin files.

## Verification

- 3.10 `ruff check`, `ruff format --check`, `mypy src`: clean.
- 3.11 `tests/unit tests/native tests/physical tests/parity tests/integration tests/perf`: 22427 passed, 159 skipped, 59 xfailed, 10 deselected, 1 failed. The one failure (`tests/unit/test_v2_cloud_sources.py::...test_profile_gcs_source_via_mocked_client`, `No module named 'google'`) fails identically on the base tree; it is the venv, not this change.
- Hand mutation on the rule (10 mutants: `%%` not consumed, date requirement removed, dangling accepted, unknown accepted, time directive counted as date, a date directive dropped, a time directive dropped, `""` rejected, `%%` treated as unknown, non-string accepted): all 10 killed by the rule and compile tests.
- `scripts/test_flight.py` (check only): 5/5 job fingerprints match golden, 53 of 53 invariant checks pass.
- Sentries (`tests/sentry`) on 3.10 and 3.11 after the final commit: all pass except the physical-seam permit list, fixed as above.

## Merge of main and post-rebase migration

Main moved to `4a08f570` (C8-iii-a, PR #223) after this branch was cut, so main was merged in (a merge commit, nothing force-pushed). Two conflicts: `CHANGELOG.md` (kept both entries) and `execution/native/_operator_config_rejections.py` (kept both checks, C8's `bucket_perturb_special_date_format` first, then the shared rule; the special-format check is now redundant but harmless).

C8-iii-a pinned `mixed` and `ISO8601` as accepted formats whose output is the format name written over every date. That output is the defect this change removes, so those cases now assert the compile rejection `PlanCompileError(bucket_perturb_date_format_unsupported)`, raised before any output. No other C8-iii-a test changed. All eight entries of `tests/native/_c8_iii_a_main_goldens.json` were special-format cases, so the file is now `{}`; no other golden existed. Evidence: `git show main:tests/native/_c8_iii_a_main_goldens.json` has the keys `ISO8601/{all_null,empty,ordinary_dates,uniform_offset}` and `mixed/{...same four}`, and nothing else.

| Test (file) | Old assertion | New assertion | Reason |
|---|---|---|---|
| `test_4_special_formats_unmasked_decline_with_the_new_code` (chunked), renamed `test_4_special_formats_are_rejected_at_compile_before_any_output` | native leg declines to the oracle with `bucket_perturb_special_date_format`; both legs run and match | both legs (native and forced oracle) raise the compile error, no chunk output, no sink writes | the format cannot write a date back |
| `test_4_special_format_jobs_that_succeeded_natively_keep_their_output` (chunked), renamed `..._that_ran_natively_are_now_rejected` | output equals the main golden (format name in every date) | the job raises the compile error before any output | the golden is the destroyed output |
| `test_4_special_formats_unmasked_decline_and_equal_the_oracle` (unified), renamed `..._unmasked_are_rejected_at_compile` | lane declines and equals lane-off | both lanes raise the compile error | same |
| `test_4_special_format_jobs_that_succeeded_natively_keep_their_output` (unified), renamed `..._that_ran_natively_are_now_rejected` | output, schema and pandas metadata equal the golden | both lanes raise the compile error | same |
| `test_4_special_formats_masked_decline_and_equal_lane_off` (unified), renamed `..._masked_are_rejected_at_compile_under_when` | lane declines under three predicates and equals lane-off | both lanes raise the compile error under each predicate, including ones selecting no row | same, and `when:` must not hide it |
| `test_4_the_config_gate_names_special_formats` (unified) | unchanged | unchanged (C8's code still wins) | none |
| `test_the_native_reason_is_the_shared_rule_code` (this branch) | `mixed`/`ISO8601` expected the shared-rule code | split: `mixed`/`ISO8601` expect C8's special-format code; other formats keep the shared-rule code | the earlier check wins |

`test_4_special_formats_masked_on_the_explicit_chunked_route_raise_the_gate` is untouched: on `run_mask_chunked` the chunked `when:` gate still raises first.

## Not covered

- The undetectable-format passthrough (plan 2e) is unchanged and tracked on the roadmap.
- Platform save-time validation is excluded by the plan.

## Final verification on the merged head (56cd82c1)

- **Sentries** (`tests/sentry`, 3.11): 2455 passed, 1 skipped.
- **Full suite** (`tests`, 3.11, `-rfE`): 25594 passed, 1 failed, 172 skipped, 59 xfailed. The failure is `test_v2_cloud_sources.py::...test_profile_gcs_source_via_mocked_client` (`No module named 'google'`). It fails identically on main 4a08f570 in the same venv, an environment gap.
- **Testflight** (check mode): 53/53 invariants, 5/5 fingerprints match the golden.
- **dennis gate:** GO (1 MEDIUM, this record section; 2 LOW, CHANGELOG spacing and the platform roadmap flip at merge).

## Codex final gate round 1 remediation (Opus)

- **`%X`** is added to `_OTHER_DIRECTIVES`. Tests: `%Y-%m-%d %X` is accepted; a bare `%X` is rejected.
- **Out-of-core preflight.** `_mask_group_c.preflight_group_c(work)` runs in `run_fk_out_of_core` after the compatibility check and before the temp root or any table. A new test, `test_a_later_table_rejects_before_the_first_table_is_written`, gives a valid parent and a `mixed` child and asserts zero sink writes. `_runner.py` stays at 707 LOC: the import became a module import and one comment was compressed.
- **docs/strategies.md:** the guard paragraph is under bucket_perturb; date_shift's line is restored to main's text.

# SUB-STOP build record

Status: record

## Environment

Base `41a86f47eed491030c5d235d1046e426d387f22d` (origin/main, unchanged at push time). Tests on GCP VM
`decoy-test-2` and `decoy-test-5` through `vm-run engine` (venv from `uv.lock`, Python 3.10). Mutations M4 to M10
ran on the VM; M1 to M3 ran on the devbox through `pytest-one` after the VM gates run was cancelled (see Not verified).

## Entry table

Matches the handoff table. E1, E2, E4, E5 and E6 have a call. E3 (`run_job_with_governor`) is covered by E2.
No extra public entry point was found. In `native/_chunked_entry.py` the import is local to `run_mask_chunked`
because a top-level import shifts the line number the secret-material census pins (`_native_route key_box`,
line 259), and `tests/sentry/secret_material_census.json` is outside the allowed files.

## Red runs on the base tree (VM, base files plus the new tests)

`pytest tests/unit/execution/test_subset_run_refused.py tests/unit/test_reason_registry_subset_run.py`: 9 failed, 2 passed.
Red by assertion: run_pipeline (patched context builder reached), refusal text, isolated True and False, governor,
chunked, native chunked, compile_plan (DID NOT RAISE), registry (`find` is None). Green on base by design:
`test_no_subset_runs_unchanged`, `test_subset_step_still_runs`.

## Green runs

Head, VM: named tests 11 passed after fixing two test bugs found in the first green run (control estimate is
unpriceable for a variable-width column, so the control asserts it is not the fallback; `ReasonRecord` field is
`retry_class`). Devbox re-run of the named tests plus the census and module-size sentries: 859 passed.
Behavior-kept list on the VM: 1311 passed. Every test file reaching the changed names (git grep list, 199
files minus perf): 1 failed, 8680 passed (see Gates).

## Mutations

| Id | Mutation | Red test |
|---|---|---|
| M1 | delete E1 call | `test_run_pipeline_refuses_subset_before_any_read` |
| M2 | delete E2 call | `test_isolated_refuses_before_spawn[True]`, `[False]`, `test_governor_refuses_before_any_rung` |
| M3 | delete E4 call (the move below preflight was not run) | `test_chunked_refuses_at_call_without_pulling` |
| M4 | delete E5 call | `test_native_chunked_refuses_at_call_without_pulling` |
| M5 | delete E6 call | `test_compile_plan_refuses_subset` |
| M6 | seed table in message | `test_refusal_text_is_fixed` |
| M7 | guard on `"subset" in config` | `test_no_subset_runs_unchanged` |
| M8 | `since="1.5.0"` without bump | `test_subset_run_reason_is_registered` (fixture entry mismatch); `test_reason_registry.py` and `test_reason_release_pin.py` stayed green, so the append-only ratchet does not cover an unbumped addition at an unreleased version |
| M9 | register under `SubsetConfigError` | `test_subset_run_reason_is_registered` (owner assertion) |
| M10 | guard inside `subset_inputs_from_config` | `test_subset_step_still_runs` |

## Registry

1.5.0 to 1.6.0. New identity `config.subset_run_unsupported`, owner `PipelineValidationError`, `since` 1.6.0.
`consumer-contract.json` diff is only `registry_version` strings (27 lines). `manifest_violations` was not used in
AT-S8: against a fixture holding only the new identity at the live version it reports "added without a bump", so
the test compares the pinned entry to the live entry exactly instead.

## Module-size bump

`_isolated_run.py` 651 to 654, `plan/_compile.py` 677 to 680, `native/_chunked_entry.py` 665 to 669.

## Gates

VM, T1 impact tests: 1 failed, 8680 passed. The failure is
`tests/unit/execution/test_auto_chunk_bookkeeping.py::test_pipeline_stays_at_its_exact_ratchet_and_the_new_module_under_the_goal`:
it pins `execution/_pipeline.py` at exactly 351 lines and the guard makes it 356. The orchestrator approved bumping that pin from 351 to 356 (intended growth from the handoff-required guard import,
call and docstring sentence); `test_auto_chunk_bookkeeping.py:31` now pins 356.
First `gates.sh --baseline` run on the VM: ruff, format, mypy pass; sentry and tests failed only on the census line
shift (since fixed by the local import) and two tests since fixed. The re-run was cancelled before it finished.
Sphinx was not run (no docs extras on the VM venv at that point).

## Files vs allowed list

All files are on the list except these, each approved by the orchestrator or required by review: `tests/unit/execution/test_auto_chunk_bookkeeping.py` (pin 351 to 356), `execution/native/_dispatch.py`, `plan/validate/_consolidator.py`, `tests/unit/plan/test_validate.py`, `tests/unit/execution/test_subset_run_guard_entries.py`. `tests/sentry/test_module_size.py` changes four values only (the `_dispatch.py` one is from review).

## Named limit

As in `config/_subset_run.py`: the guard reads the validated dump's `subset` key.

## V2-LOG

`decoy demo` (`C:cli/demo.py`) is covered only by E6. `native/_dispatch.py:595` `run_native_or_oracle_chunked` reads a
first chunk before E6 refuses. CLI and platform handling beyond the `ConfigError` mapping waits for the repin.

## Review fixes (Dennis, GO-WITH-FIXES)

`run_native_or_oracle_chunked` (`native/_dispatch.py`) is now guarded above `reject_per_table_transforms` (module-size
666 to 668) with a pulls == 0 test; `test_refusal_text_is_fixed` asserts the literal message; the `_pipeline.py` history
comment gains "-> 356 (SUB-STOP guard)"; `tests/unit/execution/test_subset_run_guard_entries.py` fails when a public
`run_*`/`compile_*` entry that takes a `config` is neither guarded nor named exempt. The V2-LOG item about
`run_native_or_oracle_chunked` is closed.

`validate_plan` (`plan/validate/_consolidator.py`) now catches the `compile_plan` refusal and returns
`PlanValidationResult(ok=False, plan=None)` with `PlanCheckError(code=subset_run_unsupported, path=subset)` instead of
letting it escape (Codex V1); regression in `tests/unit/plan/test_validate.py`.

## V2-LOG additions

The secret-material census keys on line number, not symbol (the local import in `run_mask_chunked` works around it).
CLI `validate.py` maps the new `PipelineValidationError` to a generic `compile_error` (for the CLI repin). Platform
lanes profile before `compile_plan`; the E6 backstop is accepted for them.

## Not verified

Final `gates.sh --baseline` on the head, sphinx `-W`, macOS arm64 run, the `since` probe in `test_reason_release_pin`
beyond M8. The orchestrator's T3 covers registry and frozen-contract triggers.

## Gates on the exact head

Head `e5d874d4`, base origin/main `55876436`, VM, `scripts/dev/gates.sh --keep-going --baseline`: branch ruff, format,
mypy pass; sentry 4117 passed; licenses 63 passed; tests 6143 passed (baseline 6127). Failing-id diff: branch 0,
baseline 0, branch-only 0. The commit that adds this section changes only this record.

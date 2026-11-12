# ENGINE-PAR P1 build record: stored failing-id baseline for engine gates

Status: record

Branch `feat/engine-par-p1`, base `adc212f3` (engine main). Plan: `docs/plans/2026-11-06-engine-par.md`
on `origin/plan/engine-par`, P1 only. Builder head `90729041`; this record covers the final-head
checks and the review fixes on top of it (final head named in the last section).

## What P1 adds

- `scripts/dev/gates.sh`: `--baseline-file`, `--write-baseline`, `--full`, the stale-baseline refusal,
  the shrink rule, the skipped-count rule, the environment scrub for stored runs.
- `scripts/dev/_gates_report.py`: failing ids and counts from JUnit, the totals cross-check, and one
  versioned `Baseline` model with `load_baseline` as the only reader of the stored file.
- `tests/unit/scripts/test_gates_baseline_file.py`, `_gates_harness.py` (shared fixtures; added to the
  plan's Placement table on `plan/engine-par`).

## The builder's "160 passed, 2 failed"

Reproduced on the Mac (Apple M4, arm64) on `90729041`: `tests/unit/scripts` plus
`tests/sentry/test_bash32_compat.py` gave 160 passed, 2 failed. Both failures are in
`tests/unit/scripts/test_fk_scale_infra.py`:

- `test_supervisor_kills_a_worker_over_the_rss_guard`
- `test_worker_emits_a_complete_record_at_small_n` (`rss_within_ceiling` is False)

They are Mac-only environment failures, not P1 defects:

- The same file on base `adc212f3` on the Mac gives the same 2 failures (20 passed, 2 failed).
- Both ids are listed in the Mac engine main baseline `~/decoy/mac-engine-main-failing.txt`.
- The P1 diff does not touch that test or the script it drives. The RSS accounting there measures
  `ru_maxrss`, which macOS reports in bytes and Linux in kilobytes, so the ceiling check is wrong on macOS.
- They pass on Linux (see below).

## Review fixes folded in after `90729041`

Dennis (APPROVE 0/0/3/4) and Codex (NO-GO) on `90729041`:

| Finding | Fix |
|---|---|
| Codex 1+4 (HIGH): stored fields accepted by coercion (`true` as workers, `false` as collected); no single schema | one `Baseline` dataclass and `load_baseline`/`_parse_baseline`; `format_version` required and exactly 1; exact non-negative ints, bool rejected; `failing` is `list[str]`; every mode and map field typed; unknown fields refused; `check_stored`, `_stored_diff`, `write_baseline` and the gates.sh reads all go through it; the writer serializes the same model |
| Dennis M1 / Codex 2 (HIGH): the Markdown exception hides a docs-walking sentry re-break | `DOC_WALK_GUARDS` (`tests/sentry/`, the leak sentry): a stored failing id under any of them makes any main move stale; comment names the reason |
| Dennis M2: copies of JUnit and JSON parsing in bash | reporter subcommands `--suite-collected`, `--check-stored` (prints `main_sha`), `--stored-failing-under`; no inline JSON or JUnit parsing in gates.sh (guard test) |
| Dennis M3 / Codex 3: ids keyed by the defining file for inherited tests | `_locate` resolves the longest existing module prefix of `classname` under `TEST_ROOT`; `file_seconds` uses the same; synthetic and real-pytest inherited-class tests |
| L1 | strict types and `format_version` (above) |
| L2 | `check_stored` verifies `order == order_label(order_seconds)` |
| Codex 5 | `_gates_harness.py` added to the plan Placement table (`plan/engine-par`, commit `2e9fb20a`) |

Two regressions were caught by the Mac run and fixed before the final head: a bare `"${ARR[@]}"` in
gates.sh (bash 3.2, `test_bash32_compat.py`), and the dataclass needing `sys.modules` registration in
`report_module()` of the test harness.

## Smoke tests (Mac, `1ec716a9` scripts; the later fixes do not change these paths)

- `gates.sh --steps tests --write-baseline /tmp/smoke.json tests/unit/scripts/test_gates_baseline_file.py`
  exits 2 with "--write-baseline refused: this run is a selection, not the whole suite"; no file written.
- `--baseline-file` with a baseline lacking the format field exits 2 ("has format None").
- A baseline for another platform exits 2 naming both run modes.
- A baseline with `main_sha` not in the repository exits 2 ("is not in this repository").
- A baseline with `main_sha` 2986fd5a (older main) exits 2 ("main changed CHANGELOG.md, which a test names").

## Mutations

Reconstructed from the plan's section 6 and the AT table plus the review fixes, because the builder's
numbered list was not available to the final-head agent; the numbering below is this record's. Each is
a one-line change in a scratch clone of the final head (not the worktree), run against the named
guard test with `pytest -k`, one at a time on the Mac. All 23 went red on the final head.

| # | Mutation | Red test |
|---|---|---|
| 2 | stale check warns instead of exit | `test_a_baseline_behind_a_code_move_of_main_is_refused` |
| 3 | `.md` a test names treated as docs-only | `..._markdown_a_test_names_is_refused` |
| 4 | `DOC_WALK_ROOTS` ignored | `..._markdown_a_sentry_walks_is_refused` |
| 5 | collected-count (shrink) rule dropped | `test_a_run_that_collects_ten_fewer_ids_than_the_baseline_fails` |
| 6 | run-mode check dropped | `test_a_baseline_from_another_run_mode_is_refused_before_running` |
| 7 | write allowed for a selection | `test_write_baseline_refuses_a_selection` |
| 8 | write allowed from a dirty or non-main HEAD | `test_write_baseline_refuses_a_head_that_is_not_a_clean_origin_main` |
| 9 | skipped-count rule dropped | `test_baseline_file_fails_when_the_branch_skips_more_than_the_baseline` |
| 11 | totals cross-check dropped | `test_write_baseline_refuses_when_pytest_totals_disagree_with_the_junit` |
| 12 | environment scrub dropped | `..._ignores_a_selection_smuggled_in_through_the_environment` |
| 13 | zero-case JUnit accepted | `test_write_baseline_refuses_a_zero_test_junit` |
| 14 | shrink vs deleted accounting dropped | `test_a_run_that_collects_ten_fewer_ids_than_the_baseline_fails` |
| 15 | ids keyed by defining file | `test_an_inherited_test_is_keyed_by_the_collected_module` |
| 16 | docs-walking exception check dropped | `..._docs_walking_sentry_failure_is_stale_after_any_move` |
| 17 | strict stored types dropped | `test_a_baseline_with_a_wrongly_typed_or_unknown_field_is_refused` |
| 18 | `format_version` check dropped | same |
| 19 | order label consistency dropped | `test_a_baseline_whose_order_label_does_not_match_its_map_is_refused` |
| 20 | bool accepted as a count | `test_a_baseline_with_a_wrongly_typed_or_unknown_field_is_refused[collected-True]` |
| 21 | inline JSON parse back in gates.sh | `test_gates_sh_parses_no_json_or_junit_inline` |
| 22 | `file_seconds` keyed by defining file | `test_an_inherited_test_is_keyed_by_the_collected_module` |
| 23 | unknown fields accepted | `test_a_baseline_with_an_unknown_field_is_refused` |
| 24 | guard narrowed to one walker | `..._docs_walking_sentry_failure_is_stale_after_any_move` |
| 25 | `workers` bool accepted (Codex's case) | `test_a_baseline_with_a_wrongly_typed_or_unknown_field_is_refused[workers-True]` |

One mutation (21) first survived: the guard looked for `python -c` but the code is `"$BIN/python" -c`.
The guard now bans `json.load`, `import json`, `ElementTree` and `xml.etree`, and the mutation is red.
An earlier four-way parallel run on the Mac gave unreliable reds (unrelated tests failed first) and was
discarded; the table is from the sequential run.

## Test results on the final head

- Mac (arm64), `tests/unit/scripts` plus `test_bash32_compat.py`: 201 passed, 2 failed (the two
  Mac-only `test_fk_scale_infra.py` ids above).
- Mac (arm64), `CI=true scripts/dev/gates.sh --keep-going --baseline` on the final head: branch `tests`
  step 144 passed, 0 failed (baseline side 47 passed); `licenses` 63 passed; no failing test id is
  branch-only (11 `test_native_release_workflow.py` ids fail on both sides: macOS `openssl` has no
  ed25519 `genpkey`). Branch-only steps reported: `env`, because the Mac scratch venv `venv-p1` differs
  from `uv.lock` (one package), a Mac environment state and not a P1 effect. `mypy` fails on both sides
  (one unused-ignore in `fileaccess/_openat2.py`, Mac only). Overall exit 1 comes from the `env` step.
- Linux VM, T1 (`tests/unit/scripts`, `tests/sentry`, `test_bash32_compat.py`) and gates: NOT RUN on the
  final head. All four test VMs were busy (queues of 2 to 14 jobs behind multi-hour gates runs);
  `vm-run` jobs sat in the lock queue for over an hour and were cancelled so none is left queued.
  The command to run is `~/bin/vm-run engine . 'CI=true $PY -m pytest tests/unit/scripts tests/sentry
  -q -p no:cacheprovider -rf; CI=true scripts/dev/gates.sh --keep-going --baseline'` on the head named
  in the last section.

## Deviations from the plan (builder and this pass)

- The stored file carries `format_version` (the plan's baseline JSON list had no version field); the
  reporter refuses a missing or unknown version and unknown fields, so P2 changes the format by
  bumping the version.
- `check_stored` refuses an old-format baseline (no `skipped`, no `format_version`) instead of warning;
  the "no skipped count" warning path was removed.
- `test_gates_baseline_file.py` is 819 lines (plan estimate about 380). Under the 900-line STOP; logged V2.
- `_gates_harness.py` is a new shared helper, now in the plan Placement table.
- The `decoy-vm-t3` edit and its `agent-config` commit that the plan assigns to P1 are not in this branch.

## V2-LOG

- Dennis L3: dead selection branch in `_stored_diff`.
- Dennis L4: net-count shrink rule (deleted ids are netted against the shortfall).
- Codex 6: admin-only evidence prints raw failing ids, paths and stored mode values.
- Codex 7: plan P1 test estimate is stale (819 lines against about 380).
- `file_seconds` and `_locate` resolve `classname` against the checked-out tree, so failing ids of the
  baseline-side worktree in legacy `--baseline` mode are resolved against the branch tree.
- Codex DESIGN fix: gates.sh single-reader guard is now a structural rule (python may run only the
  reporter, `-m pytest` or `--version`; no `-c`, stdin program, heredoc python, aliasing or jq). The one
  inline `-c` use (import-origin line in `--baseline` meta) moved to `_gates_report.py --import-origin`.
- `BaselineError` keeps its cause chain (`_gates_report.py` ~394); admin diagnostics only.
- Admin evidence prints modes, paths and failing ids (~423, ~435).
- Direct baseline writes are non-atomic (~485); the promoter is atomic.
- Test file is 819+ lines against about 380 planned.

## Guard fix result (code head 76c8f461 before this record commit)

Run on GCP VM decoy-test-5 (Mac lock was held by a long full run with three queued).
`tests/unit/scripts` + `tests/sentry/test_bash32_compat.py`: 211 passed. `bash -n`, ruff check and format clean.
- Mutation (a): appended `"$BIN/python" -c 'from json import load; ...'` to gates.sh: the guard test went red (1 failed).
- Mutation (b): allowed `-c` in the rule: 3 seeded cases went red (3 failed, 7 passed).
Both reverted.

## Not done

- The full suite and the first baseline write and promote (`decoy-vm-t3 <vm> engine <main> base-eng write`):
  orchestrator.
- `~/bin/decoy-vm-t3` change to pass the file (agent-config repo): orchestrator.
- Hosted CI on the final head.

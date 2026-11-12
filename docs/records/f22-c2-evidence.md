# F22 slice c2: build evidence

Slice c2 of `docs/plans/2026-10-30-f22-publication-replan.md` (revision 11, V1 scope; design 5.7,
classes 4 and 6, V1-b): the isolated parent and the in-process fallback publish through
`publish_unit`; the child stages through `StagedParquetWriter` (no descriptor, lock or nested
unit); the parent establishes the staged set itself, repairs modes and lets `publish_unit`
measure; `atomic_commit`, `stage_tables` and `sibling_staging_dir` are deleted.

## Environment

- Container, Linux, effective uid 0. Lock profile `.venv` (`bash scripts/dev/bootstrap.sh --check`:
  OK at the start; not re-synced). `.venv/bin/python -V`: Python 3.10.20. PyArrow 24.0.0,
  DuckDB 1.5.4.
- Branch `feat/f22-c2`. Slice start commit `f9054196` (c1b head). Main baseline for failure sets:
  `a3cb996f`, run in the existing detached worktree `scratchpad/wt-main` (HEAD verified
  `a3cb996f`) with `PYTHONPATH=<worktree>/src`, run from the worktree root.
- Commits: `d996f324` red tests; `8f57dcb3` production, expected-output updates of existing
  tests, sentry registers, docs, changelog; `ff3a3b45` two test hooks adapted to the new staging
  and `_run_isolated`'s `request` defaulted (found by the second check command). Code head for
  every check below: `ff3a3b45`; the commit after it adds only this record.

## Production files

| File | Lines (start to head) | Change |
|---|---|---|
| `execution/_isolated_run.py` | 660 to 605 | `request=` keyword on `run_pipeline_isolated` (checked before any I/O; a vault refused); the parent opens a `publication` stage beside `output_dir` (`begin_publication`) before the spawn and hands its path to the child; `_result_from_envelope` admits, reads back and publishes through `collect`, a failure through `failed_publication`; the fallback through `publish_resident`; the stage is aborted in `finally` (a no-op once bound); `_commit` and the fallback's sibling-stage commit removed; payload sources written owner-only through `stage_sources` |
| `execution/_isolated_commit.py` | 153 to 346 | `atomic_commit`, `stage_tables`, `sibling_staging_dir`, `resolve_staging_output_dir`, `read_staged_row_errors` deleted. New: `Publication`, `check_request`, `begin_publication`, `publish_resident`, `_names_ok` (envelope names: one path component each, no `..`, no repeat; the sink's `_validate_table_name`), `_admit` (the stage listed through the policy's `open_dir`, must equal exactly the named `<t>.parquet`), `_own` (mode set through the entry's own `O_RDONLY|O_NOFOLLOW` descriptor and `fchmod`, regular file or directory checked by `fstat`), `collect`, `failed_publication`, `row_errors_of` (row errors from the envelope), `stage_sources`. `read_envelope` now refuses a non-object JSON. `CommitError` kept (see "Plan differences") |
| `execution/_isolated_worker.py` | 271 to 271 | `_StagingSink` (the child's sink: `StagedParquetWriter` over the parent's stage, private files; `commit`/`abort` no-ops; `spill_parent`, `committed_table_path`, `staged_tables`, `bounded_write_batches` kept for the routes); `_finalize_outputs` stages resident outputs through the same sink; row errors travel in the envelope; the envelope is written with `fileaccess.write_private` (owner-only at creation); `_stage_outputs` and `_stage_row_errors` deleted |
| `execution/_isolated_common.py` | 388 to 391 | `IsolatedRunResult.publication_receipt` (additive, before `memory_evidence`); `commit_error_text(receipt)`: `publication <outcome>: <code>` plus ` (errno=<name>)` |

Four files, exactly plan 9's list. `execution/` gains no file. Production lines: +437/-296 (net
+141; see "Plan differences"). Census: `_isolated_run.py` 660 to 605 (net -55, rule net <= 0), its
`test_module_size.py` ceiling ratcheted down 672 to 625 (the sentry demands a ceiling within 20 of
the size; a ratchet down, never up).

Registers and docs (no threshold, census number or pin raised):

- `tests/sentry/file_access_registry.toml`: 16 rows removed (the deleted sites), 4 added with
  rules (`_own` `os.open` `read`, `collect` `pyarrow.parquet.read_table`, `_result_from_envelope`
  `shutil.rmtree` ordinal 1, `_StagingSink.__init__` `os.makedirs` `protected_child`); count line
  updated.
- `tests/sentry/baselines/private-file-sinks.json`: 4 keys removed (`_write_payload` sources,
  `_stage_outputs`, `_stage_row_errors`, `_write_envelope_file`: all four are now owner-only from
  creation; shrink only).
- `tests/sentry/protected_dir/test_protected_dir.py` expected-category map: the 4 deleted creator
  rows out, `_StagingSink.__init__` in.
- `tests/sentry/test_reason_registry.py` `ADAPTERS`: `("execution/_isolated_commit.py", "_failed")`,
  `_HELPER` (its `code` defaults to the registry symbol `PUBLICATION_FAILED`; no caller passes one).
- `tests/sentry/test_module_size.py`: the ceiling above.
- `docs/publication.md`: intro; new section "The isolated route on `publish_unit`".
- `changelog.d/f22-c2-isolated-publish-unit.md` (with a `Breaking:` line).

## Red first (start commit `f9054196`)

The final versions of the slice tests were run again on `f9054196` in a temporary detached
worktree (`decoy_engine.__file__` printed under it; removed after): 22 failed, 1 skipped (the
fallback has no child to plant), none passed.

| Test | Red because |
|---|---|
| `tests/unit/execution/publication/test_isolated_parent_measures.py::test_isolated_parent_measures[descriptor,extra_file,nested_dir,symlink,missing_table,bad_name]` | absent API: `request=` is forwarded to the child and fails JSON serialization (`ValueError`); without it the parent renames the planted entries into `output_dir` |
| `::test_isolated_parent_measures_the_bound_bytes` | absent API (same) |
| `::test_unmodified_child_stages_plain_files_only` | wrong outcome: the streamed child binds its own unit, the stage holds `_decoy_publication.json` and a lock file sits beside it |
| `::test_isolated_publish_leaves_only_the_unit_and_its_lock` | absent `IsolatedRunResult.publication_receipt` |
| `test_isolated_umask.py::test_umask_0477_first_attempt_publishes[real,plain_writer]` | absent receipt (`AttributeError` in the spawn child; probe `0o200` recorded) |
| `test_isolated_marker_corpus.py::test_marker_corpus_user_surfaces[*]` (9) | absent API (`request=`, receipt) |
| `test_route_outcome_matrix.py::test_route_outcome_matrix_isolated[isolated_parent,isolated_fallback]` | absent API (`request=`, receipt) |

Between the red commit and the production commit, two of these tests were strengthened, not
weakened: `test_isolated_parent_measures` now also asserts that the refusal happened in the
parent's admission (no `result_id`, no artifacts, no errno but `ELOOP` for the link), added when
mutation 2 below first stayed green (the descriptor write's `O_EXCL` refused the planted
descriptor a step later); the umask test's fake producer now leaves its table `0644` (readable,
too broad) instead of `0o200`, because the parent refuses, rather than repairs, a file its owner
cannot read (see "Plan differences"). Both final versions are red on `f9054196` (above).

## Mutations (each applied once, its test shown red, reverted; the tree compared after)

| # | Mechanism (plan) | Mutation | Red |
|---|---|---|---|
| 1 | class 4: the isolated parent on `publish_unit` | `collect` binds with `txn.publish()` and returns no receipt (the old plain rename) | `test_route_outcome_matrix_isolated[isolated_parent]` |
| 2 | class 6: no child-written descriptor | `_admit` drops `_decoy_publication.json` from the listing (trusts it) | `test_isolated_parent_measures[descriptor]` |
| 3 | V1-b: the parent's per-entry mode repair | the `_own(stage / component, 0o600)` loop removed | `test_umask_0477_first_attempt_publishes[plain_writer]` |
| 4 | class 9: census | `_reject_unserializable_kwargs` moved to `_isolated_commit.py` without the census | `tests/sentry/test_module_size.py` (595 LOC, "no longer needs an entry") |
| 5 | class 1: value-free text | `failed_publication`'s `error` gains the stage path | `test_isolated_marker_corpus.py` (7 cells) |

Class 8's non-vacuity deletion check was not run.

## Checks (head `ff3a3b45`)

Every pytest run: `.venv/bin/python -m pytest <paths> -q -p no:cacheprovider -n 2 -rfE`, one at a
time, `/tmp/pytest-of-root` removed before each. Baseline `a3cb996f` in its worktree.

| Command (paths) | Tree | Passed | Failed | Errors | Skipped | Collected |
|---|---|---|---|---|---|---|
| `tests/unit/publication tests/fileaccess tests/sentry` | head `ff3a3b45` | 6432 | 11 | 87 | 6 | 6536 |
| `tests/fileaccess tests/sentry` (`tests/unit/publication` is not on main) | base `a3cb996f` | 6138 | 10 | 87 | 6 | 6241 |
| `tests/unit/execution tests/integration tests/parity tests/security tests/physical/test_unified_slice_isolated_worker.py` | head | 12423 | 0 | 6 | 2390 (+37 xfailed) | 14856 |
| `tests/unit/execution tests/integration tests/parity tests/security` | base | 12286 | 0 | 6 | 2389 (+37 xfailed) | 14718 |
| `tests/unit --ignore=tests/unit/execution --ignore=tests/unit/scripts` | head | 13371 | 0 | 0 | 84 (+2 xfailed, 5 xpassed) | 13462 |
| uid 65534 (below) | head | 22 | 0 | 0 | 1 | 23 |

First command, failing ids: head = baseline plus one id,
`tests/sentry/protected_dir/test_protected_dir.py::test_the_walk_is_not_vacuous` (the stop
condition below). Shared: the 56 errors of `tests/fileaccess/scratch/test_private_creators.py`
and 31 of `tests/fileaccess/scratch/test_scratch_arena.py` (both need a non-root uid) and the 10
failures of `tests/sentry/layout/test_import_layers.py` (import-linter is not in `.venv`; gates
runs it in `.venv-layers`, PASS below). No baseline id passes on the head. Collected +295: the
`tests/unit/publication` directory (a2, a3) and the sentry, `tests/fileaccess` and registry
differences of the stack since `a3cb996f`.

Second command, failing ids: identical (the 6 errors of
`tests/unit/execution/test_scratch_root_umask.py`, root). Collected +138: the stack's
`tests/unit/execution/publication` (c1a, c1b, c2) and the one physical file added to this run.
An earlier run of the same command on `8f57dcb3` failed 4 ids in
`tests/unit/execution/test_row_dirs_routed.py` (they call `_run_isolated` without the new
`request`); `ff3a3b45` defaults it, and the run above is on `ff3a3b45`.

Third command: no failing id (the c1b record has none on the same tree shape). An earlier run on
`8f57dcb3` failed 4 cells of `tests/unit/profile/test_declaration_gate.py` (its staging spy reached
`pyarrow.parquet` through `_isolated_run.pq`, which the module no longer imports); fixed in
`ff3a3b45`.

Signature grep (`git grep -l` in `tests/` for `run_pipeline_isolated`, `IsolatedRunResult`,
`commit_error_text`, `_isolated_commit`, `_isolated_worker`, `_isolated_run`,
`publication_receipt`, `StagedParquetWriter`): every hit is under a directory the commands above
run, except `tests/physical/test_unified_slice_isolated_worker.py` (added to the second command,
passed) and `tests/perf/test_governor_reroute_completion.py` (perf suite, not run).

`.venv/bin/ruff check src tests`, `.venv/bin/ruff format --check src tests`,
`.venv/bin/mypy src/decoy_engine`: clean on the head.

Non-root (uid 65534, no emulation): the 3.10 interpreter copied out of `/root` to a temporary
directory, `setpriv --reuid=65534 --regid=65534 --clear-groups`,
`PYTHONPATH=src:.venv/lib/python3.10/site-packages`, `PYTHONDONTWRITEBYTECODE=1`, basetemp owned
by that uid (uid and `decoy_engine.__file__` printed first): `python -m pytest
tests/unit/execution/publication/test_isolated_umask.py test_isolated_parent_measures.py
test_isolated_marker_corpus.py test_route_outcome_matrix.py::test_route_outcome_matrix_isolated
-q -p no:cacheprovider -n 2`: 22 passed, 1 skipped (the fallback's planted cell). The umask
`0o477` cells publish as uid 65534. Copy removed.

Slice gate: `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline
tests/unit/execution/publication tests/unit/execution/test_isolated_run.py
tests/unit/execution/test_isolated_safe_error.py tests/unit/execution/test_isolated_oom_classification.py
tests/integration/test_f29_isolation.py tests/fileaccess/test_char_other_name_sets.py` on
`ff3a3b45` (only uncommitted path: this record), exit 1. Branch `env`, `ruff`, `format`, `mypy`,
`layers` (15), `licenses` (63), `tests` (261 passed, 1 skipped) PASS; `sentry` FAIL (5221
passed, 1 failed: `test_the_walk_is_not_vacuous`); baseline `sentry` 5188 passed, 0 failed.
Failing ids branch=1, baseline=0, branch-only=1. Its `/tmp/gates.*` and `/tmp/gt.*` directories
were removed.

Not run: the perf suite, the full suite, macOS and Windows (the orchestrator's); `tests/unit/scripts`
(per the disk ruling); the third command on the baseline (the c1b record covers the same
command); class 8's non-vacuity deletion check.

## Plan differences and decisions

- **`CommitError` is kept** (plan 8 says it is deleted). `reason_adapter._FIXED` resolves it by
  import and `tests/sentry/test_exception_hierarchy.py` lists it; both are outside c2's four
  files, so deleting it would break the adapter at import. It is no longer raised anywhere; its
  docstring says so. Deleting it is a two-line follow-up in `reason_adapter.py` and the sentry.
- **The child's stage object is `fileaccess._local._LocalStageDir`** (private=True), built by
  path over the stage the parent opened. `StagedParquetWriter` needs a stage object and
  `fileaccess` has no public constructor for a `StageDir` over an existing engine-made
  directory (every `begin()` makes a new stage beside its target, which would nest a stage in
  the parent's). The same class serves the parent's payload sources (`stage_sources`). V2-LOG:
  a public `fileaccess` entry for it. `StagedParquetWriter` is typed `Callable[[], OutputTxn]` but
  uses only the `StageDir` half; the child casts (a `_transactional_sink.py` annotation change is
  outside c2).
- **How the parent enumerates the stage.** No listing method was added: the parent lists the
  stage through the policy's existing `FileAccess.open_dir(...).entries()` (ambient: `scandir`
  by path; the route is ambient-only) and compares names with the set it derived from the
  envelope. Each admitted entry is then opened by itself, `O_RDONLY|O_NOFOLLOW`, checked regular
  by `fstat`, and set `0600` with `fchmod` (the stage `0700`). A link fails `ELOOP`, a directory
  fails the kind check. The opens go through `fileaccess._output._call` (errno only).
- **Mode repair is through a descriptor, not `fchmodat`** (plan 5.7: "`fchmodat` 0600 ... before
  opening it"). A by-name `chmod` outside `fileaccess` is a P4 finding in
  `tests/sentry/test_private_files.py`, and an `O_WRONLY` open would be a write disposition. So
  a staged file its owner cannot read is refused (`failed`, errno `EACCES`), not repaired. The
  real child never produces one: it creates its files `0600` through the private stage, and the
  envelope and the payload sources are owner-only from creation, so the route publishes under
  umask `0o477` as uid 65534 (below). A too-broad mode is narrowed.
- **The fallback stages into its own `publication` stage and calls `publish_unit`** (plan 5.7:
  "through `publish_tables`"). It is what `publish_tables` does, with the stage held here so a
  refused stage's cleanup is confirmed by looking (`commit_reason`), which
  `tests/integration/test_f29_isolation.py` asserts (`cleanup_state` `confirmed`).
- **Failure classification.** A publication that does not commit is outcome `crashed`, `error`
  `publication <outcome>: <code>`, the receipt on `publication_receipt`, and the F29 reason of
  the typed error (`raise_for_outcome`): `publication_target_occupied` for an occupied target
  (was `publication_failed`; `test_f29_isolation.py` updated to the plan's E3 code),
  `publication_operation_conflict`, `publication_failed`, or `publication_commit_uncertain` with
  publication state `unknown` (the unit is bound, not confirmed durable; `committed_output_dir`
  stays `None`).
- **Row errors** travel in the envelope instead of `row_errors.json` in the stage (the stage holds
  only the named tables, so the descriptor describes every bound file). `output_dir` no longer
  receives `row_errors.json`.
- **Payload sources** sit directly in the private work root (`s0000.parquet`), not in a
  `sources/` subdirectory: a subdirectory made under umask `0o477` is born `0o300` and could not
  be opened for the private creator. `tests/fileaccess/test_x4_scratch_and_names.py` updated to
  the location.
- **Production lines are net +141**, not net negative as plan 9 estimated: the admission and mode
  repair (class 6, V1-b), the child sink class, the failure classification and the fallback stage
  are new; the deleted helpers were short. `_isolated_run.py` itself is net -55.
- **Expected-output test updates** (no assertion weakened): stage-name globs
  `_decoy_isolated_stage_*` to `._decoy_publication_pending_*` (`test_isolated_run.py`,
  `test_isolated_safe_error.py`); the commit-error text in `test_isolated_safe_error.py`; the
  deleted helpers' characterization in `test_char_other_name_sets.py` (hard delete; the envelope
  read-back cell kept); the renamed post-run hook in `test_isolated_oom_classification.py`; the
  staging spy in `tests/unit/profile/test_declaration_gate.py` patches `pyarrow.parquet` directly.
- The isolated win32 refusal is not in this slice (w1). New POSIX-only calls (`O_NOFOLLOW`,
  `fchmod`) sit behind `sys.platform != "win32"` at their call sites; the route already imports
  `resource`.

## Stop condition hit

`tests/sentry/protected_dir/test_protected_dir.py::test_the_walk_is_not_vacuous` asserts
`len(creators) >= 15` (raw directory creators outside the fd-relative modules). This slice
deletes the plan-mandated raw creators (`stage_tables`, `atomic_commit`, the worker's
`_stage_outputs`) and the payload's `sources/` mkdir, leaving 12. Lowering the floor is
weakening an assertion, so it is left at 15 and the test is red on the head (it passes on the
baseline). Owner call needed: lower the floor to 12 (the scanner is not vacuous: 12 creators are
found), or keep it and restore creators. Nothing else in the slice depends on it.

## V2-LOG

- A public `fileaccess` constructor for a private `StageDir` over an existing engine-made
  directory (the child's stage and the payload sources use `_LocalStageDir`).
- `StagedParquetWriter`'s `stage` parameter could be typed `Callable[[], StageDir]`.
- Delete `CommitError` with its `reason_adapter` row and sentry row.
- `ExecutionResult.publication_receipt` is still `None` on `run_pipeline`'s sequential and
  out-of-core routes (`_pipeline_route_exec.py`, c1b's note); inside the isolated child it does
  not matter (the child publishes nothing).
- `run_pipeline_isolated` with a `request` and no `output_dir` ignores the request.

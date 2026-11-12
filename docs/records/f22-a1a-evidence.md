# F22 slice a1a: build evidence

Slice a1a of `docs/plans/2026-10-30-f22-publication-replan.md` (revision 11, V1 scope): 5.0 rule (1)
`_make_dir_core` with the `make_stage_dir_at` and `make_parent_dir_at` entry points, both POSIX
policies; ambient stage and `create_dir` directories through `make_stage_dir_at`; CODEMAP rows
for `docs/publication.md` and `tests/fileaccess/publication/`; `docs/publication.md` created.

## Environment

- Container, Linux, effective uid 0 (root). The lock profile: `.venv` built by
  `bash scripts/dev/bootstrap.sh` (extras dev, lint, vault, cloud); `bootstrap.sh --check`
  reports `OK, environment matches uv.lock`. `.venv/bin/python -V`: Python 3.10.20. PyArrow
  24.0.0, DuckDB 1.5.4, ruff 0.15.22, mypy 2.3.0, pytest 9.1.1. The native companion is not
  installed (native-only tests skip, as on baseline).
- Branch `feat/f22-a1a`; slice start commit `9878d17` (= `origin/main`, unchanged during these
  runs). Code head for every check below: `04ff3d7`; the commit after it adds only this record.
- Because the container runs as root, which ignores owner mode bits, the new tests wrap `os.open`
  and `os.mkdir` in their `spawn` children so that a call needing an owner bit the directory
  lacks raises `EACCES` (plan 7.2, "Root:"), and set `_openat2.FORCE_PORTABLE` so confined opens
  go through that wrapper. The emulation is on only under uid 0. The slice tests and the F25
  creator tests (`tests/fileaccess/scratch/test_private_creators.py`, which refuse to run as root)
  were also run as uid 65534 without emulation (below).

## Red first (start commit `9878d17`, tests from commit `db11405`)

Run in a temporary detached worktree at `db11405` (the slice tests on main's `src/`), with the same
`.venv`: `PYTHONPATH=<worktree>/src .venv/bin/python -m pytest tests/fileaccess/publication -q
-p no:cacheprovider -rA --tb=line`, from the worktree root. The project is installed editable
from the main checkout, so the worktree's `src` was put first on the path (`PYTHONPATH`, and
pytest's `pythonpath = ["src"]` resolves against the worktree root); a session-finish hook printed
`decoy_engine.__file__` and `fileaccess._local.__file__`, both under the worktree, and
`_local.make_parent_dir_at` was absent. Worktree removed afterwards.

Result: 12 failed, 5 passed.

| Test id | Red reason on `9878d17` |
|---|---|
| `test_i_umask_0477_parents_are_0700_through_the_entry_point[strict]` | wrong outcome: `begin()` refused, the new parent `p` is born 0300 and cannot be opened |
| `test_i_umask_0477_parents_are_0700_through_the_entry_point[ambient]` | wrong outcome: parents left 0300; absent API (`_make_dir_core` spy saw nothing) |
| `test_ii_killed_between_mkdir_and_repair_retry_refuses_then_publishes[strict]` | wrong outcome: parent `p` is left 0300 before `a` is reached, so the kill never fires (exit 0, not SIGKILL) |
| `test_ii_killed_between_mkdir_and_repair_retry_refuses_then_publishes[ambient]` | wrong outcome: as strict, the kill never fires |
| `test_iii_eexist_continues_through_the_existing_directory[ambient]` | hook not reached (non-vacuity line `"ino" in got` fails): see below |
| `test_v_set_group_id_parent[18]`, `[319]` | absent API: `make_parent_dir_at` (`AttributeError`) |
| `test_vii_ensure_dir_under_0477[strict]` | wrong outcome: `ensure_dir` refused (0300 parent); spy empty |
| `test_vii_ensure_dir_under_0477[ambient]` | wrong outcome: `p` is 0300 (192) |
| `test_viii_no_umask_round_trip_and_no_proc_read[strict]` | wrong outcome: refused (0300 parent) |
| `test_viii_no_umask_round_trip_and_no_proc_read[ambient]` | absent API: the core spy saw no call (non-vacuity line) |
| `test_umask_0477_first_attempt_publishes[ambient]` | wrong outcome: the `mkdtemp` stage or the `create_dir` subdirectory is 0300 (192), not 0700 |

`test_iii[ambient]` is red on Python 3.10 and green on 3.11 for a version reason, not an outcome:
main's ambient walk makes parents with `Path.mkdir(parents=True, exist_ok=True)`, and on 3.10
`Path.mkdir` calls `pathlib._NormalAccessor.mkdir`, the builtin `os.mkdir` bound when `pathlib`
was imported, so the test's `os.mkdir` hook is never reached and its non-vacuity line fails. On
3.11 `Path.mkdir` calls `os.mkdir` at call time, the hook fires, and the case is green at
baseline (characterization). On the head it is green on 3.10: the ambient walk calls
`os.mkdir(..., dir_fd=)` through `_make_dir_core`.

Green at baseline on 3.10 (characterization, each paired with a mutation below):
`test_iii_eexist_continues_through_the_existing_directory[strict]`,
`test_iv_umask_022_parents_keep_the_born_mode[strict]`, `[ambient]`,
`test_vi_ambient_user_components_keep_path_semantics`,
`test_umask_0477_first_attempt_publishes[strict]` (main's strict stage already used
`make_stage_dir_at`, which F25 S1a-0 landed before this slice).

Earlier note (Python 3.11.18, a venv off the lock profile): 11 failed, 6 passed, the same set
without `test_iii[ambient]`; the same sets under uid 65534.

## Mutations (each applied once on the production commit, run, then reverted with `git checkout -- src`)

| Mutation | Test that went red |
|---|---|
| drop the stage owner-bit repair (`make_stage_dir_at` as plain `mkdir` + open) | `test_umask_0477_first_attempt_publishes[strict]`, `[ambient]` |
| treat `EEXIST` as an error in `make_parent_dir_at` | `test_iii_...[strict]`, `[ambient]` |
| repair an existing unopenable parent on `EEXIST` (strict path) | `test_ii_...[strict]` |
| repair an unopenable existing ancestor in the ambient walk | `test_ii_...[ambient]` |
| parent permission bits 0700 whatever the born mode | `test_iv_...[strict]`, `[ambient]` |
| clear set-group-ID in the parent entry point (`keep_special=False`) | `test_v_set_group_id_parent[18]`, `[319]` |
| apply `validate_component` to ambient user components | `test_vi_ambient_user_components_keep_path_semantics` |
| raw `os.mkdir` / `Path.mkdir` left in `ensure_dir` (both policies) | `test_vii_...[strict]`, `[ambient]` |
| `os.umask` round trip in `_make_dir_core` (pairs (viii), not named in the plan) | `test_viii_...[strict]`, `[ambient]` |

The plan's mutation "report `published` over an unopenable same-uid parent" needed two forms because
the ambient walk opens existing ancestors by path, not through `make_parent_dir_at`. Working tree
clean after every revert; no commit carries a mutation.

Re-run on the code head `04ff3d7` (lock profile) for the mutations on the ambient walk and the
`EEXIST` path, each applied once, run on `tests/fileaccess/publication -n 2`, reverted with
`git checkout -- src` (tree clean after each):

| Mutation | Tests that went red |
|---|---|
| treat `EEXIST` as an error in `make_parent_dir_at` | `test_iii[strict]`, `test_iii[ambient]`, also `test_ii[strict]` and `test_umask_0477_first_attempt_publishes[strict]` |
| repair an unopenable existing ancestor in the ambient walk (`chmod` 0700 and reopen in `_local._open_dir`) | `test_ii[ambient]` |
| repair an existing unopenable parent on `EEXIST` (strict path, `_open_existing_dir`) | `test_ii[strict]` |

## Checks (lock profile, code head `04ff3d7`)

All runs from `/home/user/decoy-engine` unless a worktree is named. Baselines ran in a detached
worktree of `origin/main` (`9878d17`) with the same `.venv` and `PYTHONPATH=<worktree>/src`; a
session-finish hook confirmed `decoy_engine` was imported from the worktree. `tests/unit/publication`
does not exist yet (slice a2 creates it) and pytest exits 4 on a missing path, so it was dropped
from the first command on both sides.

| Command | Tree | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| `.venv/bin/python -m pytest tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE` | `9878d17` | 5890 | 10 | 56 | 7 | 5963 collected |
| same | `04ff3d7` | 5910 | 10 | 56 | 7 | 5983 collected; same 66 failing ids |
| `.venv/bin/python -m pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE` | `9878d17` | 12230 | 0 | 6 | 2389 | 37 xfailed |
| same | `04ff3d7` | 12230 | 0 | 6 | 2389 | 37 xfailed; same 6 failing ids |
| `.venv/bin/ruff check src tests` | `04ff3d7` | All checks passed | | | | |
| `.venv/bin/ruff format --check src tests` | `04ff3d7` | 2210 files already formatted | | | | |
| `.venv/bin/mypy src/decoy_engine` | `04ff3d7` | Success, 785 source files | | | | |
| `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline tests/fileaccess/publication tests/fileaccess tests/unit/execution/test_b6b_sink.py` | `04ff3d7`, baseline `9878d17` | exit 0 | | | | see below |

Failing ids, first command: identical sets on baseline and head (66 ids): the 56 errors of
`tests/fileaccess/scratch/test_private_creators.py` (its fixture asserts a non-root uid) and 10
failures of `tests/sentry/layout/test_import_layers.py` (import-linter is not in `.venv`; gates
runs that file in `.venv-layers`, where its `layers` step passes). No new failing id; no baseline
id now passes. Collected count 5963 to 5983 (+20: the 17 new tests, the new seeded case of
`test_private_files.py`, and 2 sentry cases not itemised here).

Failing ids, second command: identical on both sides, the 6 errors of
`tests/unit/execution/test_scratch_root_umask.py` (root precondition). Collected count unchanged.

gates.sh (EVIDENCE block, Python 3.10.20, workers 2): branch `env`, `ruff`, `format`, `mypy`
(`src/decoy_engine testflight`), `sentry` (ratchet check, then 4965 passed, 0 failed, 2 skipped),
`layers` (15 passed) and `licenses` (63 passed) all PASS. `tests`: branch 985 passed, 56 errors,
7 skipped; baseline 968 passed, 56 errors, 7 skipped; failing ids branch=56 baseline=56
branch-only=0 (all `test_private_creators.py`, root precondition). Branch-only failing steps: none.

Signature grep (`git grep` in `tests/` for `make_stage_dir_at`, `make_parent_dir_at`,
`_make_dir_core`, `ensure_dir`, `_open_parent`, `create_dir(`, `LocalOutputTxn`, `LocalOutputDir`,
`RootedOutputDir`, `RootedOutputTxn`, `_open_dir`, `_open_dir_creating`, `_make_stage_dir`,
`_ensure_dirs`): every hit is under `tests/fileaccess/`, `tests/sentry/` or is
`tests/unit/execution/test_b6b_sink.py`, all run by the first two commands.

Non-root (uid 65534, no emulation), code head `04ff3d7`: the 3.10 interpreter lives under `/root`
(mode 0700), so it was copied to a world-readable temporary directory and run with
`setpriv --reuid=65534 --regid=65534 --clear-groups`, `PYTHONPATH=src:.venv/lib/python3.10/site-packages`
(uid and `decoy_engine.__file__` printed first), `PYTHONDONTWRITEBYTECODE=1` and a basetemp owned
by that uid: `python -m pytest tests/fileaccess/scratch/test_private_creators.py
tests/fileaccess/publication -q -p no:cacheprovider` gave 73 passed (56 F25 creator cases,
including `public_dir_stage_unchanged` and `output_parent_unchanged`, plus the 17 slice tests).

### Why the earlier 3.11 run missed a sentry failure

The first record ran on Python 3.11.18 in a venv without the `cloud` extra. There
`tests/sentry/test_outbound_clients.py` fails on both sides because `google-cloud-storage` is
not importable (`gcs_name_set` fails rather than skips), which hid a branch-only failure of
`test_gcs_calls_carry_timeout_and_retry`: the new `os.open` in `_local._open_dir` was a new
`.open` collision key, and `baselines/gcs-call-kwargs.json` is shrink-only (ratchet). Commit
`151d0b3` replaced the hand-written errno-only wrapper in `_open_dir` with the existing
`_output._call` (the same helper `_output.py` uses for its `os.open` calls); the outbound sentry
treats `_call(os.open, ...)` as a call of `_call`, so the baseline is untouched. The file-access
scan still records the site as `os.open` (`_local.py`, `_open_dir`, ordinal 0), so the registry
row the slice added is unchanged. `test_private_files.py` read the `os.open` flags at the wrong
argument of a `_call` site and failed closed (a false P1 on that `read` row); it now skips the
wrapped callee as `fa/rules.py` does, with a seeded write case (P1) and a seeded read-only control.
The same 3.11 venv also lacked `ruff` and `mypy`; its 3 RUF036 and 158 mypy lines came from tools
outside the lock and do not appear on the lock profile.

Earlier note (Python 3.11.18, off the lock profile, head `f3bbb0f`): first command 5885 passed,
12 failed, 77 errors, 7 skipped, same 89 ids as its baseline; second command 12230 passed,
6 errors, 2389 skipped, 37 xfailed on `c563498`; gates `env`, `ruff`, `format`, `mypy` failed for
the environment.

## Tests changed outside the new directory (fault seams and sentry reading, assertions unchanged)

- `tests/fileaccess/test_write_private.py::test_error_text_is_value_free[begin]`: the fault was
  injected at `Path.mkdir`, which the POSIX arm no longer calls; it now also fails the
  directory open the POSIX arm makes first.
- `tests/fileaccess/test_x4_scratch_and_names.py`: the creation probe recorded a bare name for a
  descriptor-relative `os.mkdir`; it now resolves `dir_fd` before judging the location.
- `tests/unit/execution/test_b6b_sink.py`: its `mkdtemp` spy no longer saw ambient stages; it
  now also observes `_local._make_stage_dir`.
- `tests/sentry/file_access_registry.toml`: one row for `_local._open_dir` (`os.open`, `read`);
  count 190 to 191. The `win32` arms keep main's calls at the same registry keys.
- `tests/sentry/test_private_files.py` and its `fixtures/private_files/` corpus: flags of a
  `_call(os.open, ...)` site are read one argument later (above); with that shift removed, the
  `_open_dir` row and the new seeded control both go red.
- The two unit-file content checks in `tests/fileaccess/publication/` compare bytes again: the
  test makes the bound file 0600 first (it is born 0200 under umask 0477; file modes are a1b), so
  the check holds under root and uid 65534.

## Not run

- The full suite, the perf suite, and the docs build.
- `tests/sentry/layout/test_import_layers.py` outside gates (no import-linter in `.venv`); gates
  ran it in `.venv-layers` (15 passed).
- macOS: ran on the Mac mini (M4, arm64) at head a6a09e4a. `tests/fileaccess/publication`: 14 passed,
  7 skipped (the strict cells, which need Linux `openat2`), 0 failed. The rest of `tests/fileaccess`
  (strict policy, strace and other Linux-only files) is not green on macOS and is outside this
  slice; the same run showed 51 failed, 592 passed, 8 skipped, 421 errors.
- Windows: the win32 arms are main's code unchanged and were not executed.

## V2-LOG

- The ambient `create_dir` now validates its component with `validate_component` inside
  `make_stage_dir_at`, so a NUL or over-255-byte name raises `FileAccessDenied` where it raised the
  operating system's `OSError` before. Stage subdirectory names are engine-chosen; no user-visible
  path reaches it.
- Ambient parent creation over an existing non-directory now raises `NotADirectoryError` (errno
  only) where `Path.mkdir(exist_ok=True)` raised `FileExistsError`; both refuse.
- Ambient stage directories are named `<prefix><12 hex>` instead of `mkdtemp`'s `<prefix><8 chars>`,
  and their path is relative when the target is (as `mkdtemp` on Python 3.11; 3.12 made it absolute).
- macOS cannot observe the born mode of a new directory the umask left owner-unreadable (no
  `O_PATH`); the repair there is 0700, dropping group and other bits. Named in `docs/publication.md`.

## Plan and brief notes

- The capability sentry (`tests/sentry/fa/rules.py`) does not allow `fstatat` (`os.stat` with
  `dir_fd`) in a capability module, so the born mode on the `EACCES` path is read through an
  `O_PATH` descriptor on Linux (an allowed `os.open`) instead of the plan's `fstatat`. No allowlist
  was widened.
- `make_stage_dir_at` already existed on `main` (F25 S1a-0), and the strict stage and `create_dir`
  already used it; a1a extracted `_make_dir_core` from it and added the special-bit clear the plan's
  case (v) requires.
- The plan's "spy clause" of `test_make_parent_dir_modes_and_crash` is listed under a1b; a1a already
  carries the `_make_dir_core` spies for cases (i), (vi), (vii) and (viii) because the entry point
  exists now.
- `_make_dir_core` takes a keyword `keep_special` besides the plan's `(parent_fd, name, mode)`; the
  two entry points pass literal values.
- `docs/index.md` gained a link and a toctree entry for `docs/publication.md`, outside the plan's
  file list, so the docs build does not warn about an orphan page.

## 2026-11-12: ambient directory close fault (found by the a3 suite run)

Two tests passed on `9878d17` and failed on `126fa46` and every branch stacked on it:

- `tests/unit/test_fixed_width_writer.py::test_fsync_dir_survives_close_error[fsync_and_close_fail-ambient]`
- `tests/unit/test_quarantine_staging.py::TestFdopenFailureDoesNotLeakRawFd::test_fdopen_failure_closes_the_raw_mkstemp_fd`

Cause. a1a made the ambient arm open the directories on an output chain (`_open_dir_creating`,
`_ensure_dirs`, `_make_stage_dir`, `_LocalStageDir.create_dir` in `fileaccess/_local.py`) and
closed them with a raw `os.close`, where the strict arm closes through the best-effort `_close`
of `_output.py`. That is a production defect, not only a test artifact:

- a `close(2)` error on a directory descriptor opened only to make a parent failed a write that
  had nothing wrong with it (the fixed-width ambient case: the strict case of the same test passed);
- in `_open_dir_creating`, a close error inside the loop reached `except BaseException:
  os.close(fd)`, which closed the same descriptor number a second time (`EBADF`, or another
  thread's descriptor) and leaked the child just opened;
- in `_make_stage_dir` and `create_dir` the `finally: os.close(...)` replaced the primary error.

Fix (`760e53a`): those closes go through `_close`. No new mechanism. The quarantine test was the
other kind: its `os.close` spy counted every close in the process, and the ambient arm now
legitimately opens and closes the parent directory it creates (`[12, 13, 12]`: the root, the made
`out`, then the mkstemp fd reusing 12). Its spy now skips directory descriptors (`d767bde`); the
assertion `len(closed_fds) == 1` and its message are unchanged. The fixed-width test is unchanged.

Red first (`5775f95`, run on `126fa46` + that test file):
`tests/fileaccess/publication/test_ambient_dir_close_fault.py` 4 failed: the three
`test_directory_close_fault_does_not_fail_the_call[publish|ensure_dir|create_dir]` cases raised
the injected `EIO` (with an `EBADF` from the double close in the chain), and
`test_directory_close_fault_does_not_mask_the_primary_error` got errno 9 (`EBADF`) instead of
`EACCES`. After the fix: 4 passed. Each case asserts the fault was injected (non-vacuity). On
`9878d17` the `ensure_dir` case would fail its non-vacuity line (no directory descriptor is opened
there), so the file is a1a-onward only.

Why a1a's checks missed it: its pytest commands covered `tests/fileaccess`, `tests/sentry`,
`tests/unit/execution`, `tests/integration`, `tests/parity` and `tests/security`, and its signature
grep looked for the new helper names. Neither reached `tests/unit` outside `execution`, where the
sinks' `os.close` fault tests live.

Runs (lock profile, Python 3.10.20, `-n 2`):

| Command | Tree | Result | Failing ids |
|---|---|---|---|
| `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider -n 2 --ignore=tests/unit/execution -rfE` | `d767bde` | 12699 passed, 94 skipped, 2 xfailed, 5 xpassed | none |
| same, detached worktree, `PYTHONPATH=<wt>/src` (import confirmed under the worktree) | `9878d17` | 12699 passed, 94 skipped, 2 xfailed, 5 xpassed | none |
| `.venv/bin/python -m pytest tests/unit/publication tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE` | `f6756d3` | 5914 passed, 10 failed, 56 errors, 7 skipped | the known 56 `test_private_creators.py` (root) + 10 `test_import_layers.py` (no import-linter in `.venv`); +4 passed over `04ff3d7` are the new tests |
| `.venv/bin/python -m pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE` | `f6756d3` | 12230 passed, 6 errors, 2389 skipped, 37 xfailed | the known 6 `test_scratch_root_umask.py` (root) |
| `.venv/bin/ruff check src tests`, `ruff format --check src tests`, `mypy src/decoy_engine` | `f6756d3` | pass, 2211 files formatted, 785 source files clean | |
| `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline tests/fileaccess tests/unit/test_fixed_width_writer.py tests/unit/test_quarantine_staging.py` | `f6756d3`, baseline `9878d17` | exit 0; env, ruff, format, mypy, sentry (4965 passed), layers (15), licenses (63) PASS; tests branch 1026 passed, 56 errors, 5 skipped vs baseline 1005 passed, 56 errors | branch=56 baseline=56 branch-only=0 |

Failing-id diff for `tests/unit --ignore=tests/unit/execution`: empty on both sides; collected count
identical. The two ids above now pass. `tests/unit/publication` does not exist on a1a; pytest ran
the other two paths. Not run: the uid 65534 pass for the new file (it checks no mode).

V2-LOG: `LocalOutputTxn.create_file`'s private `mkstemp` branch (main's code) still closes with a
raw `os.close` before `abort()` when `fchmod` fails; a close error there would replace the
`fchmod` error. Pre-existing, not touched.

# F22 slice a1a: build evidence

Slice a1a of `docs/plans/2026-10-30-f22-publication-replan.md` (revision 11, V1 scope): 5.0 rule (1)
`_make_dir_core` with the `make_stage_dir_at` and `make_parent_dir_at` entry points, both POSIX
policies; ambient stage and `create_dir` directories through `make_stage_dir_at`; CODEMAP rows
for `docs/publication.md` and `tests/fileaccess/publication/`; `docs/publication.md` created.

## Environment

- Container, Linux, effective uid 0 (root). `python -V` in the project venv: Python 3.11.18.
  PyArrow 24.0.0, DuckDB 1.5.4. `uv sync --frozen --extra dev` was run before the slice.
- Branch `feat/f22-a1a`; slice start commit `9878d17` (= `origin/main`).
- Because the container runs as root, which ignores owner mode bits, the new tests wrap `os.open`
  and `os.mkdir` in their `spawn` children so that a call needing an owner bit the directory
  lacks raises `EACCES` (plan 7.2, "Root:"), and set `_openat2.FORCE_PORTABLE` so confined opens
  go through that wrapper. The emulation is on only under uid 0. The slice tests and the F25
  creator tests (`tests/fileaccess/scratch/test_private_creators.py`, which refuse to run as root)
  were also run as uid 65534 without emulation (below).

## Red first (start commit `9878d17`, tests from commit `db11405`)

Command: `uv run pytest tests/fileaccess/publication -q -p no:cacheprovider -rA --tb=line`.
Result: 11 failed, 6 passed. Same sets under uid 65534 (no emulation): 11 failed, 6 passed.

| Test id | Red reason on `9878d17` |
|---|---|
| `test_i_umask_0477_parents_are_0700_through_the_entry_point[strict]` | wrong outcome: `begin()` refused, the new parent `p` is born 0300 and cannot be opened |
| `test_i_umask_0477_parents_are_0700_through_the_entry_point[ambient]` | wrong outcome: parents left 0300; absent API (`_make_dir_core` spy saw nothing) |
| `test_ii_killed_between_mkdir_and_repair_retry_refuses_then_publishes[strict]` | wrong outcome: parent `p` is left 0300 before `a` is reached, so the kill never fires and nothing publishes |
| `test_ii_killed_between_mkdir_and_repair_retry_refuses_then_publishes[ambient]` | wrong outcome: the retry publishes through the unopenable 0300 parent |
| `test_v_set_group_id_parent[18]`, `[319]` | absent API: `make_parent_dir_at` (`AttributeError`); also `make_stage_dir_at` keeps set-group-ID (0o2700) |
| `test_vii_ensure_dir_under_0477[strict]` | wrong outcome: `ensure_dir` refused (0300 parent); spy empty |
| `test_vii_ensure_dir_under_0477[ambient]` | wrong outcome: `p`, `a`, `out` are 0300 |
| `test_viii_no_umask_round_trip_and_no_proc_read[strict]` | wrong outcome: refused (0300 parent) |
| `test_viii_no_umask_round_trip_and_no_proc_read[ambient]` | absent API: the core spy saw no call (non-vacuity line) |
| `test_umask_0477_first_attempt_publishes[ambient]` | wrong outcome: the `mkdtemp` stage and the `create_dir` subdirectory are 0300 |

Green at baseline (characterization, each paired with a mutation below):
`test_iii_eexist_continues_through_the_existing_directory[strict]`, `[ambient]`,
`test_iv_umask_022_parents_keep_the_born_mode[strict]`, `[ambient]`,
`test_vi_ambient_user_components_keep_path_semantics`,
`test_umask_0477_first_attempt_publishes[strict]` (main's strict stage already used
`make_stage_dir_at`, which F25 S1a-0 landed before this slice).

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

## Checks

All runs from `/home/user/decoy-engine`, `-n 2`. `tests/unit/publication` does not exist yet (slice
a2 creates it) and pytest exits 4 when given a missing path, so it was dropped from the first
command on both sides.

| Command | Head | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| `uv run pytest tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE` | `9878d17` (baseline) | 5867 | 12 | 77 | 7 | |
| same | `c33f376` | 5885 | 12 | 77 | 7 | |
| same | `f3bbb0f` | 5885 | 12 | 77 | 7 | same 89 ids as baseline |
| `uv run pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE` | `9878d17` (baseline) | 12230 | 0 | 6 | 2389 | 37 xfailed |
| same | `c563498` | 12230 | 0 | 6 | 2389 | 37 xfailed |
| `uv run ruff check src tests` | branch and `9878d17` | | 3 (both) | | | RUF036 in `plan/checks/_generate_columns.py`, untouched |
| `uv run ruff format --check src tests` | branch | clean | | | | |
| `mypy src/decoy_engine` (`/root/.local/bin/mypy`, the tool `uv run` resolves) | branch and `9878d17` | | 158 (both, identical lines) | | | missing `numpy` stubs and similar, environment |
| `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline <slice paths>` | `f3bbb0f` | tests: branch=80 baseline=80 failing ids, branch-only=0 | | | | |

Failing ids, first command: identical sets on baseline and branch (89 ids). They are the 77
`tests/fileaccess/scratch/test_private_creators.py` errors (its fixture asserts a non-root uid),
10 `tests/sentry/layout/test_import_layers.py` failures and 2 `tests/sentry/test_outbound_clients.py`
failures. No new failing id; no baseline id now passes. Collected count: 5963 to 5981 (+18: the 17
new tests, plus one sentry case parametrized by the new registry row; gates' sentry step shows
4938 to 4939 passed).

Failing ids, second command: identical on both sides, the 6 errors of
`tests/unit/execution/test_scratch_root_umask.py` (root precondition). Collected count unchanged.
`tests/unit/execution/test_b6b_sink.py::test_a_nested_target_spills_under_its_own_spill_parent_not_tmpdir`
failed once on `c33f376` because its `mkdtemp` spy no longer saw ambient stages; the spy now also
observes `_local._make_stage_dir` (commit `c563498`) and the file passes (9 passed, 2 skipped for the
absent native companion, as on baseline).

gates.sh: its `env`, `ruff`, `format` and `mypy` steps fail on both sides for the environment
(`bootstrap.sh --check`: the venv is Python 3.11.18 where the lock expects 3.10.20; `ruff` and `mypy`
are not installed in `.venv`, rc 127); they are listed as branch-only only because the baseline skips
`env`. Sentry and tests failing ids are the same on both sides; `layers` and `licenses` pass.

Signature grep (`git grep` in `tests/` for `make_stage_dir_at`, `make_parent_dir_at`,
`_make_dir_core`, `ensure_dir`, `_open_parent`, `create_dir(`, `LocalOutputTxn`, `LocalOutputDir`,
`RootedOutputDir`, `RootedOutputTxn`): every hit is under `tests/fileaccess/` or `tests/sentry/`,
covered by the first command.

Non-root (uid 65534, no emulation), branch `f3bbb0f`:
`.venv/bin/python -m pytest tests/fileaccess/scratch/test_private_creators.py tests/fileaccess/publication -q -p no:cacheprovider`
73 passed (56 F25 creator cases, including `public_dir_stage_unchanged` and
`output_parent_unchanged`, plus the 17 slice tests). On `9878d17` the same 56 creator cases pass.

### Final code head

`f3bbb0f` is the last commit that touches code or tests; the commit after it adds only this record.
The first command was rerun there (row above): identical failing ids to baseline, 5981 collected.
The second command's last run is on `c563498`; `f3bbb0f` changed only files under
`tests/fileaccess/publication/`, which that command does not collect.

## Tests changed outside the new directory (fault seams only, assertions unchanged)

- `tests/fileaccess/test_write_private.py::test_error_text_is_value_free[begin]`: the fault was
  injected at `Path.mkdir`, which the POSIX arm no longer calls; it now also fails the
  directory open the POSIX arm makes first.
- `tests/fileaccess/test_x4_scratch_and_names.py`: the creation probe recorded a bare name for a
  descriptor-relative `os.mkdir`; it now resolves `dir_fd` before judging the location.
- `tests/unit/execution/test_b6b_sink.py`: see above.
- `tests/sentry/file_access_registry.toml`: one row for `_local._open_dir` (`os.open`, `read`);
  count 190 to 191. The `win32` arms keep main's calls at the same registry keys.

## Not run

- The full suite, the perf suite, and the docs build.
- macOS (the plan names a Mac mini run before a1a lands): not available here.
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

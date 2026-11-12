# F22 slice a1b: build evidence

Slice a1b of `docs/plans/2026-10-30-f22-publication-replan.md` (revision 11, V1 scope; design
5.0 and 5.1): durable directory publish with the chain to the anchor (capability root on the
strict policies, mount boundary on the ambient one), the `publication` stage label with the
ambient file restore, `OutputDir.lock`, `OutputDir.read_member`, `OutputDir.sync_chain`, both
POSIX policies, and the spy clause of `test_make_parent_dir_modes_and_crash`.

## Environment

- Container, Linux, effective uid 0. Lock profile `.venv` (built by `scripts/dev/bootstrap.sh`;
  `bootstrap.sh --check`: `OK, environment matches uv.lock`). `.venv/bin/python -V`: Python
  3.10.20. PyArrow 24.0.0, DuckDB 1.5.4, ruff 0.15.22, mypy 2.3.0, pytest 9.1.1.
- Branch `feat/f22-a1b`. Slice start commit `126fa464` (a1a head). Main baseline for the two
  pytest commands: `9878d17` (as the brief names); `gates.sh --baseline` uses `origin/main`,
  which had moved to `c96e2855` (two commits, docs and CI only).
- Commits: `fed0e9e` red tests; `b541f34` two test-harness fixes (below); `0df334f` production;
  `622e020` sentry `UNCLASSIFIED` entries (owner decision below). Code head for every check
  below: `622e020`; the commit after it adds only this record.

## Production files and module sizes

`fileaccess/_output.py` 533 to 596, `_local.py` 469 to 578, `_types.py` 262 to 322,
`__init__.py` (exports), and new `fileaccess/_durable.py` 197. `_output.py` would have gone over
600 with the walk in it, so the fsync walk, chain sync, lock and bounded read live in
`_durable.py` as plan 9's module census directs; `fileaccess/` goes from 13 to 14 modules (cap
25). `_durable.py` imports `_output` (for `_call`, `_close`, `_DIR_FLAGS` and the owner-bit
restore); `_output.py` imports `_durable` inside the three methods that use it, so there is no
import cycle at module load.

B-6 (`feat/b6-vault-core`) is not merged, so per plan 5.1 and Q3 this slice introduces
`DURABLE_LABELS = {"publication"}` in `_output.py`. B-6's `STAGE_MODE` does not exist on the
branch; the strict 0600 file mode comes from main's existing private-label path (`publication`
added to `PRIVATE_LABELS`, so dir-stage files go through `create_private_child_at`, which
restores missing owner bits with `fchmod`). The ambient POSIX arm uses the same creator for the
`publication` label. No guarantee changes; B-6 rebases onto the set.

## Red first (start commit `126fa464`, tests from `fed0e9e`)

Detached worktree at `fed0e9e`, `PYTHONPATH=<worktree>/src`, run from the worktree root;
`decoy_engine.__file__` printed under the worktree and `_local._probe_dir` absent. Command:
`.venv/bin/python -m pytest tests/fileaccess/publication -q -p no:cacheprovider -n 2 -rf`.
Result: 32 failed, 19 passed. Worktree removed.

| Test id | Red reason on `126fa464` |
|---|---|
| `test_durable_dir_publish.py::test_durable_dir_publish_fsync_order[strict]` | absent label: `begin(label="publication")` denied |
| `test_durable_dir_publish.py::test_durable_dir_publish_fsync_order[ambient]` | absent API: no `_local._probe_dir` |
| `test_durable_dir_publish.py::test_durable_publish_faults[strict-{file,stage,rename,chain:y,chain:x,chain:anchor}]` (6) | absent label: `FileAccessDenied` at `begin` |
| `test_durable_dir_publish.py::test_durable_publish_faults[ambient-{same six}]` (6) | absent API: no `_local._probe_dir` |
| `test_ancestor_chain.py::test_ancestor_created_by_other_attempt[strict-killed_child]`, `[ambient-killed_child]` | wrong outcome: the child refused the unknown label before any `mkdir`, so it exited 0, not SIGKILL |
| `test_ancestor_chain.py::test_ancestor_created_by_other_attempt[strict-live_begin]`, `[strict-foreign_mkdir]` | absent label: `FileAccessDenied` |
| `test_ancestor_chain.py::test_ancestor_created_by_other_attempt[ambient-live_begin]`, `[ambient-foreign_mkdir]` | absent API: no `_local._probe_dir` |
| `test_ancestor_chain.py::test_unreadable_ancestor_fail_closed_seeded[ambient]` | absent API: no `_local._probe_dir` |
| `test_ancestor_chain.py::test_unreadable_ancestor_fail_closed_create_under[ambient]` | absent label: `KeyError: 'publication'` |
| `test_ancestor_chain.py::test_unreadable_ancestor_fail_closed_foreign_owner` | absent API: no `_local._probe_dir` |
| `test_lock_and_member.py::test_lock_contention_and_unsupported[strict]`, `[ambient]` | absent API: no `OutputDir.lock` |
| `test_lock_and_member.py::test_strict_policy_confined` | absent API: no `fileaccess.LockUnavailable` |
| `test_lock_and_member.py::test_read_member_bounded_and_absent[strict]`, `[ambient]` | absent API: no `read_member` |
| `test_make_parent_dir_modes_and_crash.py::test_viii_no_umask_round_trip_and_no_proc_read[strict-publication]`, `[ambient-publication]` | wrong outcome: the child refused the label; the core and entry spies saw nothing |
| `test_umask_0477_first_attempt_publishes.py::test_umask_0477_first_attempt_publishes[strict-publication]` | absent label: refused (`FileAccessDenied`) |
| `test_umask_0477_first_attempt_publishes.py::test_umask_0477_first_attempt_publishes[ambient-publication]` | absent label: refused (`KeyError`) |

Green at baseline: the 17 a1a ids (unchanged or re-parametrized as `[...-table_sink]`), plus
characterization, each paired with a mutation below: `test_unreadable_ancestor_fail_closed_seeded[strict]`
and `test_unreadable_ancestor_fail_closed_create_under[strict]` (the strict chain already opened
`p` and `q` for reading and was refused, and the spy saw `EACCES`), and the new entry-point spy
lines in `test_i_umask_0477_parents_are_0700_through_the_entry_point` and
`test_vii_ensure_dir_under_0477` (a1a already routed parents through `make_parent_dir_at`).

## Mutations (each applied once on the code head, run, then reverted with `git checkout -- src tests`)

Runner: `.venv/bin/python -m pytest <ids> -q -p no:cacheprovider -n 2`; the tree was clean
after every revert and no commit carries a mutation.

| Mutation (plan source) | Tests that went red |
|---|---|
| drop the stage fsync walk before the rename, both policies (7.2 durable rows; P3/P5) | `test_durable_dir_publish_fsync_order[strict]`, `[ambient]`; `test_durable_publish_faults[strict-file]`, `[strict-stage]`, `[ambient-file]`, `[ambient-stage]` |
| chain stops at the parent, both policies (class 3) | `test_durable_dir_publish_fsync_order[strict]`, `[ambient]` |
| ambient walk stops at the first pre-existing ancestor (class 3; 7.2 `test_ancestor_created_by_other_attempt`) | `test_ancestor_created_by_other_attempt[ambient-killed_child]`, `[ambient-foreign_mkdir]` |
| ambient walk ends at any `EACCES` (class 3; 7.2 unreadable (i)) | `test_unreadable_ancestor_fail_closed_seeded[ambient]` |
| ambient `mkdir(parents=True)` without opening the parent (7.2 unreadable (ii)) | `test_unreadable_ancestor_fail_closed_create_under[ambient]` |
| ambient walk raises on any `EACCES` (owner check dropped; pairs unreadable (iii)) | `test_unreadable_ancestor_fail_closed_foreign_owner` |
| strict: an unreadable existing parent opened search-only and used (pairs characterization (i) strict: "report published over an unopenable same-uid parent") | `test_unreadable_ancestor_fail_closed_seeded[strict]` |
| strict: parents made by path from the root without opening each parent (pairs characterization (ii) strict) | `test_unreadable_ancestor_fail_closed_create_under[strict]` |
| drop the ambient `publication` file restore (7.2 umask 0477) | `test_umask_0477_first_attempt_publishes[ambient-publication]` |
| drop `publication` from the strict private labels (7.2 umask 0477) | `test_umask_0477_first_attempt_publishes[strict-publication]` |
| lock returns without `flock` (7.2 lock row) | `test_lock_contention_and_unsupported[strict]`, `[ambient]` |
| strict lock opened by path from the root, links followed (7.2 confined row) | `test_strict_policy_confined` |
| bypass `make_parent_dir_at` (call `_make_dir_core` directly; pairs the new entry-point spy lines) | `test_i_umask_0477_parents_are_0700_through_the_entry_point[strict]`, `[ambient]`; `test_vii_ensure_dir_under_0477[strict]`, `[ambient]` |
| class 8 check, test side: the `live_begin` hook made a no-op (never fires) | `test_ancestor_created_by_other_attempt[strict-live_begin]`, `[ambient-live_begin]` red; with the non-vacuity line `assert fired` also deleted the same 6 tests pass, so that line is what makes the case non-vacuous |

## Checks (code head `622e020`)

| Command | Tree | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| `.venv/bin/python -m pytest tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE --tb=no` | `9878d17` (worktree) | 5890 | 10 | 56 | 7 | 5963 collected |
| same | `622e020` | 5948 | 10 | 56 | 7 | 6021 collected; same 66 failing ids |
| `.venv/bin/python -m pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE --tb=no` | `9878d17` (worktree) | 12230 | 0 | 6 | 2389 | 37 xfailed |
| same | `622e020` | 12230 | 0 | 6 | 2389 | 37 xfailed; same 6 failing ids |
| `.venv/bin/ruff check src tests` | `622e020` | All checks passed | | | | |
| `.venv/bin/ruff format --check src tests` | `622e020` | 2215 files already formatted | | | | |
| `.venv/bin/mypy src/decoy_engine` | `622e020` | Success, 786 source files | | | | |
| `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline tests/fileaccess/publication tests/fileaccess tests/unit/execution/test_b6b_sink.py tests/unit/execution/test_isolated_run.py` | `622e020`, baseline `origin/main` `c96e2855` | exit 0 | | | | below |

`tests/unit/publication` does not exist yet (slice a2), so the first command drops it on both
sides, as in a1a. Baselines ran in a detached worktree with the same `.venv` and
`PYTHONPATH=<worktree>/src`; `decoy_engine.__file__` printed under the worktree.

Failing ids, first command: identical sets on baseline and head (`diff` empty), 66 ids: the 56
errors of `tests/fileaccess/scratch/test_private_creators.py` (fixture asserts a non-root uid)
and the 10 failures of `tests/sentry/layout/test_import_layers.py` (import-linter is not in
`.venv`; gates runs it in `.venv-layers`, where it passes). No new failing id; no baseline id
now passes. Collected 5963 to 6021 (+58): the baseline is main, before a1a, so 20 are a1a's
(its record) and 38 this slice's: 34 in `tests/fileaccess/publication` (30 in the three new
files, 4 new parametrized cases in the two a1a files) and 4 sentry cases.

Failing ids, second command: identical on both sides, the 6 errors of
`tests/unit/execution/test_scratch_root_umask.py` (root precondition). Collected count unchanged.

gates.sh EVIDENCE (Python 3.10.20, workers 2): branch `env`, `ruff`, `format`, `mypy`, `sentry`
(ratchet check, then 4969 passed, 0 failed, 2 skipped), `layers` (15 passed), `licenses` (63
passed) all PASS. `tests`: branch 1046 passed, 56 errors, 7 skipped; baseline 995 passed, 56
errors, 7 skipped; branch=56 baseline=56 branch-only=0 (all `test_private_creators.py`, root
precondition). Branch-only failing steps: none. `scripts/dev/ratchet_check.py` accepted the
`UNCLASSIFIED` addition. The gates log directory was removed.

Before the owner decision, on code head `0df334f`, the first command gave 5947 passed, 11
failed: the same 66 ids plus
`tests/sentry/test_reason_registry.py::test_every_engine_exception_family_is_classified_or_listed`
(`new: ['CommitUncertain', 'LeafTooLarge', 'LockTimeout', 'LockUnavailable']`). Commit `622e020`
clears it.

Non-root (uid 65534, no emulation), code head `622e020`: the 3.10 interpreter copied out of
`/root` to a temporary directory, `setpriv --reuid=65534 --regid=65534 --clear-groups`,
`PYTHONPATH=src:.venv/lib/python3.10/site-packages`, `PYTHONDONTWRITEBYTECODE=1`, basetemp owned
by that uid (uid and `decoy_engine.__file__` printed first): `python -m pytest
tests/fileaccess/scratch/test_private_creators.py tests/fileaccess/publication -q -p
no:cacheprovider` gave 107 passed (56 F25 creator cases and the 51 slice-directory tests). Copy
removed.

Signature grep (`git grep -l` in `tests/` for `_open_dir_creating`, `_make_stage_dir`,
`_LocalStageDir`, `LocalOutputTxn`, `LocalOutputDir`, `RootedOutputTxn`, `RootedOutputDir`,
`_open_parent`, `_close_children`, `_close_all`, `PRIVATE_LABELS`, `STAGE_PREFIX`, `StageLabel`,
`OutputDir`, `CommitUncertain`, `LockUnavailable`, `LockTimeout`, `LeafTooLarge`, `read_member`,
`sync_chain`, `DURABLE_LABELS`, `_probe_dir`, `_sync_up`): every hit is under
`tests/fileaccess/`, `tests/sentry/`, or is `tests/unit/execution/test_b6b_sink.py` or
`tests/unit/execution/test_isolated_run.py`, all run by the two commands above.

## Owner decision (2026-11-12)

The plan names four new fileaccess errors (5.1: `CommitUncertain`, `LockUnavailable`,
`LockTimeout`, `LeafTooLarge`). As `DecoyError` subclasses without a registry identity they
failed `tests/sentry/test_reason_registry.py::test_every_engine_exception_family_is_classified_or_listed`,
whose `UNCLASSIFIED` list is documented as shrink-only. The plan is silent on this: 4.4 and 8
give the publication errors registered codes, but those codes register in slice a3, and
`reason_adapter.py` is in no slice's file list in plan section 9.

The owner approved four temporary `UNCLASSIFIED` entries, exactly these four classes, added in
commit `622e020` under a comment saying they carry an errno name only and have no code until a3.
Slice a3 must remove them when it registers the publication codes, and therefore a3 also has to
touch `src/decoy_engine/reason_adapter.py` (to map the classes to the codes), which plan section
9 does not list for a3. The shared errno-only behaviour is a plain mixin (`_ErrnoOnly`, not an
exception class), so the sentry's new list is exactly the four plan-named classes.

## Plan differences

- **Harness fix 1 (`b541f34`)**: in `test_ancestor_created_by_other_attempt[*-live_begin]` the
  hook started the other `begin()` before setting its guard, and that `begin()` re-entered the
  hook through its own `mkdir("a")` (recursion). The guard is now set first. The cases were
  red on the start commit for the absent API before reaching the hook; no assertion changed.
- **Harness fix 2 (`b541f34`)**: `stage_unit` marked the unit file in "the" stage; in
  `live_begin` two stages share the parent (the other attempt's, empty, and this one's), so it
  now picks the stage that holds the unit file. No assertion changed.
- **Extra refusal**: `begin("file", ..., label="publication")` raises `FileAccessUnavailable` on
  both policies. The plan only describes a directory stage for this label; the refusal keeps a
  `publication` publish from taking a non-durable file path. It is checked in
  `test_read_member_bounded_and_absent`.
- `test_unreadable_ancestor_fail_closed` is three functions with suffixes `_seeded` (i),
  `_create_under` (ii) and `_foreign_owner` (iii); the vault-chain part of the row is b1.
- Strict refusal in unreadable (i) happens at `begin()` (the strict chain opens every parent);
  ambient (i) ends in `CommitUncertain` with the leaf kept. Both are "never published" per the
  row.
- `read_member` and `sync_chain` raise an `OSError` with the errno only on both policies (the
  strict policy's `FileAccessDenied` is kept for an invalid component name).
- `lock()` creates no parent: an absent parent is `LockUnavailable(ENOENT)`, matching 4.1 E1.
- The ambient walk resolves the leaf's parent with `os.path.realpath` before walking up, so the
  physical chain is synced when the caller path contains links.
- New registry rows (`tests/sentry/file_access_registry.toml`, count 191 to 194):
  `_local._probe_dir` `os.stat`, `_local._sync_up` `os.path.realpath`,
  `LocalOutputTxn._publish_durable` `os.replace`. `_durable.py` is added to `capability_modules`
  in `tests/sentry/file_access_modules.toml` (it passes the capability rules: raw fd-relative
  calls on held descriptors). `test_lock_contention_and_unsupported` carries the `serial` mark
  (it asserts a wall-clock lower bound).
- The plan's `vm-run`/devbox wording is ignored per the brief; tests ran in this container.

## Not run

- The full suite, the perf suite and the docs build.
- `tests/sentry/layout/test_import_layers.py` outside gates (gates ran it in `.venv-layers`: 15
  passed).
- macOS and Windows: the win32 arms of the new methods raise (`LockUnavailable` or
  `FileAccessUnavailable`) and a `publication` publish there keeps main's plain rename; none of it
  was executed.

## V2-LOG

- The ambient walk's `EACCES` relaxation reads the owner by path (`lstat`), a check-then-use on
  an ancestor another uid owns; it only decides where the walk stops, never what is bound.
- `_durable._held_lock` refuses on Windows before opening anything; the `msvcrt` arm is w1.

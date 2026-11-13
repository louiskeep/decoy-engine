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
  `622e020` sentry `UNCLASSIFIED` entries (see the superseded section below; removed in review round 1). Code head for every check
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

Before those entries, on code head `0df334f`, the first command gave 5947 passed, 11
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

## `UNCLASSIFIED` entries (2026-11-12; superseded in review round 1)

The plan names four new fileaccess errors (5.1: `CommitUncertain`, `LockUnavailable`,
`LockTimeout`, `LeafTooLarge`). As `DecoyError` subclasses without a registry identity they
failed `tests/sentry/test_reason_registry.py::test_every_engine_exception_family_is_classified_or_listed`,
whose `UNCLASSIFIED` list is documented as shrink-only. The plan is silent on this: 4.4 and 8
give the publication errors registered codes, but those codes register in slice a3, and
`reason_adapter.py` is in no slice's file list in plan section 9.

Four temporary `UNCLASSIFIED` entries, exactly these four classes, were added in commit
`622e020` on an answer recorded in the orchestrator session, under a comment saying they carry an
errno name only and have no code until a3. The owner has since stated there was no approval for
them. Review round 1 (below) removes the four entries and registers the classes as pending reason
codes in this slice, with the adapter rows in `src/decoy_engine/reason_adapter.py`, so `UNCLASSIFIED`
is back to main's 29 entries. The shared errno-only behaviour is a plain mixin (`_ErrnoOnly`, not
an exception class).

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
- New registry rows (`tests/sentry/file_access_registry.toml`, count 197 to 200; corrected in review round 1 below):
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

## 2026-11-12: directory close faults (merged from a1a, plus a1b's own sites)

`feat/f22-a1a` fixed raw `os.close` calls on ambient directory descriptors (see
`f22-a1a-evidence.md`, same date); merged here in `1d89ae2`. a1b's own code had the same class in
`fileaccess/_local.py`: `_LocalStageDir.create_file` (private create: a close error after
`create_private_child_at` returned leaked the new file descriptor), `_sync_up` and
`LocalOutputTxn._publish_durable` (a close error after the rename turned a bound leaf into
`CommitUncertain`, before it aborted the publish), and `LocalOutputDir.sync_chain`. Red test
`test_durable_publish_survives_directory_close_faults` (`55d3d89`) failed with the injected `EIO`
from `create_file`; fix `816789a` routes those closes through `_output._close`; the test passes.
`_durable.py` and `_output.py` already closed through `_close`. LOC: `_local.py` 579,
`_output.py` 596.

Checks on `816789a`: `.venv/bin/python -m pytest tests/fileaccess tests/sentry
tests/unit/test_fixed_width_writer.py tests/unit/test_quarantine_staging.py -q -p no:cacheprovider
-n 2 -rfE` gave 6000 passed, 10 failed, 56 errors, 7 skipped: only the known
`test_private_creators.py` (56, root) and `test_import_layers.py` (10, no import-linter in
`.venv`). ruff, format (2216 files) and mypy (786 files) clean.

## 2026-11-13: review round 1 fix

Review round 1 on `40f7870e` (both reviewers GO-WITH-FIXES). Commits: `07629a8f` red tests,
`1c3a1f06` fix for items 1 to 3, `b72cc1cc` item 5 (pending codes and the `UNCLASSIFIED`
removal, one commit). Code head for every check below: `b72cc1cc`; the commit after it adds only
this record. Same `.venv` (Python 3.10.20) as above.

### Red first (tests from `07629a8f`, run on `40f7870e`'s code)

Command: `.venv/bin/python -m pytest tests/fileaccess/publication/test_publish_fault_paths.py
tests/fileaccess/publication/test_errno_only_reasons.py -q -p no:cacheprovider -rfEs`, as root
(11 failed, 4 passed, 1 skipped) and as uid 65534 (12 failed, 4 passed).

| Item | Test id | Red reason on `40f7870e` |
|---|---|---|
| 1 | `test_publish_fault_paths.py::test_failed_begin_closes_its_descriptors[mode_0500]` (uid 65534 only; skipped as root, which ignores 0500) | wrong outcome: three strict `begin(label="publication")` into a 0500 parent raised `EACCES` and left 9 more open fds (1 parent + 2 chain dups each) |
| 1 | `...[mkdir_enospc]` | wrong outcome: `ENOSPC` from `make_stage_dir_at`, 9 fds leaked over three attempts |
| 1 | `...[dup_emfile]` | wrong outcome: `EMFILE` on the second chain dup, 6 fds leaked (parent + first dup each) |
| 2 | `test_publish_fault_paths.py::test_sync_chain_probe_error_is_value_free` | wrong outcome: `sync_chain()` raised the probe's `FileNotFoundError` unchanged, `"/SECRET"` in `str(exc)` |
| 3 | `test_publish_fault_paths.py::test_publication_leaf_limit[244-strict]`, `[244-ambient]`, `[244_multibyte-strict]`, `[244_multibyte-ambient]` | wrong outcome: `lock()` raised `LockUnavailable(ENAMETOOLONG)` after opening the parent, not a denial before any I/O |
| 5 | `test_errno_only_reasons.py::test_errno_only_error_has_a_registered_reason[CommitUncertain]`, `[LockTimeout]`, `[LockUnavailable]` | absent codes: `reasons.find("decoy", ...)` is `None` |
| 5 | `...[LeafTooLarge]` | wrong outcome: maps to `decoy.unexpected_error`, not `decoy.publication_failed` |

Green at baseline (characterization, each paired with a mutation below):
`test_errno_only_errors_carry_no_chain[strict]`, `[ambient]` (the four classes are already raised
outside any handler) and `test_publication_leaf_limit[243-strict]`, `[243-ambient]` (a 243-byte
leaf already locked and published).

### Fixes

1. `fileaccess/_output.py` `RootedOutputTxn.__init__`: the chain dups and `make_stage_dir_at` run
   in one `try`; on any exception before the finalizer exists, every descriptor this call opened
   (the parent dup and each chain dup made so far) is closed through `_close_all`/`_close`, then
   the exception is re-raised. The nested `_make_dir_core` already removes a directory it made.
2. `fileaccess/_local.py` `_probe_dir`: `_call(os.stat, path, follow_symlinks=False)`, so the
   error keeps only its errno (`filename` is `None`, no cause, no context).
3. `fileaccess/_component.py`: `LOCK_PREFIX`, `LOCK_SUFFIX` and
   `MAX_PUBLICATION_LEAF_BYTES = MAX_COMPONENT_BYTES - len(LOCK_PREFIX) - len(LOCK_SUFFIX)` (243),
   and `validate_publication_leaf(leaf, field_key)` (byte length via `os.fsencode`, raises the
   policy's value-free `deny(field_key)`). `_durable._open_lock_at` builds the lock name from the
   same two constants. Entry points that enforce it, each before any syscall:
   `RootedOutputDir.lock`, `RootedOutputDir.begin` (when `label in DURABLE_LABELS`, before
   `_open_parent`, so no parent is created), `LocalOutputDir.lock` (before the win32 branch) and
   `LocalOutputDir.begin` (when `label in DURABLE_LABELS`, before `LocalOutputTxn` makes parents
   or the stage). `read_member` and `sync_chain` do not check it: they read a bound leaf and take
   no lock. `docs/publication.md` names the limit, with the two lock sentences (NFS, fork).
5. `reasons/decoy/_core.py`: `PUBLICATION_LOCK_UNAVAILABLE`, `PUBLICATION_LOCK_TIMEOUT`,
   `PUBLICATION_COMMIT_UNCERTAIN`, each `since=UNRELEASED`, byte-identical to the `_p(...)` blocks
   (and the header comment and `_p` declarer) on `origin/feat/f22-a3`; `reason_adapter._FIXED`
   gains the four rows exactly as a3 has them (`LeafTooLarge` to `decoy.publication_failed`);
   the four `UNCLASSIFIED` entries are removed in the same commit (33 to 29, equal to
   `origin/main` and `a3cb996f`). The slice fragment `changelog.d/f22-a1b-durable-publish.md`
   names `decoy/publication_commit_uncertain`, `decoy/publication_lock_timeout` and
   `decoy/publication_lock_unavailable`; `docs/reasons.md` states them as pending. No edit to
   `REGISTRY_VERSION`, a minor fixture or `consumer-contract.json`. A second fragment naming the
   same identity is allowed: `reason_manifest_policy.pending_fragment_violations` collects the
   claims into a set and `stamp_release.parse_fragment` reads one fragment at a time, so a3's
   `f22-a3-publish-unit.md` keeps its list.
4. Registry count: the `file_access_registry.toml` pin moved 197 (`a3cb996f`, also
   `origin/main`) to 200 on this branch; the earlier "191 to 194" is corrected above.

`_output.py` is 600 lines (cap 600, no census entry), `_local.py` 584, `_durable.py` 198,
`_component.py` 42.

### Mutations (in a detached worktree at `b72cc1cc`, `PYTHONPATH=<worktree>/src`, each applied
once, `tests/fileaccess/publication` run with `-n 2`, then reverted with `git checkout -- src`;
tree clean after each, no commit carries one)

| Mutation | Tests that went red |
|---|---|
| drop the stage fsync walk, both policies | `test_durable_dir_publish_fsync_order[strict]`, `[ambient]`; `test_durable_publish_faults[strict-file]`, `[strict-stage]`, `[ambient-file]`, `[ambient-stage]`; `test_ancestor_created_by_other_attempt[*]` (6); `test_umask_0477_first_attempt_publishes[strict-publication]`, `[ambient-publication]` (14) |
| chain stops at the parent, both policies | `test_durable_dir_publish_fsync_order[strict]`, `[ambient]`; `test_durable_publish_faults[strict-chain:x]`, `[strict-chain:anchor]`, `[ambient-chain:x]`, `[ambient-chain:anchor]`; `test_ancestor_created_by_other_attempt[*]` (6); `test_unreadable_ancestor_fail_closed_seeded[ambient]`; `test_unreadable_ancestor_fail_closed_foreign_owner` (14) |
| owner check dropped (ambient walk raises on any `EACCES`) | `test_unreadable_ancestor_fail_closed_foreign_owner` |
| item 1: the `except` closes nothing | `test_failed_begin_closes_its_descriptors[mkdir_enospc]`, `[dup_emfile]` |
| item 2: `_probe_dir` back to a raw `os.stat` | `test_sync_chain_probe_error_is_value_free` |
| item 3: `validate_publication_leaf` never denies | `test_publication_leaf_limit[244-strict]`, `[244-ambient]`, `[244_multibyte-strict]`, `[244_multibyte-ambient]` |
| item 3: limit one byte short (242; pairs the 243 characterization) | `test_publication_leaf_limit[243-strict]`, `[243-ambient]` |
| item 3: strict `begin` check removed | `test_publication_leaf_limit[244-strict]`, `[244_multibyte-strict]` |
| item 3: ambient `lock` check removed | `test_publication_leaf_limit[244-ambient]`, `[244_multibyte-ambient]` |
| `LockUnavailable` raised inside the `flock` handler (pairs the no-chain characterization) | `test_errno_only_errors_carry_no_chain[strict]`, `[ambient]` |
| item 5: the four adapter rows removed | `test_errno_only_error_has_a_registered_reason[*]` (4) |

The `mode_0500` cell was not mutated separately as root (it skips there); as uid 65534 it was
red on `40f7870e` and passes on `b72cc1cc`.

### Checks (code head `b72cc1cc`; baseline `a3cb996f` in a detached worktree with
`PYTHONPATH=<worktree>/src`, `decoy_engine.__file__` printed under the worktree)

| Command | Tree | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| `.venv/bin/python -m pytest tests/fileaccess tests/sentry tests/unit/test_reason_registry.py -q -p no:cacheprovider -n 2 -rfE --tb=no` | `a3cb996f` | 6203 | 10 | 87 | 6 | 6306 collected |
| same | `b72cc1cc` | 6258 | 10 | 87 | 7 | 6362 collected |
| `.venv/bin/python -m pytest tests/unit --ignore=tests/unit/execution --ignore=tests/unit/scripts -q -p no:cacheprovider -n 2 -rfE --tb=no` | `a3cb996f` | 13132 | 0 | 0 | 84 | 2 xfailed, 5 xpassed |
| same | `b72cc1cc` | 13132 | 0 | 0 | 84 | 2 xfailed, 5 xpassed |
| uid 65534: `python -m pytest tests/fileaccess/publication tests/fileaccess/scratch/test_private_creators.py -q` (interpreter copied out of `/root`, `setpriv --reuid=65534 --regid=65534 --clear-groups`, `PYTHONPATH=src:.:.venv/lib/python3.10/site-packages`) | `b72cc1cc` | 128 | 0 | 0 | 0 | copy removed after |
| `.venv/bin/ruff check src tests` | `b72cc1cc` | All checks passed | | | | |
| `.venv/bin/ruff format --check src tests` | `b72cc1cc` | 2295 files already formatted | | | | |
| `.venv/bin/mypy src/decoy_engine` | `b72cc1cc` | Success, 806 source files | | | | |
| `.venv/bin/python scripts/dev/check_fragment.py --base origin/main` | `b72cc1cc` | exit 0 | | | | |
| `.venv/bin/python scripts/dev/registry_renumber.py --check --main origin/main` | `b72cc1cc` | exit 2, `main-not-merged` | | | | `origin/main` is `06907ead`, past `a3cb996f`, and is not merged by owner rule |
| `.venv/bin/python scripts/dev/registry_renumber.py --check --main a3cb996f` | `b72cc1cc` | exit 0, `registry consistent` | | | | |

The first command includes the reason sentry (`tests/sentry/test_reason_registry.py`,
`tests/sentry/test_reason_minor_fixtures.py`); the second includes
`tests/unit/test_reason_release_pin.py`. Failing ids, first command: identical sets (97 ids, `diff`
empty): 56 errors in `tests/fileaccess/scratch/test_private_creators.py` and 31 in
`tests/fileaccess/scratch/test_scratch_arena.py` (root precondition), 10 failures in
`tests/sentry/layout/test_import_layers.py` (import-linter not in `.venv`). No new failing id, no
baseline id now passes. Collected +56: the slice's tests against main (40 before this round, 16
added now); skipped +1 is `test_failed_begin_closes_its_descriptors[mode_0500]` under root.
Second command: no failing id on either side; collected count unchanged.

### Not run in this round

`gates.sh`, the `tests/unit/execution tests/integration tests/parity tests/security` command,
`tests/unit/scripts` (disk, per the brief), the full suite, perf, macOS and Windows.

### V2-LOG (from the review, logged, no change)

The review's after-V1 list stands as written in the review comment; of it, only the two lock
sentences were added to `docs/publication.md`, since item 3 edits that page.

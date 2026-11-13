# F22 slice a3: build evidence

Slice a3 of `docs/plans/2026-10-30-f22-publication-replan.md` (revision 11, V1 scope; design 5.3,
classes 2, 3, 7): `publish_unit` for `immutable_directory/v1` with judge and bind under the
destination lock, the D2 chain on `already_committed`, the `_after_judge` hook, outcomes as
receipts, `publish_tables`, and the registry codes.

## Environment

- Container, Linux, effective uid 0. Lock profile `.venv` (built by `scripts/dev/bootstrap.sh`,
  not re-synced in this slice). `.venv/bin/python -V`: Python 3.10.20. PyArrow 24.0.0,
  DuckDB 1.5.4, ruff 0.15.22, mypy 2.3.0.
- Branch `feat/f22-a3`. Slice start commit `634d1333` (a2 head). Main baseline for the two pytest
  commands: `c96e2855` (the standing baseline). `origin/main` moved during this slice to
  `426e6d05` (four commits: CI triggers, a QA checklist doc, derived-expression `if()`, docs build
  warnings; registry still 1.23.0; `git merge-tree` reports a clean merge), so it was not merged;
  `gates.sh --baseline` compares against `origin/main` as fetched, `426e6d05`.
- Commits: `353d1594` red tests; `67ffce1e` production, registry, fixtures, sentry rows and docs.
  Code head for every check below: `67ffce1e`; the commit after it adds only this record.

## Production files

| File | Lines | Change |
|---|---|---|
| `publication/_publish.py` (new) | 341 | `publish_unit`, `publish_tables`, `_after_judge`, judge, D2, receipts |
| `publication/_types.py` | 180 to 235 | `PublicationRefused`, `PublicationFailed`, `raise_for_outcome`; `CommitReceipt.reason_code` checked against the registry (`decoy` namespace) instead of by shape |
| `publication/__init__.py` | 48 to 58 | exports |
| `reasons/decoy/_core.py` | 158 to 268 | thirteen `decoy.publication_*` codes, owner `decoy_engine.publication`, outcome `publication`, `since` 1.25.0 |
| `reasons/_registry.py` | 198 | `REGISTRY_VERSION` 1.23.0 to 1.25.0 |
| `reason_adapter.py` | 446 to 462 | `_CODED`: `PublicationRefused`, `PublicationFailed` (namespace `decoy`); `_FIXED`: `CommitUncertain`, `LockUnavailable`, `LockTimeout` to their publication codes, `LeafTooLarge` to `decoy.publication_failed` |

Six production files: plan 9 row a3 names four, plus `reason_adapter.py` (owner decision
2026-11-12) and `publication/__init__.py` (exports, allowed by the brief). Every module is under
600 lines; no census entry changed.

Registry procedure (`docs/reasons.md`, `scripts/release/reason_manifest_policy.py`), additive
only: the slice builds at the next minor of its base (1.25.0); one pinned fixture for the minor,
the publication minor fixture under `tests/fixtures/f29/` (13 entries, generated from
`reasons.manifest()` with the formatting that reproduces the 1.23 fixture byte for byte);
`tests/fixtures/f29/consumer-contract.json` regenerated with
`python tests/unit/progress/test_consumer_contract.py` (only `registry_version` moves, 27 lines);
`docs/reasons.md` names 1.25.0 and the fixture; `tests/unit/test_reason_registry.py` gains the
pin test. `scripts/dev/registry_renumber.py --check` answers `main-not-merged` (main moved and is
not merged into the stack), so the renumber to the next free minor is the merge-time step the
policy describes.

Sentry and placement rows (reviewed registers, no threshold or census number raised):

- `tests/sentry/test_reason_registry.py`: the four temporary `UNCLASSIFIED` entries
  (`CommitUncertain`, `LockUnavailable`, `LockTimeout`, `LeafTooLarge`) removed, as the owner
  decision requires; no entry added. `ADAPTERS` gains three forwarders with their rationale, the
  documented way to register a forward (`_CodedOutcome.__init__`: `_CTOR`, as every coded family;
  `_publish._receipt`: `_HELPER`, whose callers the scanner then checks pass registry symbols;
  `raise_for_outcome`: re-raises a receipt's code, which `CommitReceipt` accepts only when it is a
  registered `decoy` code).
- `tests/sentry/file_access_registry.toml`: 194 to 196 entries, two honest rows for
  `_publish._measure_one` (`mmap.mmap` and `pyarrow.parquet.ParquetFile` over the descriptor
  `OutputTxn.open_file` returned; no path).
- `docs/methodology-registry.yaml`: `_publish.py` carries a `Pattern:` line (Delta Lake
  idempotent transaction identifiers; PostgreSQL `SyncDataDirectory`) and is registered.
- CODEMAP: the publication row and key-paths row name `_publish.py` (no longer `(target)`).
- `docs/publication.md`: new section "Judge and bind: `publish_unit`" (judge table, exit table,
  the caller-names-the-files contract, `raise_for_outcome`); intro and descriptor sections updated.
- `changelog.d/f22-a3-publish-unit.md`.

## Red first (start commit `634d1333`, tests from `353d1594`)

Detached worktree at `353d1594`, `PYTHONPATH=<worktree>/src`, run from the worktree root;
`decoy_engine.__file__` printed under the worktree. Command:
`.venv/bin/python -m pytest tests/unit/publication/test_publish_unit.py tests/unit/publication/test_publish_two_writers.py tests/unit/publication/test_publish_crash_chain.py tests/unit/publication/test_publication_reasons.py tests/unit/test_reason_registry.py::test_f22_publication_codes_are_registered_with_a_pinned_fixture tests/sentry/test_reason_registry.py::test_every_engine_exception_family_is_classified_or_listed -q -p no:cacheprovider -n 2 -rf --tb=line`.
Result: 71 failed, 0 passed. Worktree removed.

| Test id | Count | Red reason on `634d1333` |
|---|---|---|
| `test_publish_unit.py::test_retry_already_committed[strict,ambient]` | 2 | absent API: `cannot import name 'publish_tables'` |
| `test_publish_unit.py::test_refused_overwrite_survivor[*]` (one table, zero tables; both policies) | 4 | absent API |
| `test_publish_unit.py::test_exit_matrix_publish[*]` (18 exits x 2 policies) | 36 | absent API |
| `test_publish_unit.py::test_marker_corpus_user_surfaces[strict,ambient]` | 2 | absent API |
| `test_publish_two_writers.py::test_two_writers_different_result[*]`, `::test_two_writers_same_result[*]` | 6 | absent API |
| `test_publish_crash_chain.py::test_adopt_syncs_ancestor_chain[*]` (b, a, anchor x 2 policies) | 6 | absent API (the child cannot import `publish_tables`, so it is not SIGKILLed) |
| `test_publication_reasons.py::test_the_publication_codes_are_registered_decoy_publication_reasons` | 1 | wrong outcome: `reasons.find("decoy", "publication_lock_unavailable")` is `None` |
| `test_publication_reasons.py::test_file_access_publication_errors_have_fixed_reasons[*]` | 4 | wrong outcome: each maps to `decoy.unexpected_error` |
| `test_publication_reasons.py::test_raise_for_outcome_maps_each_refusal_to_its_registered_reason[*]` | 5 | absent API: no `raise_for_outcome` |
| `test_publication_reasons.py::test_raise_for_outcome_passes_success_and_raises_commit_uncertain` | 1 | absent API |
| `test_publication_reasons.py::test_a_receipt_code_must_be_a_registered_decoy_reason` | 1 | wrong outcome: the a2 shape check accepts an unregistered code (`DID NOT RAISE`) |
| `test_publication_reasons.py::test_refusal_errors_take_registered_codes_only` | 1 | absent API |
| `tests/unit/test_reason_registry.py::test_f22_publication_codes_are_registered_with_a_pinned_fixture` | 1 | absent codes |
| `tests/sentry/test_reason_registry.py::test_every_engine_exception_family_is_classified_or_listed` | 1 | `new: ['CommitUncertain', 'LeafTooLarge', 'LockTimeout', 'LockUnavailable']` (the four entries removed) |

No test was green at baseline.

## Mutations (each applied once on the code head, run, then reverted with `git checkout -- src tests`)

Runner: `.venv/bin/python -m pytest <files> -q -p no:cacheprovider -n 2 --tb=no`; the tree was
clean after every revert and no commit carries a mutation.

| Mutation (plan source) | Tests that went red |
|---|---|
| drop `out.sync_chain()` on the `already_committed` branch (7.2 retry row) | 10: `test_retry_already_committed[strict,ambient]`, `test_exit_matrix_publish[*-E4a_chain_fsync_fails]` (2), `test_adopt_syncs_ancestor_chain[*]` (6) |
| `sync_chain` stops at the parent, both policies (7.2 adopt row; `_local.py`/`_output.py` changed for the mutation only) | 9: `test_retry_already_committed[strict,ambient]`, `test_exit_matrix_publish[ambient-E4a_chain_fsync_fails]`, `test_adopt_syncs_ancestor_chain[*]` (6) |
| judge before taking the lock (class 3: the destination read moved before `out.lock`) | 6: every `test_two_writers_*` cell; L's receipt is `failed` (its bind hit W's unit), not the judged outcome |
| replace the lock with an `exists()` check (class 7; `nullcontext` plus `is_empty_or_absent`) | 6: every `test_two_writers_*` cell, at the non-vacuity probe (no lock file, no `EWOULDBLOCK`) |
| skip `txn.abort()` when the outcome is `already_committed` (class 2) | 2: `test_exit_matrix_publish[ambient-E4_already_committed]`, `test_retry_already_committed[ambient]`; the strict cells stay green because the strict transaction's `weakref.finalize` discards an unreferenced stage when it is collected (A-4) |
| report `already_committed` whatever the D2 fsync returned (class 3: "issue already_committed before its fsync") | 8: `test_exit_matrix_publish[*-E4a_chain_fsync_fails]` (2), `test_adopt_syncs_ancestor_chain[*]` (6) |
| put the leaf name in the refusal text (class 1; 7.2 marker row) | 2: `test_marker_corpus_user_surfaces[strict,ambient]` |
| add a `decoy` code at `since` 1.25.0 without its fixture entry (class 10) | `tests/sentry/test_reason_minor_fixtures.py::test_live_registry_has_one_complete_fixture_per_minor`, `tests/unit/test_reason_registry.py::test_every_symbol_matches_its_attribute` |
| class 8, test side: the crash child's SIGKILL made a no-op | `test_adopt_syncs_ancestor_chain[*]` (6) red; with the two non-vacuity lines (`code == -SIGKILL`, kill marker) also deleted the same 6 pass on the no-kill build, so those lines are what make the crash cells non-vacuous |

The "judge before lock" mutation was first applied wrongly (it edited the signature of `_locked`
instead of `_judge_and_bind`, so the winner crashed and the cells went red for the wrong
reason); it was corrected and rerun, and the table shows the corrected run.

## Checks (code head `67ffce1e`)

| Command | Tree | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| `.venv/bin/python -m pytest tests/unit/publication tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE --tb=no` | `67ffce1e` | 6145 | 10 | 56 | 7 | 6218 collected |
| same without `tests/unit/publication` (absent on main) | `c96e2855` (worktree) | 5897 | 10 | 56 | 7 | 5970 collected |
| `.venv/bin/python -m pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE --tb=no` | `67ffce1e` | 12230 | 0 | 6 | 2389 | 37 xfailed |
| same | `c96e2855` (worktree) | 12230 | 0 | 6 | 2389 | 37 xfailed |
| `.venv/bin/ruff check src tests` | `67ffce1e` | All checks passed | | | | |
| `.venv/bin/ruff format --check src tests` | `67ffce1e` | 2231 files already formatted | | | | |
| `.venv/bin/mypy src/decoy_engine` | `67ffce1e` | Success, 790 source files | | | | |
| signature-grep files (below) | `67ffce1e` | 1243 | 0 | 0 | 2 | |
| uid 65534 (below) | `67ffce1e` | 283 | 0 | 0 | 0 | |

Baselines ran in a detached worktree with the same `.venv` and `PYTHONPATH=<worktree>/src`;
`decoy_engine.__file__` printed under the worktree.

First command, failing ids: identical sets on head and baseline (`diff` empty), 66 ids: the 56
errors of `tests/fileaccess/scratch/test_private_creators.py` (fixture asserts a non-root uid)
and the 10 failures of `tests/sentry/layout/test_import_layers.py` (import-linter is not in
`.venv`; gates runs it in `.venv-layers`). No new failing id; no baseline id now passes.
Collected 5970 to 6218 (+248): +176 `tests/unit/publication` (107 a2, 69 this slice), +51
`tests/fileaccess/publication` (a1a, a1b), and the sentry difference (per-module parametrizations
over the new modules of a1b, a2 and this slice, one new `test_reason_registry` pin, and main's CI
commits on the baseline side; the a2 record itemizes the a2 part).

Second command, failing ids: identical on both sides (`diff` empty), the 6 errors of
`tests/unit/execution/test_scratch_root_umask.py` (root precondition). Collected count unchanged.

Signature grep (`git grep -l` in `tests/` for `CommitReceipt`, `raise_for_outcome`,
`PublicationRefused`, `PublicationFailed`, `publish_unit`, `publish_tables`,
`registered_families`, `REGISTRY_VERSION`, `CommitUncertain`, `LockUnavailable`, `LockTimeout`,
`LeafTooLarge`, `consumer-contract`, `_CODED`, `_FIXED`): hits under `tests/unit/publication`,
`tests/fileaccess`, `tests/sentry` and `tests/security` are run by the two commands above; the
rest were run as `.venv/bin/python -m pytest tests/unit/progress tests/unit/run_result
tests/unit/safe_output tests/unit/test_reason_registry.py tests/unit/test_reason_release_pin.py
tests/unit/test_registry_renumber.py tests/unit/config -q -p no:cacheprovider -n 2`: 1243 passed,
2 skipped. The `_FIXED` hits in `tests/native` and `tests/physical` are an unrelated identifier
(`..._FIXED` constants), covered by the full-suite run below.

Non-root (uid 65534, no emulation): the 3.10 interpreter copied out of `/root` to a temporary
directory, `setpriv --reuid=65534 --regid=65534 --clear-groups`,
`PYTHONPATH=src:.venv/lib/python3.10/site-packages`, `PYTHONDONTWRITEBYTECODE=1`, basetemp owned
by that uid (uid and `decoy_engine.__file__` printed first): `python -m pytest tests/unit/publication
tests/fileaccess/publication tests/fileaccess/scratch/test_private_creators.py -q -p
no:cacheprovider -n 2` gave 283 passed (176 + 51 + 56), including the 0400-file/0500-directory
`already_committed` cell of `test_retry_already_committed`, which only binds under a non-root
uid. Copy removed.

Slice gate: `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline
tests/unit/publication tests/unit/test_reason_registry.py tests/unit/progress/test_consumer_contract.py`
on the code head (only uncommitted path: this record), exit 0. Branch `env`, `ruff`, `format`,
`mypy`, `sentry` (ratchet check, then 4990 passed, 0 failed, 2 skipped), `layers` (15 passed),
`licenses` (63 passed), `tests` (243 passed) all PASS; baseline not rerun because every branch
step passed; failing ids branch=0, baseline=0, branch-only=0.

Full-suite trigger (this slice changes the registry, a frozen contract):
`GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline tests` on the code head,
baseline `origin/main` `426e6d05`. It ran for the two-hour limit of this session's background
jobs and was stopped during the baseline serial pass, so gates printed no final summary; the
numbers below are from its step logs.

| Pass | Tree | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| branch, parallel (`not serial`, `-n 2 --dist loadfile`) | `67ffce1e` | 40658 | 2 | 6 | 5316 | 39 xfailed, 5 xpassed, 56 min |
| branch, serial | `67ffce1e` | 91 | 0 | 56 | 22 | 46197 deselected |
| baseline, parallel | `426e6d05` | 40538 | 0 | 6 | 5316 | 39 xfailed, 5 xpassed, 50 min |
| baseline, serial | `426e6d05` | not finished (stopped at 46%) | | | | |

Branch steps `env`, `ruff`, `format`, `mypy`, `sentry`, `layers`, `licenses` all exit 0.
Failing ids, parallel pass: the 6 `test_scratch_root_umask.py` errors on both sides (root
precondition). Branch-only, 2:

- `tests/unit/test_fixed_width_writer.py::test_fsync_dir_survives_close_error[fsync_and_close_fail-ambient]`
- `tests/unit/test_quarantine_staging.py::TestFdopenFailureDoesNotLeakRawFd::test_fdopen_failure_closes_the_raw_mkstemp_fd`

Neither is from this slice: both fail deterministically when run alone on the a2 head
`634d1333` and on the a1a head `126fa464`, and pass on the stack base `9878d17e` (each run in a
detached worktree, `PYTHONPATH=<worktree>/src`). Both patch `os.close`, and a1a's ambient
`_ensure_dirs` now opens and closes the parent directory descriptor
(`fileaccess/_local.py` `_ensure_dirs`: `os.close(_open_dir_creating(directory))`), so the first
sees its injected close error raised from the parent close and the second counts three closes
instead of one. They sit outside the a1a and a1b check commands (`tests/unit/` top level), which
is why those records do not list them. Reported, not fixed here (outside this slice's files).
Branch serial pass failing ids: the 56 `test_private_creators.py` root-precondition errors, the
same set as the first pytest command. Collected count: branch 46,026 in the parallel pass versus
baseline 45,904 (+122: this stack's new tests minus main's `426e6d05` additions; not itemized).
The 353 `/tmp/gates.*` and `/tmp/gt.*` directories this run and its own test step made were removed.

## Plan differences and decisions

- **Who names the unit's files.** Plan 5.3's pseudocode has `measure(txn)`, but the `OutputTxn`
  protocol has no listing method and a3's files do not include `fileaccess`. `publish_unit` takes
  the table names (`tables=[...]`, each a file directly in the stage) and measures every one itself
  through `txn.open_file` (size, SHA-256, Parquet footer row count): the numbers never come from
  the producer, the names do. A file in the stage that is not named is bound without being
  described; every V1 engine caller stages only what it names (`publish_tables` here; the sink in
  c1; c2's parent walks the stage itself and refuses extras per plan 5.7). A planted
  `_decoy_publication.json` is refused (the descriptor write is `O_EXCL`: E10). Documented in
  `docs/publication.md`. A stage listing on `OutputTxn` would close the gap structurally; see
  V2-LOG.
- **Outcomes are receipts.** Following the 5.3 pseudocode and 1.4 ("outcomes as receipts, not raw
  `OSError`"), every 4.1 exit returns a `CommitReceipt`, including lock refusals and `failed`;
  `raise_for_outcome(receipt)` gives the typed errors of 4.5 (`PublicationRefused`,
  `PublicationFailed`, the file-access `CommitUncertain`) for a caller that wants an exception.
  Interrupts and programming errors (`TypeError`, an invalid table list) propagate; the stage is
  aborted in every case.
- **Registry codes.** All thirteen plan-8 codes register here, because a3 is the only slice whose
  files include the catalog (b1, b2, c1a and c1b list none), so the later slices can use them
  without a second registry bump. Kind `failure`, outcome `publication`, all phases (as the
  existing `publication_failed`); retry classes: lock unavailable and target occupied
  `environment_repair`; lock timeout, commit uncertain and the three in-progress codes
  `retry_operation`; commit not visible `restart_attempt`; operation conflict, vault mismatch,
  vault conflict, attempt sealed and aborted `never`. `failed` receipts reuse the existing
  `decoy.publication_failed`. `LeafTooLarge` maps to `decoy.publication_failed` (no plan code
  fits a bare over-the-cap read; the publisher turns its own oversized descriptor read into
  `publication_target_occupied` first, and b1 maps the vault one to `publication_vault_conflict`).
- **Vault in a3.** A request with `vault` set raises `NotImplementedError` before anything is
  staged or locked: binding output without its vault would break rule V, and vault ordering is
  b1/b2. The LOW-1 presence check that a3 can reach is built: a bound unit whose descriptor names
  a vault, judged equal by `result_id` for a request without one, is refused
  `publication_vault_mismatch` (exit E7, presence half).
- **E3 detail.** "Unreadable" includes a leaf that is a regular file or a symlink, a descriptor
  that is a directory or FIFO, an `EACCES`, an over-1-MiB descriptor (`LeafTooLarge`, `os_error`
  `EFBIG`) and a strict-policy denial on the leaf: all `publication_target_occupied`, `os_error`
  set where an OS call failed. Every a2 refusal (`ValueError("descriptor_refused:<check>")`) maps
  to `publication_target_occupied`; the `ValueError` is caught only around `decode_descriptor`.
- **Two-writer ordering.** As 7.1 prescribes, plus one event: W leaves the hook only after
  `l_probed` and `l_waiting`, which a spy on L's `flock` sets at L's first refused attempt inside
  `publish_unit`. Without it W could bind before L reached its judge, and the "judge before lock"
  mutation would not be red on every run. Still ordered by events, never by timing.
- **Receipt fields** for non-bound exits: `result_id` and `artifacts` are this attempt's measured
  result (`None`/empty when nothing was measured: lock refusals, `failed` before measuring);
  `descriptor_digest` only when a descriptor is bound; for `already_committed`,
  `commit_uncertain` and the vault mismatch they describe the bound unit.
- **Defect found while building**: the first measurement read the Parquet footer through a
  `pa.py_buffer` over the hashing `mmap`; on a non-Parquet file the `ArrowInvalid` traceback held
  the exported buffer and closing the map raised `BufferError`, escaping as an unexpected
  exception. The footer is now read through a separate file object over a `dup` of the
  descriptor, and the exit matrix gained `E10_staged_table_not_parquet` (`failed`, nothing bound)
before the red-test commit.
- **Hashing by `mmap`.** `hashlib`'s incremental `update` is a GCS-named call for
  `tests/sentry/test_outbound_clients.py` (shrink-only baseline); the file is hashed in one call
  over a read-only map instead, which also keeps a large table out of the heap.
- The plan's `vm-run`/devbox wording is ignored per the brief; tests ran in this container.

## Not run

- The perf suite and the docs build.
- The baseline serial pass of the full-suite gates run (stopped at the two-hour limit, above).
- `tests/sentry/layout/test_import_layers.py` outside gates (gates runs it in `.venv-layers`).
- macOS and Windows: `publish_unit` adds no platform branch of its own (it calls the a1b
  capability methods, whose win32 arms refuse); the `fcntl`-based tests skip on win32. Nothing
  ran on macOS.

## V2-LOG

- `OutputTxn` has no listing method, so `publish_unit` measures the files its caller names and
  cannot itself refuse an unnamed extra file in the stage (class 6 is closed for the isolated
  route by c2's parent walk). A `StageDir.entries()` capability method would let the publisher
  measure the whole stage.
- On the strict policy an unaborted transaction's stage is removed when the transaction is
  collected (`weakref.finalize`), so the "skip abort on `already_committed`" mutation is caught
  only by the ambient cells.
- `CommitReceipt` reports this attempt's measured `result_id` on refusals but not the bound unit's;
  a consumer that wants the bound digest reads the descriptor.

## 2026-11-12: the two branch-only ids, fixed below a3

The two branch-only ids of the full-suite run above came from a1a: raw `os.close` on ambient
directory descriptors in `fileaccess/_local.py` (a close error failed the write, masked the primary
error and double-closed a descriptor), plus the same class in a1b's durable path (a close error
after the rename raised `CommitUncertain`). Fixed on `feat/f22-a1a` (`760e53a`, test spy
`d767bde`) and `feat/f22-a1b` (`816789a`), cause and red tests in `f22-a1a-evidence.md` and
`f22-a1b-evidence.md`; merged forward into a2 and here (`0a8e1dc`), no conflicts past a1b.

Checks on `0a8e1dc`, baselines in a detached worktree of `origin/main` as fetched, `4bb9506`
(import confirmed under the worktree; `tests/unit/publication` dropped there, absent on main):

| Command | Tree | Result | Failing ids |
|---|---|---|---|
| `.venv/bin/python -m pytest tests/unit/publication tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE --tb=no` | `0a8e1dc` | 6151 passed, 10 failed, 56 errors, 7 skipped | 56 `test_private_creators.py` (root), 10 `test_import_layers.py` |
| same | `4bb9506` | 6008 passed, 12 failed, 56 errors, 7 skipped | the same 66, plus 2 baseline-only `tests/sentry/test_license_policy.py` ids (`test_notice_matches_installed_metadata`, `test_wrong_license_in_notice_is_caught_against_installed_metadata`: main's newer commits against this `.venv`) |
| `.venv/bin/python -m pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE --tb=no` | both | 12230 passed, 6 errors, 2389 skipped, 37 xfailed | the 6 `test_scratch_root_umask.py` (root) on both; diff empty |
| `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider -n 2 --ignore=tests/unit/execution -rfE --tb=no` | `0a8e1dc` | 12876 passed, 94 skipped, 2 xfailed, 5 xpassed | none |
| same | `4bb9506` | 12994 passed, 92 skipped, 2 xfailed, 5 xpassed | none |
| ruff check, ruff format --check (2232 files), mypy (790 files) | `0a8e1dc` | clean | |
| `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline tests/unit/publication tests/unit/test_reason_registry.py tests/unit/progress/test_consumer_contract.py tests/fileaccess/publication tests/unit/test_fixed_width_writer.py tests/unit/test_quarantine_staging.py` | `0a8e1dc` | exit 0; env, ruff, format, mypy, sentry (4991 passed), layers (15), licenses (63), tests (345 passed) PASS | branch=0 |

No new failing id. The two ids above now pass. The `tests/unit` collected counts differ (12977 vs
13093) because `origin/main` gained unit tests in commits the stack has not merged; both sides
have no failures. `_local.py` 579 LOC, `_output.py` 596. Not run: the full suite, the perf suite,
the uid 65534 pass.

V2-LOG: `publication/_publish.py::_measure_one` closes the staged file descriptor with a raw
`os.close` in a `finally`; a close error there would replace a measurement error or fail a good
measurement. It is a read-only regular file, not a directory, and was left as is.
Registry renumbered at merge from 1.24.0 to 1.25.0 by scripts/dev/registry_renumber.py; main was at 1.24.0.

## Merge of main (2026-11-12)

`origin/main` `4bb95063a68c55f229bd6e1ba88fbe2511f9a4aa` merged bottom-up with real merge commits:
a1a `2201c34c` (clean), a1b `96e8331b` (merges a1a, clean), a2 `dbebb07c` (merges a1b), a3
`cabff5ee` (merges a2). Conflicts: in a2, `.importlinter` and the `LAYERS` snapshot in
`tests/sentry/layout/test_import_layers.py` (main added `support` to tier 1, a2 added
`publication` to tier 2; both kept). In a3, `docs/reasons.md` and
`tests/unit/test_reason_registry.py` (main's FPE fallback minor and the publication minor claimed
the same version; both paragraphs and both tests kept). Then `scripts/dev/registry_renumber.py`
moved the publication codes to the next free minor, 1.25.0: it renamed the pinned fixture to
the publication minor fixture under `tests/fixtures/f29/`, rewrote `since`,
`REGISTRY_VERSION`, the `docs/reasons.md` sentence and the a3 test, and regenerated
`tests/fixtures/f29/consumer-contract.json`. The tool does not rewrite `changelog.d/`, so the
fragment's version moved by hand first, in its own commit; the test's `(1, 25, 0)` tuple floor
also moved by hand. Main's reason codes, fixture and fragment are byte-identical to main.
`registry_renumber.py --check`: registry consistent.

Environment after `bash scripts/dev/bootstrap.sh` (main changed `uv.lock`): Python 3.10.20,
PyArrow 24.0.0, DuckDB 1.5.4, polars 1.42.0, pandas 2.3.3; `--check` OK.

| command | head | result | failing ids |
|---|---|---|---|
| `.venv/bin/python -m pytest tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE` | main `4bb9506` | 6011 passed, 10 failed, 56 errors, 6 skipped | 66 known root/layers ids |
| same | a1a `2201c34` | 6035 passed, 10 failed, 56 errors, 6 skipped | same 66; diff empty |
| same | a1b `96e8331` | 6075 passed, 10 failed, 56 errors, 6 skipped | same 66; diff empty |
| same plus `tests/unit/publication` | a2 `dbebb07` | 6197 passed, 10 failed, 56 errors, 6 skipped | same 66; diff empty |
| same plus `tests/unit/publication` | a3 `8e28cd6` | 6272 passed, 10 failed, 56 errors, 6 skipped | same 66; diff empty |
| `.venv/bin/python -m pytest tests/unit/test_fixed_width_writer.py tests/unit/test_quarantine_staging.py -q -p no:cacheprovider` | a1a `2201c34` | 46 passed | none |
| renumber `--print-tests` set plus `tests/unit/publication tests/unit/test_registry_renumber.py`, `-n 2` | a3 `0ff3083` | 431 passed | none |
| `.venv/bin/python -m pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE` | a3 `8e28cd6` | 12230 passed, 6 errors, 2389 skipped, 37 xfailed | the 6 `test_scratch_root_umask.py` (root) |
| same | main `4bb9506` | 12229 passed, 1 failed, 6 errors, 2389 skipped, 37 xfailed | the same 6, plus `test_byte_estimate_routing.py::TestEndToEndWiring::test_width_change_flips_route_at_a_fixed_budget_and_row_count` (pytest-timeout 300s, main only) |
| `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider -n 2 --ignore=tests/unit/execution -rfE` | a3 `8e28cd6` | 13223 passed, 84 skipped, 2 xfailed, 5 xpassed | none |
| same | main `4bb9506` | 13046 passed, 84 skipped, 2 xfailed, 5 xpassed | none |
| ruff check, ruff format --check (2258 files), mypy (795 files) | each head | clean | |
| `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline tests/unit/publication tests/fileaccess/publication tests/unit/test_reason_registry.py` | a3 `8e28cd6` | ruff, format, mypy, sentry (5111 passed, 2 skipped), layers (15), licenses (63), tests (298) PASS; env FAIL on the stale editable install of the project itself after branch switches | branch=0 |
| `bash scripts/dev/bootstrap.sh`, then `gates.sh --keep-going --baseline --steps env` | a3 `8e28cd6` | env PASS | branch=0 |

No branch-only failing id. The one main-only id is a timeout on main. Collected counts rise only
by the stack's own tests. `_output.py` 596 LOC, `_local.py` 579. Not run: the full suite, the
perf suite, the uid 65534 pass.

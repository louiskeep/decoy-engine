# F22 slice c1a: build evidence

Slice c1a of `docs/plans/2026-10-30-f22-publication-replan.md` (revision 11, V1 scope; design 5.6,
classes 3 and 9): the sink state table and its transition cells, `StagedParquetWriter` split out
of the sink's write half, and the characterization tests.

## Environment

- Container, Linux, effective uid 0. Lock profile `.venv` (`bash scripts/dev/bootstrap.sh --check`:
  OK at the start; not re-synced). `.venv/bin/python -V`: Python 3.10.20. PyArrow 24.0.0,
  DuckDB 1.5.4, ruff 0.15.22, mypy 2.3.0.
- Branch `feat/f22-c1a`. Slice start commit `f43d8df9` (a3 head). Main baseline for failure sets:
  `origin/main` `4bb95063` (detached worktree, `PYTHONPATH=<worktree>/src`, `decoy_engine.__file__`
  printed under the worktree, worktree removed after). `origin/main` has since moved to
  `105291d8`; it was not merged.
- Commits: `f28e8a79` characterization pin (green at the start commit); `3056f013` red tests (and
  the deliberate P6 update); `77d60d11` production, sentry row, docs; `5061bdf0` keeps the
  validator's original name `_validate_table_name` (the file-access scan in
  `tests/sentry/fa/scan.py` recognizes it by that name); `8aeb5287` the deliberate E1-0 update
  (below). Code head for every check below: `8aeb5287` unless stated (`src/` is identical at
  `5061bdf0` and `8aeb5287`).

## Production files

| File | Lines | Change |
|---|---|---|
| `publication/_sink_state.py` (new) | 246 | `SinkState`, `Act`, `Exit`, the literal `ARRIVALS` (36 cells) and `EXITS` (11 cells) tables, `SinkStateMachine` (one `RLock`, transitions under it, no I/O, pending abort applied on the exit, refusals raised outside the lock as `PublicationRefused` with a value-free receipt) |
| `execution/_transactional_sink.py` | 438 to 519 | `StagedParquetWriter` (the write half: `write`, `write_batches`, replace-on-rewrite, stored size and progress, failure evidence) split out unchanged in behaviour; `ParquetTransactionalSink` composes it with a `SinkStateMachine` (fresh uuid4 `operation_id`), adds `state`, and moves `write`, `write_batches`, `commit`, `abort` through the table; the commit itself is unchanged (`table_sink` stage, plain rename) |

Two production files, as plan 9 lists. `publication/__init__.py` is not touched (tests import from
`_sink_state` directly; its docstring still says the sink state arrives later, see V2-LOG). No
census entry changed; `_transactional_sink.py` is 519 (cap 600), `_sink_state.py` 246.

Registers and docs (no threshold, census number or pin raised):

- `tests/sentry/test_reason_registry.py`: one `ADAPTERS` row,
  `("publication/_sink_state.py", "SinkStateMachine._refuse")`, with its rationale (raises the code
  of a literal cell, a registry symbol, or the one the attempt ended with; its `CommitReceipt`
  accepts only a registered `decoy` code). The cells themselves use `reasons.decoy.*` symbols.
  This is the documented way to register a forward (as a3 did for `_publish._receipt`); no
  exception family was added, so `test_every_engine_exception_family_is_classified_or_listed`
  is unchanged.
- `docs/methodology-registry.yaml`: `_sink_state.py` (table-driven finite-state machine; UML and
  W3C SCXML state tables; `java.util.concurrent.FutureTask`).
- CODEMAP: the publication rows name `_sink_state.py` (no longer `(target)`), the sink row names
  `StagedParquetWriter` and the state table, and a placement row for
  `tests/unit/execution/publication/` (plan 6.1). `src/decoy_engine/_MAP.md` publication row.
- `docs/publication.md`: new section "The reference sink's state table" (both tables, what the
  sink does today, what it does not claim yet); intro sentence updated.
- `changelog.d/f22-c1a-sink-state.md`.

## Red first

Characterization pin `f28e8a79` on the start source: `tests/unit/execution/publication/test_sink_characterization.py`,
24 passed (P1 to P6 and the write-half pins, both policies).

Red commit `3056f013` (its `src/` is byte-identical to `f43d8df9`), detached worktree,
`PYTHONPATH=<worktree>/src`, `decoy_engine.__file__` under the worktree. Command:
`.venv/bin/python -m pytest tests/unit/publication/test_sink_state.py tests/unit/execution/publication -q -p no:cacheprovider -n 2 -rfE --tb=line`.
Result: 2 failed, 22 passed, 2 collection errors. Worktree removed.

| Test id | Count | Red reason on `f43d8df9` |
|---|---|---|
| `tests/unit/publication/test_sink_state.py` (all `test_sink_transition_cells_*`: 36 arrival cells, 22 exit cells, table completeness, settled-state leave, invalid `operation_id`, concurrent arrivals) | 62 | absent API: `ModuleNotFoundError: decoy_engine.publication._sink_state` at collection |
| `tests/unit/execution/publication/test_sink_transition_cells.py` (sealed, sealed no-I/O, writing, writing ok, committing, committing failure with pending abort, failed commit, failed write, open abort, invalid name; both policies) | 20 | absent API: the same import, at collection |
| `test_sink_characterization.py::test_p6_after_a_commit_the_sink_is_sealed[ambient,strict]` | 2 | wrong outcome (probe P6): the write after a commit is accepted into a new stage, `DID NOT RAISE PublicationRefused` |

The other 22 characterization ids are green at the start commit by design; each is paired with a
mutation below.

## Mutations (each applied once, run, then reverted with `git checkout -- src`; tree clean after)

Run on `77d60d11` (the `5061bdf0` rename does not touch any mutated line except mutation 8's text,
which was applied to the pre-rename name). Runner: `.venv/bin/python -m pytest
tests/unit/publication/test_sink_state.py tests/unit/execution/publication -q -p no:cacheprovider
-n 2 --tb=no` (106 tests).

| # | Mutation (plan source) | Red |
|---|---|---|
| 0 | allow `write` in SEALED (plan 3, class 3) | 6: `test_sink_transition_cells_arrival[SEALED-write]`, `..._tables_are_complete_and_literal`, `test_p6_*[ambient,strict]`, `test_sink_transition_cells_sealed[ambient,strict]` |
| 1 | hold the transition lock across the I/O (7.2 "no I/O under the lock") | `test_sink_transition_cells_exit[False-WRITING-OK]` (`lock_free_during_io` false); run on that one cell only, since every other in-flight cell then deadlocks until the pytest timeout |
| 2 | drop the pending abort on the exit | 11: `arrival[WRITING-abort]`, `exit[True-*]` for every exit landing outside SEALED/UNCERTAIN (6), sink `writing[*]` (2), `committing_failure_with_a_pending_abort[*]` (2) |
| 3 | apply a pending abort even when the exit seals | 9: `arrival[COMMITTING-abort]`, `arrival[RECOVERING-abort]`, `exit[True-*]` landing SEALED/UNCERTAIN (5), sink `committing[*]` (2) |
| 4 | a sealed `commit()` runs the commit again | 10: `arrival[SEALED-commit]`, `exit[*]` sealing cells (6), table completeness, `sealed_commit_does_no_io[*]` (2) |
| 5 | an in-flight write is accepted instead of busy | 5: `arrival[WRITING-write]`, table completeness, concurrent arrivals, sink `writing[*]` (2) |
| 6 | a failed write leaves the sink open | 2: `failed_write[*]`; and, on `8aeb5287`, `tests/fileaccess/test_char_sink_publish.py::test_nul_and_overlong_names_fail_at_write_not_at_validation` |
| 7 | validate the table name after the state moves | 2: `an_invalid_name_is_refused_before_the_state_moves[*]` |
| 8 | abort in OPEN leaves the sink open | 8: `arrival[OPEN-abort]`, `arrival[ABORTED-*]` (4), table completeness, `open_abort[*]` (2) |
| 9 | a REFUSED/FAILED refusal forgets the code the attempt ended with | 6: `arrival[REFUSED-*]` (3), `exit[False-*]` landing REFUSED (3) |
| 10 | write half: a rewrite does not replace the staged file (class 9) | 2: `test_a_table_written_twice_replaces_its_staged_file[*]` |
| 11 | write half: stored size not recorded (class 9) | 2: `test_stored_bytes_are_the_closed_files_size_for_both_write_paths[*]` |
| 12 | write half: a failing close is recorded as a write (class 9) | 2: `test_a_failing_stream_records_the_write_boundary_and_a_failing_close_the_close[*]` |
| 13 | P3/P5: stage under the durable `publication` label | 6: `test_p3_p5_*[*]` (2) and four sink cells |
| 14 | P4: an empty commit does not create the target | 4: `test_p4_*[*]`, `sealed_commit_does_no_io[*]` |
| 15 | P1/P2: a failed commit rename is swallowed | 8: `test_p1_*[*]`, `test_p2_*[*]`, `failed_commit[*]`, `committing_failure_with_a_pending_abort[*]` |
| 16 | drop table-name validation (class 9) | 10: `test_a_table_name_must_be_one_component[*]` (8), `an_invalid_name_is_refused_before_the_state_moves[*]` (2) |

Plan 3's other class-3 mutations (judge before lock, adopted `VaultCommit` before fsync, chain
stops, skip writer compare, seal in `reconcile()` on `operation_id`) belong to a3, b1, b2 and c1b.
Class 9's named mutation (move a function out of `_isolated_run.py` without the census) is c2's
module; this slice's class-9 pairs are 10 to 16. The class-8 "delete a non-vacuity line" check
was not run for this slice's tests.

## Checks

| Command | Tree | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| `.venv/bin/python -m pytest tests/unit/publication tests/fileaccess tests/sentry -q -p no:cacheprovider -n 2 -rfE --tb=no` | `8aeb5287` | 6339 | 10 | 56 | 6 | |
| `.venv/bin/python -m pytest tests/fileaccess tests/sentry ...` (no `tests/unit/publication` on main) | `4bb95063` | 6011 | 10 | 56 | 6 | |
| `.venv/bin/python -m pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 2 -rfE --tb=no` | `5061bdf0` (same `src/`) | 12274 | 0 | 6 | 2389 | 37 xfailed |
| same | `4bb95063` | 12230 | 0 | 6 | 2389 | 37 xfailed |
| `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider -n 2 --ignore=tests/unit/execution -rfE --tb=no` | `5061bdf0` (same `src/`) | 13285 | 0 | 0 | 84 | 2 xfailed, 5 xpassed |
| same | `4bb95063` | 13046 | 0 | 0 | 84 | 2 xfailed, 5 xpassed |
| `.venv/bin/ruff check src tests`; `ruff format --check src tests`; `mypy src/decoy_engine` | `8aeb5287` | clean; 2264 files formatted; 796 source files | | | | |
| signature-grep files outside the commands (below) | `8aeb5287` | 108 | 0 | 0 | 2 | |

First command, failing ids: identical to the baseline (`diff` empty), 66 ids: the 56 errors of
`tests/fileaccess/scratch/test_private_creators.py` (root) and the 10 failures of
`tests/sentry/layout/test_import_layers.py` (import-linter runs in `.venv-layers` under gates).
On `5061bdf0` the same command had one more id,
`tests/fileaccess/test_char_sink_publish.py::test_nul_and_overlong_names_fail_at_write_not_at_validation`
(see "Plan differences": a failed write now ends the attempt); `8aeb5287` updates that cell and
pairs it with mutation 6. Collected 6083 to 6411 (+328): `tests/unit/publication` (absent on main:
the stack's 176 plus this slice's 62), the stack's `tests/fileaccess/publication` (51), and sentry
parametrizations over the stack's new modules, this slice's included (not itemized).

Second command, failing ids: identical (`diff` empty), the 6 `test_scratch_root_umask.py` errors
(root). Collected count +44: this slice's `tests/unit/execution/publication` (24 + 20).

Third command: no failing id on either side. Collected 13137 to 13376 (+239): `tests/unit/publication`
(the stack's 176 plus this slice's 62) and the stack's reason-registry pin.

Signature grep (`git grep -l` in `tests/` for `ParquetTransactionalSink`, `_validate_table_name`,
`_replace_staged`, `_stored`, `StagedParquetWriter`, `SinkState`, `_sink_state`): hits under
`tests/unit`, `tests/fileaccess`, `tests/integration`, `tests/parity` and `tests/sentry` are run by the
commands above; the rest were run as `.venv/bin/python -m pytest
tests/acceptance/test_f06_route_parity.py tests/physical/test_characterization_out_of_core.py
tests/physical/test_characterization_sequential.py tests/physical/test_shadow_disconnection.py
tests/physical/test_unified_slice_admission.py tests/physical/test_unified_slice_isolated_worker.py
-q -p no:cacheprovider -n 2`: 108 passed, 2 skipped. The four `tests/perf` hits are the perf suite
(not run). `tests/sentry/baselines/gcs-call-kwargs.json` is data read by a sentry in the first
command.

Slice gate: `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline
tests/unit/publication/test_sink_state.py tests/unit/execution/publication
tests/fileaccess/test_char_sink_publish.py tests/sentry/test_reason_registry.py` on `8aeb5287` (only
uncommitted path: this record), exit 0. Branch `env`, `ruff`, `format`, `mypy`, `sentry` (ratchet
check, then 5117 passed, 2 skipped), `layers` (15), `licenses` (63), `tests` (184 passed) all
PASS; baseline not rerun because every branch step passed; failing ids branch=0, baseline=0,
branch-only=0. Its `/tmp/gates.*` and `/tmp/gt.*` directories were removed.

## Plan differences and decisions

- **Scope of the commit.** Plan 5.6's opening line says the sink's commit calls `publish_unit`,
  but plan 9 gives "restart through `commit()`", the evidence projection and route wiring to c1b,
  and the brief says not to build them. c1a therefore keeps the commit's plain `table_sink`
  rename (probes P1 to P5 stay pinned and green) and puts every call through the state table;
  only P6 changes. The table is complete (all 36 arrival and 11 exit cells, the RECOVERING row
  included) and tested on the machine; the sink reaches open, writing, committing, sealed,
  failed and aborted today. The sink has no `reconcile()` yet and its sealed result is `None`
  (no receipt until the commit goes through `publish_unit`). An earlier probe (commit through
  `publish_unit`, not committed) broke 423 tests in the second command, almost all directory
  listings that now include `_decoy_publication.json`; c1b should budget for that.
- **Refusal codes the plan leaves open.** Plan 5.6 says only "refuse" for UNCERTAIN `write` and for
  every REFUSED and FAILED call. Chosen: UNCERTAIN `write` is `publication_commit_uncertain` (as
  its `commit`); REFUSED and FAILED refuse with the code the attempt ended with (the refusal's
  code, `publication_commit_not_visible` after RECOVERING, `publication_failed` after a failed
  write or commit). Busy and refused calls raise `PublicationRefused` with a receipt
  (`outcome="refused"`, the sink's `operation_id`, no digests, no artifacts), so no new exception
  family was needed.
- **Failed writes end the attempt** (plan 5.6 "WRITING, exception: FAILED"). Before, a sink took
  further writes after a failed one. The E1-0 characterization cell
  `test_nul_and_overlong_names_fail_at_write_not_at_validation` pinned that implicitly (two bad
  names on one sink); it now asserts the second call is refused `publication_failed` and checks
  the overlong name on a fresh sink, so both original assertions are kept. Every engine route
  aborts on a write failure, and the three suites show no other dependency. A table name that is
  not one component is still refused with `ValueError` before the state moves (validated before
  `arrive`), so it does not end the attempt.
- **Abort ends the attempt.** OPEN `abort` is ABORTED (plan table); a sink is not reused after an
  abort by any engine route (the three suites pass).
- **Operation id.** The sink takes a fresh `uuid4().hex` as its `operation_id` (plan 5.6, "with no
  request"); the `request` keyword is c1b's.
- **Two test commits before production.** Plan 7.2 says P1 to P6 are "pinned before c1a changes the
  sink, then deliberately updated in c1a's red commit", so the pin (`f28e8a79`, green at the start)
  precedes the red commit (`3056f013`), which updates P6.
- **Slice size.** Production +419/-92 and tests +890/-2, about 1,400 lines changed against the plan's
  estimate of 535 and its 800-line cap per slice. The overrun is tests: every cell of both tables
  as its own case and every sink cell on both policies. Not cut, since cutting cells would weaken
  the transition proof; reported for the orchestrator.
- The plan's `vm-run`/devbox wording is ignored per the brief; tests ran in this container.

## Not run

- The perf suite (four `tests/perf` sink users) and the full suite.
- The uid 65534 pass: this slice adds no mode or permission behaviour.
- `tests/sentry/layout/test_import_layers.py` outside gates.
- The class-8 non-vacuity deletion check.
- macOS and Windows: the slice adds no platform call (no `sys.platform` branch needed).

## V2-LOG

- `StagedParquetWriter._stored` (moved unchanged) closes the staged file descriptor with a raw
  `os.close` in a `finally`, so a close error there would fail a good write. Kept as it was on main
  to keep the split behaviour-preserving; the fix is `fileaccess/_output.py::_close`.
- `publication/__init__.py`'s docstring still says the sink state table arrives in a later slice
  (outside this slice's files except for exports).
- The machine's `leave` raises `KeyError` for an exit that is not valid from the current state
  (a programming error); the state is left unchanged in that case.

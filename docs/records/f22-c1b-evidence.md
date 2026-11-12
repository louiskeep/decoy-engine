# F22 slice c1b: build evidence

Slice c1b of `docs/plans/2026-10-30-f22-publication-replan.md` (revision 11, V1 scope; design 5.6,
classes 3, 4 and 5): `reconcile` from UNCERTAIN with the D4 chain, restart through `commit()` (the
sink's commit now goes through `publish_unit`), the evidence projection, and the route wiring with
receipts out.

## Environment

- Container, Linux, effective uid 0. Lock profile `.venv` (`bash scripts/dev/bootstrap.sh --check`:
  OK at the start; not re-synced). `.venv/bin/python -V`: Python 3.10.20. PyArrow 24.0.0,
  DuckDB 1.5.4, ruff 0.15.22, mypy 2.3.0.
- Branch `feat/f22-c1b`. Slice start commit `acb2c4aa` (c1a head). Main baseline for failure sets:
  `4bb95063` (the standing baseline, merged into the stack), run in a detached worktree with
  `PYTHONPATH=<worktree>/src` (`decoy_engine.__file__` printed under the worktree; worktree removed
  after). `origin/main` has since moved to `7d2cd2dc`; it was not merged (no conflict reported).
- Commits: `3471a6dc` red tests (and the deliberate updates of the c1a sink cells); `55b6f6cf`
  production, the expected-output updates of existing tests, sentry registers, docs, changelog;
  `b96579ae` the owner's stage-listing ruling (`staged_tables()` on both sinks, the
  descriptor-count test). Code head for every check below: `b96579ae`; the commit after it adds
  only this record.

## Production files

| File | Lines (start to head) | Change |
|---|---|---|
| `execution/_transactional_sink.py` | 519 to 600 | the sink stages under the `publication` label and commits through `publish_unit` with its own full staged set, `ParquetTransactionalSink.staged_tables()` (from `StagedParquetWriter.staged`), the one source of the `tables` argument; `request=` keyword (a request carrying a vault is refused, rule B0); `receipt`; `commit()` returns the receipt for `published`/`already_committed` and raises the typed error otherwise (`raise_for_outcome`), a failed outcome carrying its errno as `__cause__` (an errno-only `OSError`) so F29 still classifies a full disk; `reconcile()` (RECOVERING: read the bound descriptor, this sink's `result_id`, then `sync_chain` as D4); the state projected onto the evidence after every exit; the stage path recorded at `begin` so cleanup is confirmed by looking after `publish_unit` removed the stage; module and class docstrings rewritten (they described the plain rename); `TransactionalSink.commit` annotated `-> object` (typing only: the reference sink returns its receipt) |
| `execution/_publication_observer.py` | 158 to 196 | `SINK_STATE_EVIDENCE` (literal state to `PublicationState`/`CleanupState` table, plan 5.6) and `project_sink_state`; `observed_commit` returns the sink's `CommitReceipt` (the value `commit()` returned, else `sink.receipt`, else `None`) |
| `execution/_adapter.py` | 505 to 509 | `ExecutionResult.publication_receipt` (additive, default `None`) |
| `execution/_sequential.py` | 660 to 658 | receipt from `observed_commit` onto `ExecutionResult`; the residual-window comment shortened (net -2, census rule net <= 0) |
| `execution/out_of_core/_runner.py` | 335 to 339 | receipt onto `ExecutionResult` |
| `execution/_chunked_output_sink.py` | 598 to 600 | `OutputPublish.commit()` returns the receipt; `OutputPublish.staged_tables()` (the streamed sink's own staged set, `[]` while the session is not streaming); module and class docstrings rewrapped to stay at 600 |
| `execution/_pipeline_full_frame.py` | 317 to 317 | the streamed route's result carries the receipt (`dataclasses.replace` after the commit, still the run's last action) |
| `execution/_pipeline_mixed_route.py` | 314 to 314 | the same |
| `generation/_streamed.py` | 594 to 598 | `GenerationResult.publication_receipt` (typed `Any`: `generation` is a tier below `publication` in `.importlinter`, so it may not import `CommitReceipt`) |

Nine of the ten files plan 9 lists for c1b; `_pipeline_context.py` is not touched (see "Plan
differences"). No eleventh module. Census: `_transactional_sink.py` 600 (cap 600),
`_chunked_output_sink.py` 600 (cap 600), `_sequential.py` 658 (net -2), `_runner.py` 339 (cap 600),
`_pipeline_context.py` 594 (unchanged). No census entry, threshold or pin changed. `execution/`
gains no file.

Registers and docs (no threshold, census number or pin raised):

- `tests/sentry/test_reason_registry.py` `ADAPTERS`: two rows with their rationale,
  `ParquetTransactionalSink._leave` (forwards the code of the receipt the attempt ended with to the
  state table; a receipt holds only registered `decoy` codes) and `_recover` (builds the RECOVERING
  refusal from the registry symbol `PUBLICATION_COMMIT_NOT_VISIBLE` as an exact `str`, which
  `CommitReceipt` requires). No exception family was added.
- `tests/sentry/secret_material_census.json`: the two `StrategyContext` field rows moved from lines
  144/149 to 148/153 (the new `ExecutionResult` field sits above them); no row added.
- `tests/sentry/file_access_registry.toml` and `tests/sentry/baselines/gcs-call-kwargs.json`:
  unchanged; the stage existence check stays in `ParquetTransactionalSink._cleanup_staging`.
- CODEMAP: the observer row, the sink row and the `tests/unit/execution/publication/` row.
- `docs/publication.md`: intro; "What the sink does" rewritten; new section "The reference sink on
  `publish_unit`" (commit outcomes table, restart, `reconcile()`, evidence, routes, on-disk
  changes: every unit holds `_decoy_publication.json`, the parent holds `.<leaf>.decoy-lock`, unit
  files `0600` and directories `0700`).
- `changelog.d/f22-c1b-sink-publish-unit.md` (with a `Breaking:` line for the on-disk and exception
  changes).

## Red first

Red commit `3471a6dc` (its `src/` is byte-identical to `acb2c4aa`), detached worktree,
`PYTHONPATH=<worktree>/src`, `decoy_engine.__file__` under the worktree. Command:
`.venv/bin/python -m pytest tests/unit/execution/publication -q -p no:cacheprovider -n 2 -rfE --tb=line`.
Result: 65 failed, 32 passed, 1 collection error. Worktree removed.

| Test id | Count | Red reason on `acb2c4aa` |
|---|---|---|
| `test_restart_recovery.py` (all 12: `test_restart_recovery[*]`, `..._reconcile_seals_an_uncertain_sink_after_d4[*]`, `..._reconcile_refuses_a_unit_that_is_not_its_own[*-*]`, `..._reconcile_eio_reading_the_unit_stays_uncertain[*]`, `test_restart_same_operation_different_result[*]`) | 12 | absent API: `ParquetTransactionalSink.__init__() got an unexpected keyword argument 'request'`; in the crash cell the child dies with that `TypeError` (exit 1, not SIGKILL) |
| `test_route_outcome_matrix.py::test_route_outcome_matrix[*]` (5 routes) | 5 | absent API (`request`) |
| `test_sink_evidence_projection.py` | 1 error | absent API: `cannot import name 'SINK_STATE_EVIDENCE'` at collection |
| `test_fk_unit_all_or_none.py::..._refused[*]` (5 shapes x 2 policies) | 10 | wrong outcome: raw `OSError` 39 (`ENOTEMPTY`), not `PublicationRefused` |
| `test_fk_unit_all_or_none.py::..._commit[*]`, `..._route_commit_and_refused[*]` | 12 | wrong outcome: the unit listing has no `_decoy_publication.json` |
| `test_sink_characterization.py` P1, P2 (`OSError` 39), P3/P5 (no fsync, no descriptor), P4 (no unit), written-twice listing | 10 | wrong outcome (deliberate updates of the c1a pins, named in each docstring) |
| `test_sink_transition_cells.py` sealed (commit returns `None`), sealed-no-I/O and writing-ok and invalid-name listings, refused and failed commit (`OSError`, FAILED, stage kept), refused-with-pending-abort (the judge read never parks: no `read_member` call), writing (no stage under the publication prefix) | 16 | wrong outcome (deliberate updates of the c1a cells) |

Green at the start commit by design (characterization, paired with mutations below): the 10
sink and 2 route SIGKILL-mid-stage cells of `test_fk_unit_all_or_none` (one rename was already
the unit's only bind) and the c1a cells the commit does not touch.

`test_route_outcome_matrix.py::test_descriptor_describes_every_staged_table[*]` (owner ruling,
added in `b96579ae`): run on a detached worktree at `acb2c4aa` with that test file, 5 failed,
absent API (`request`).

## Mutations (each applied once on the code head, run, then reverted; tree clean after)

Runner: `.venv/bin/python -m pytest tests/unit/execution/publication tests/integration/test_f29_failure_contract.py -q -p no:cacheprovider -n 2 --tb=no` (114 tests) unless stated.

| # | Mutation (plan source) | Red |
|---|---|---|
| 1 | seal in `reconcile()` on `operation_id` (plan 3, class 3; 7.2 V1-B row): OPEN `reconcile()` returns the bound unit's receipt when its `operation_id` matches | 4: `test_restart_same_operation_different_result[ambient,strict]`, `test_restart_recovery[ambient,strict]` |
| 2 | bind one rename per table (plan 3, class 5): ambient commit renames each staged file into the target, then publishes the empty stage | 40, including `test_fk_unit_all_or_none_refused[ambient-*]` (5), `..._commit[ambient-*]` (5), `..._route_commit_and_refused[*]` (2), every ambient route-matrix and restart cell |
| 3 | `reconcile()` skips the D4 `sync_chain` (plan 5.0 D4) | 2: `test_restart_recovery_reconcile_seals_an_uncertain_sink_after_d4[ambient,strict]` |
| 4 | the sequential route drops its receipt (class 4) | 1: `test_route_outcome_matrix[sequential]` |
| 5 | UNCERTAIN projected as `published` (plan 5.6 evidence) | 3: `test_every_sink_state_has_one_evidence_row`, `..._reconcile_seals_an_uncertain_sink_after_d4[*]` |
| 6 | the sink names no tables to `publish_unit` (`tables=[]`) | 25 (every different-result cell: refused, conflict, not-visible, route matrix) |
| 7 | a failed commit carries no errno cause (F29) | 1: `test_f29_failure_contract.py::test_a_failed_commit_rename_publishes_nothing_and_says_so` (`decoy.publication_failed` instead of `storage_capacity_exhausted`) |
| 8 | UNCERTAIN accepts a write (plan 3, "allow `write` in" a settled state) | 2: `..._reconcile_seals_an_uncertain_sink_after_d4[*]` |
| 9 | owner ruling: one staged table left out of `tables` (`self.staged_tables()[1:]`); runner `-k test_descriptor_describes` | 5 of 5: `test_descriptor_describes_every_staged_table[sequential,out_of_core,full_frame_streamed,mixed,streamed_generation]` |

Plan 3's other class-3, 4 and 8 mutations belong to a3, b1, b2, c1a and c2. The class-8 "delete a
non-vacuity line" check was not run for this slice's tests.

## Existing test files edited, by reason

Every listing stays an exact equality; nothing is filtered in production; no assertion was turned
into a subset check.

- **The descriptor in an exact unit listing** (plan 8 and Q5, "every engine unit gains
  `_decoy_publication.json`"): `tests/unit/execution/test_b6b_equality.py`,
  `test_b6a_output.py`, `test_b6a_split_worker.py`, `test_b6b_sink.py` (an unsorted `iterdir`
  listing became sorted), `test_b6a_publish.py`, `tests/parity/test_phase_f_f2_mixed_publish.py`,
  `tests/fileaccess/test_char_sink_publish.py`; `tests/unit/execution/r3_failure_trace_baseline.json`
  (`published_files` of `f_ff_streamed_success`, `f_ooc_sink_success`, `f_seq_sink_success`: one
  added line each, nothing else changed; edited by hand, not regenerated; the file says "regenerate
  only with reviewer approval", so this needs the reviewer's eye); `tests/fileaccess/route_corpus.py`
  plus `route_corpus_golden.json` (the corpus read every published file as Parquet; it now records
  the descriptor by its writer-independent fields, schema, unit, artifact path, role and rows, and
  vault, because its sizes and digests carry the Parquet footer's pyarrow version; 140 added
  lines, none removed, the `companion` section mirrored from the regenerated `no_companion` one,
  whose outputs are equal by the file's own contract). The c1a files
  `tests/unit/execution/publication/test_sink_characterization.py` and
  `test_sink_transition_cells.py` (listings) are in the red commit.
- **The sink's stage is now `._decoy_publication_pending_<token>`** (plan 8 on-disk names), so
  every "no stage left" check moved to that prefix instead of passing vacuously on the old one:
  `tests/unit/execution/_b6a_support.py` (`leftovers`), `test_b6a_publish.py`,
  `test_transactional_sink.py`, `test_ooc_null_bearing_int_guard.py`,
  `tests/unit/generation/test_fr0_streamed_entry.py`, `tests/integration/test_f29_failure_contract.py`,
  `test_f29_publication_state.py`, `tests/parity/test_phase_d_d1a_hash_pinning.py`,
  `test_phase_f_f2_mixed_publish.py`, `test_phase_f_f2_mixed_sweep.py`,
  `tests/fileaccess/test_char_sink_publish.py`, `tests/fileaccess/test_x4_scratch_and_names.py`
  (comment only), `tests/unit/execution/publication/_sink_support.py`.
- **A target holding something else is refused** (`PublicationRefused`,
  `publication_target_occupied`, plan 1.4, 5.3, 4.1 E3/E6, probes P2 and P4), with the stage
  removed on that exit, where the old pin was a raw `OSError` 39 and a stage kept for `abort()`:
  `test_b6a_publish.py::test_a_non_empty_target_fails_the_commit_and_leaves_it_unchanged`,
  `test_phase_f_f2_mixed_publish.py::test_the_real_sink_commit_failure_leaves_the_target_untouched`,
  `test_char_sink_publish.py` (three cells renamed: `..._is_refused_and_left_unchanged`,
  `test_zero_write_commit_binds_a_descriptor_only_unit`,
  `test_zero_write_commit_onto_a_non_empty_target_is_refused`, the last one formerly "is silent").
- **A failed commit rename is `PublicationFailed`** (plan 4.1 E12 and G5: errno only, no OS text,
  stage removed on that exit): `test_transactional_sink.py::test_commit_failure_publishes_nothing`,
  `tests/integration/test_f29_failure_contract.py::test_a_failed_commit_rename_publishes_nothing_and_says_so`
  (its F29 record assertions unchanged: `storage_capacity_exhausted`, commit, `not_published`,
  `confirmed`, no secret), `test_f29_publication_state.py::test_the_reference_sink_confirms_cleanup_by_looking`,
  `tests/unit/generation/test_fr0_streamed_entry.py::test_commit_failure_aborts_and_preserves_the_error`
  (the message match became "the OS text is absent").
- **Registers moved, not grown**: `tests/sentry/test_reason_registry.py` (two `ADAPTERS` rows),
  `tests/sentry/secret_material_census.json` (two line numbers).

No existing test changed for a byte, row-count, determinism or route-parity difference: every
diff above is the descriptor, the stage name, or an outcome the plan names.

## Checks (code head `b96579ae`)

Each pytest run: `<tree>` root, `PYTHONPATH=<tree>/src`, `-q -p no:cacheprovider -n 2 -rfE
--tb=line --basetemp=<per-run dir, removed after>`; `decoy_engine.__file__` printed under the tree.

| Command | Tree | Passed | Failed | Errors | Skipped | Other |
|---|---|---|---|---|---|---|
| `pytest tests/unit/publication tests/fileaccess tests/sentry` | `b96579ae` | 6341 | 10 | 56 | 6 | 6413 collected |
| `pytest tests/fileaccess tests/sentry` (`tests/unit/publication` is absent on main) | `4bb95063` | 6011 | 10 | 56 | 6 | 6083 collected |
| `pytest tests/unit/execution tests/integration tests/parity tests/security` | `b96579ae` | 12342 | 0 | 6 | 2389 | 37 xfailed |
| same | `4bb95063` | 12230 | 0 | 6 | 2389 | 37 xfailed |
| `pytest tests/unit --ignore=tests/unit/execution` | `b96579ae` | 13285 | 0 | 0 | 84 | 2 xfailed, 5 xpassed |
| same | `4bb95063` | not rerun: the c1a record ran it on `4bb95063` in this container (13046 passed, 0 failed, 0 errors) | | | | |
| signature-grep files outside the commands (below) | `b96579ae` | 1129 | 0 | 0 | 657 | |
| same 30 files | `4bb95063` | 1129 | 0 | 0 | 657 | |
| `.venv/bin/ruff check src tests`; `ruff format --check src tests`; `mypy src/decoy_engine` | `b96579ae` | clean; 2268 files formatted; 796 source files | | | | |

First command, failing ids: identical to the baseline (`diff` empty), the 66 known ids: 56
errors of `tests/fileaccess/scratch/test_private_creators.py` (root) and 10 failures of
`tests/sentry/layout/test_import_layers.py` (import-linter runs in `.venv-layers` under gates). No
new failing id, no baseline id now passing. Collected 6083 to 6413 (+330): the stack's
`tests/unit/publication` (238) and `tests/fileaccess/publication` (51), and sentry
parametrizations over the stack's new modules (c1a recorded 6411 on its head; this slice adds the
two `ADAPTERS` rows' checks).

Second command, failing ids: identical (`diff` empty), the 6 errors of
`tests/unit/execution/test_scratch_root_umask.py` (root). Collected count +112: this slice's and
c1a's `tests/unit/execution/publication` (112 now, 44 at c1a; the c1a ids renamed here are the
P1 to P4 cells and the refused/failed commit cells).

Third command: no failing id on the head; on the baseline the c1a record (same commit, same
command, same container) has none either. Collected: 13376 on the head, as on the c1a head.

Signature grep (`git grep -l` in `tests/` for `ParquetTransactionalSink`, `observed_commit`,
`OutputPublish`, `ExecutionResult`, `GenerationResult`, `StagedParquetWriter`,
`TransactionalSink`, `project_sink_state`, `SINK_STATE_EVIDENCE`, `_replace_staged`,
`publication_receipt`, `staged_tables`): hits under `tests/unit`, `tests/fileaccess`,
`tests/sentry`, `tests/integration`, `tests/parity` and `tests/security` are run by the commands
above; the other 30 test files (`tests/acceptance/test_f06_cleanup_failure.py`,
`test_f06_route_parity.py`, five `tests/native`, twenty-two `tests/physical`,
`tests/privacy/test_no_pii_in_quality_summary.py`) ran as one command on both trees with the same
result and no failing id. The five `tests/perf` hits are the perf suite (not run);
`tests/fixtures/f29/producer-inventory.json` is data a sentry reads in the first command.

Slice gate: `GATES_WORKERS=2 bash scripts/dev/gates.sh --keep-going --baseline
tests/unit/execution/publication tests/fileaccess/test_char_sink_publish.py
tests/fileaccess/test_char_route_corpus.py tests/integration/test_f29_failure_contract.py
tests/integration/test_f29_publication_state.py tests/sentry/test_reason_registry.py
tests/sentry/test_secret_material_census.py tests/unit/execution/test_r3_failure_publication_trace.py`
on `b96579ae` (only uncommitted path: this record), exit 0. Branch `env`, `ruff`, `format`,
`mypy`, `sentry` (ratchet check, then 5118 passed, 2 skipped), `layers` (15), `licenses` (63),
`tests` (269 passed, 1 skipped) all PASS; baseline not rerun because every branch step passed;
failing ids branch=0, baseline=0, branch-only=0. Its own `/tmp/gates.*` and `/tmp/gt.*`
directories were removed.

An earlier probe of the second command (before the expected-output updates) showed one
pytest-timeout failure in
`tests/unit/execution/test_byte_estimate_routing.py::TestEndToEndWiring::test_width_change_flips_route_at_a_fixed_budget_and_row_count`:
the isolated worker child was spinning in OpenBLAS thread initialisation inside `import scipy`
under its address-space limit (native stack from `gdb`: `blas_thread_init` -> `mmap`), before any
publication code. Rerun alone three times: passed, hung (killed), passed. It passed in both final
runs above. Recorded as an environment flake, not a slice failure.

## Plan differences and decisions

- **`run_pipeline`'s sequential and out-of-core results carry no receipt.** The plan's c1b route
  edits name `_sequential.py:603` and `out_of_core/_runner.py:303` ("receipt from `commit()`"),
  and those functions now return it on `ExecutionResult.publication_receipt`. But
  `run_pipeline` reaches them through `_pipeline_route_exec.run_sequential_route` and
  `run_out_of_core_route`, which rebuild the `ExecutionResult` field by field and drop the new
  field, so on those two routes `run_pipeline(...).publication_receipt` is `None` (the receipt is
  on `sink.receipt`, which the caller built; full-frame streamed and mixed carry it on the
  pipeline result). Fixing it is two lines in `_pipeline_route_exec.py`, an eleventh production
  module, which the plan says splits the slice instead; not done. `test_route_outcome_matrix`
  reads those two routes' receipts where the route functions return them (spied at the pipeline's
  call sites) and from `sink.receipt`. `docs/publication.md` states the gap. Needs an owner call
  (a follow-up route-exec change, or accept).
- **`_pipeline_context.py` untouched.** Plan 9 says the request is "threaded from
  `_pipeline_context.py`", but every engine route takes a caller-built sink, and the caller passes
  the request to the sink (`ParquetTransactionalSink(..., request=...)`), so there is nothing to
  thread; the receipt comes back through `OutputPublish.commit()`.
- **Non-success commits raise.** Plan 8 says `commit()` returns a `CommitReceipt`; a route must
  not treat a refused or failed commit as a published run, so `commit()` returns the receipt for
  `published`/`already_committed` and raises the plan's typed errors otherwise (the receipt rides
  on the error and on `sink.receipt`). A failed rename that used to raise `OSError` now raises
  `PublicationFailed`; its errno rides as an errno-only `__cause__`, so F29 keeps
  `storage_capacity_exhausted` for a full disk (mutation 7).
- **`reconcile()`'s sealed outcome** is `already_committed` (found bound and made durable in this
  call); the plan names no outcome for it. It reads the bound descriptor without the destination
  lock: a bound unit is never replaced or removed by the engine (G2, L4).
- **Evidence projection** lives in `_publication_observer.py` (`SINK_STATE_EVIDENCE`), applied by
  the sink after every exit (plan 5.6 says "in the sink"; the table sits with the evidence type,
  the call in the sink).
- **Owner rulings of 2026-11-12, applied:** (1) `staged_tables()` on `ParquetTransactionalSink`
  (`execution/_transactional_sink.py`) and on `OutputPublish` (`execution/_chunked_output_sink.py`),
  the sink's one source of the `tables` argument; (2)
  `test_descriptor_describes_every_staged_table[*]` over the five routes c1b wires, with mutation
  9; (3) no `StageDir.entries()`; (4) every test file under 900 lines (largest:
  `test_sink_transition_cells.py`, 355); (5) a request carrying a vault: the sink refuses it with
  `ValueError` at construction and `publish_unit`'s `NotImplementedError` stays; (6) `origin/main`
  not merged; baseline `4bb95063`; (7) the "rest of the suite in pieces" run was stopped before it
  produced results (it was started, then dropped with the ruling; no partial numbers are claimed).
- **Isolated route in between c1b and c2.** The isolated child still builds a
  `ParquetTransactionalSink` on the parent's sibling staging directory (c2 swaps it), so the child
  now binds a descriptor into that staging directory, which the parent then renames into
  `output_dir` (the descriptor is valid there), and the child's lock file
  `.<staging-name>.decoy-lock` stays beside `output_dir`, one per isolated run with an
  `output_dir`. The isolated suites pass. Litter only; c2 removes the cause.
- **R5 (plan 12) grep:** platform `main` (`fbaee2a9`) reads the engine's staged output by table
  name (`api/jobs/v2_sequential.py`, `v2_out_of_core.py`: `stage_dir/{table}.parquet`), not by
  listing, and builds its sink from engine `RootedFileAccess` capabilities (which have `lock`,
  `read_member`, `sync_chain`); it pins engine 0.18.0, so nothing changes for it before a repin.
  The CLI (`decoy` `1d53eda`) does not use the sink. No hit; nothing patched.
- **Slice size.** Production +305/-174 across nine files; tests +1,244/-121 (28 files). Over the plan's
  estimate of 575 and its 800-line cap, all of it tests (every route, both policies, the crash
  cells) and the expected-output updates; reported for the orchestrator.

## Not run

- The perf suite and the full suite (the full suite and macOS are the orchestrator's on the merge
  head; the per-directory run was dropped by the ruling, see above).
- The baseline of the third command (cited from the c1a record, same commit and container).
- The uid 65534 pass: this slice adds no mode or permission behaviour of its own (unit modes come
  from a1b's `publication` label, whose tests ran under uid 65534 in a1b and a3).
- The class-8 non-vacuity deletion check.
- macOS and Windows: the slice adds no platform call (no `sys.platform` branch needed).

## V2-LOG

- `_pipeline_route_exec.py` drops `publication_receipt` on `run_pipeline`'s sequential and
  out-of-core results (above; owner call).
- A test in `tests/unit` (the `gates.sh` tests) clones the repository per case into the pytest
  basetemp (about 156 MB each, about 20 GB per run under `-n 2`); with older basetemps left by
  killed runs the container's volume filled once during this slice. Those tests also leave
  `/tmp/gates.*` directories behind; this slice's own gate run removed its own.
- `TransactionalSink.commit` is annotated `-> object` so the reference sink's receipt type-checks
  against the protocol; the method set is unchanged.
- The OpenBLAS-in-isolated-child hang above (environment).

## Merge of main, second pass (2026-11-12)

`origin/main` `a3cb996f` (which contains slice a1a, squash-merged as #488 after its
review fix round) was merged up the stack with merge commits, never a rebase:
a1b `d954ce3f` then `40f7870e`, a2 `2d4e7b16`, a3 `1edfa4c9` then `5861cfb5`,
c1a `ea40c2cc`, c1b `53806a33`.

- a1b: after the merge, its strict test cells take the shared `POLICIES` list from
  `tests/fileaccess/publication/_dirmode_support.py` (the #488 review form: strict
  cells are skipped where `openat2` is not usable, which is never the case on Linux).
- a3: main's #506 replaced per-PR registry minors with release-time stamping. The
  thirteen publication codes moved to the pending form (`since=UNRELEASED`) with
  `scripts/dev/registry_renumber.py --to-unreleased`; the branch no longer changes
  `REGISTRY_VERSION` and its minor fixture is deleted. `registry_renumber.py --check`
  reports "registry consistent". This supersedes the 1.25.0 renumber recorded earlier.
- c1a: conflicts only in the `publication/` rows of `CODEMAP.md` and
  `src/decoy_engine/_MAP.md`; main's rows kept for its packages, the slice's row for
  `publication/`.
- c1b: clean merge. `staged_tables()` is on `ParquetTransactionalSink` and
  `OutputPublish`. LOC: `_transactional_sink.py` 600, `_chunked_output_sink.py` 600,
  `_sequential.py` 658 (main 660), `_pipeline_context.py` 573, `out_of_core/_runner.py` 339.

Environment: Python 3.10.20, `bootstrap.sh --check` OK after the merge.

Checks on c1b `53806a33` (`.venv/bin/python -m pytest ... -q -p no:cacheprovider -n 2 -rfE`):

| Command | Result | Failing ids |
|---|---|---|
| `tests/unit/publication tests/fileaccess tests/sentry` | 6444 passed, 10 failed, 87 errors, 6 skipped | 56 `test_private_creators.py` and 31 `test_scratch_arena.py` errors (both files identical to main; they assert a non-root uid), 10 `test_import_layers.py` (import-linter is only in `.venv-layers`) |
| `tests/unit/execution tests/integration tests/parity tests/security` | 12398 passed, 6 errors, 2389 skipped, 37 xfailed | 6 `test_scratch_root_umask.py` (non-root uid) |
| `tests/unit --ignore=tests/unit/execution --ignore=tests/unit/scripts` | 13371 passed, 84 skipped, 2 xfailed, 5 xpassed | none |
| `ruff check`, `ruff format --check`, `mypy src/decoy_engine` | pass | none |

On a3 `5861cfb5` and c1a `ea40c2cc` the targeted sets (publication, fileaccess, sentry,
reason registry tests; ruff, format, mypy) show the same known ids only.

Not run or not completed in this pass: a baseline run on `a3cb996f` (the known ids were
classified by cause and by the files being identical to main, not by a fresh diff);
`tests/unit/scripts` (its tests clone the repository per case and filled the 25 GB
volume twice; a run with `tmp_path_retention_policy=none` gave 428 passed and 73
failures or errors that are artefacts of that option removing the worker temp directory;
the stack changes nothing under `scripts/` or `tests/unit/scripts/`); `gates.sh`; the
uid 65534 pass; the full suite; macOS; Windows. `origin/main` has moved past
`a3cb996f` and was not taken.

Open for review fix lists: later slices' test modules (a3, c1a, c1b) still define their
own `["strict", "ambient"]` lists rather than the shared `POLICIES`, so their strict
cells would not be skipped on a host without `openat2`.

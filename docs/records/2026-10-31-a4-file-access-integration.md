# A-4 engine file-access: integration of `claude/jolly-carson-3k5stf` with the STORM close-out and main (2026-10-31)

Status: handoff evidence. Integration only (merge, resolve, re-record, re-prove); nothing new was
built and nothing here is a gate receipt. No PR was opened, nothing was merged into main, no branch
was deleted. The gate that follows is Dennis (R3, frozen contracts: the `run_pipeline` signature
table, `CONTRACT_VERSION` versus `EXPECTED_FILE_ACCESS_CONTRACT`) and then Codex FINAL.

Plan followed: `plan/a4b5-file-access-refold` (`37cec36e`, decoy-platform), sections 2 and 4, with
the base chosen by `docs/records/2026-10-30-file-access-branch-inventory.md` (branch
`review/file-access-inventory`, `d97d5a9f`).

## Head and history

Branch `claude/jolly-carson-3k5stf`. Start `aa424af4`. Every run below is on `59abbb16`; the commits on top of it change only `docs/records/` (this file). Merges only; the pushed
branch was never rebased.

| Commit | What |
|---|---|
| `58a2a16f` | Merge `origin/claude/eager-clarke-14deh1` (`ab29ef0c`, STORM close-out carrying F29 #336, the `storm` to `profiling` rename and the unified `decoy_engine.reasons` registry). 30 conflicts. |
| `1a5973dd` | Merge `origin/main` (`a6e5c252`: #347 worker bootstrap, #348 H-1 Wave A). 1 conflict. |
| `1ec21ce5` | Cherry-pick of `9d478486` from `claude/sharp-goodall-ikag71` (two comment lines; re-applied by hand). |
| `761eaedd` | E1-4 registry entry, golden provenance, error-text re-record, pyarrow 24 canary, sink cleanup evidence, F29 contract tests re-pointed at the adapter. |
| `59abbb16` | Field keys follow the close-out, second registry entry, census history. |

The cherry-pick of `215cc7e0` (golden re-baselined from unmodified main) produced no change: the
branch's `route_corpus_golden.json` was already byte-identical to that commit's version, and its
`test_physical_seam_disconnection.py` hunk only removed duplicate entries this branch never had.

`git merge-tree --write-tree origin/main HEAD`: clean, no conflicts.

## Conflict resolution, per class

STORM merge (`58a2a16f`), 30 files:

- 26 import-adjacency hunks (both sides added an import at the same line): union. Files:
  `connectors/postgres_snapshot.py`, `execution/_chunked.py`, `_chunked_code_set.py`,
  `_chunked_output_sink.py`, `_governor.py`, `_isolated_run.py`, `_job_estimate.py`,
  `_pipeline_routing_signals.py`, `_strategies/_code_set.py`, `_strategies/_joint_mask.py`,
  `capacity.py`, `generation/_pinned_compile.py`, `_pinned_execute.py`, `generation/window.py`,
  `io/bounded_reader.py`, `subset/_api.py`, `subset/_preflight.py`, `unmask.py`, plus the import
  hunk of each file below.
- Content hunks, resolved by composition (F29's registry symbol and typed plumbing kept; E1's
  policy-bound open, handle, stage or redacted `shown` display kept at the same site; E1's
  no-cause-chain raises kept where the library text can name the file):
  `generation/statistical/_spec.py`, `plan/_generation.py`, `keyprovider.py`,
  `quality/dp_provenance.py`, `vault.py`, `transforms/joint_mask.py`,
  `transforms/_codeset_loader.py`, `subset/_materialize.py`.
- `execution/_transactional_sink.py` (6 hunks): E1's policy-issued `OutputTxn` stage plus F29's
  `PublicationEvidence`, `capture_publication_failure` and the `stored_output` report. The stored
  size is read through the staged component's own descriptor (`txn.open_file` then `fstat`),
  because a strict stage has no path. Cleanup after abort is confirmed by looking at the ambient
  transaction's stage path (F29's semantics); a strict stage has no path and its discard swallows
  removal errors, so its cleanup state is recorded `unknown`, not claimed. This is an open item
  for the gate (see below).
- Four E1-only raise sites outside the conflict hunks still carried string codes F29 never saw
  (found by `tests/sentry/test_reason_registry.py::test_no_ad_hoc_reason_codes`):
  `statistical/_spec.py:165`, `plan/_generation.py:136`, `keyprovider.py:272`,
  `transforms/_codeset_loader.py:56`. Each now references the existing registry symbol; no
  registry identity was added.
- The three E1 exception families (`FileAccessDenied`, `FileAccessReadError`,
  `FileAccessUnavailable`) are listed as consciously unclassified in the reason-registry sentry.
  A registry identity for the denial is the gate's call.
- `CHANGELOG.md`: union under Unreleased (E1 entry first, then the close-out and F29 entries).
- `tests/sentry/test_module_size.py`: union at the LOC measured on the merged tree. No cap raised.
- `tests/sentry/test_physical_seam_disconnection.py`: the close-out's restructure taken whole,
  E1's permitted-module entries re-inserted into the new set; no entry deleted.

Main merge (`1a5973dd`), 1 file: `tests/sentry/test_module_size.py`, union at measured LOC;
entries retired by H-1's split (`native/_determinism_protocol.py`, `native/_draw_site_providers.py`,
`execution/_mem_telemetry.py`, `quality/synth_report.py`) deleted.

Census delta against main (all exact, measured by the sentry): `_isolated_run.py` 622 to 651,
`_pipeline_routing_signals.py` 638 to 645, `_chunked.py` 627 to 630, `native/_chunked_entry.py`
681 to 684, `_sequential.py` 696 to 699 (one under the cap), `profiling/profiler.py` 637 to 640,
`plan/_compile.py` 663 to 665, `out_of_core/_spill_estimate.py` 623 to 645,
`internal/faker_setup.py` 637 to 668, `transforms/_codeset_loader.py` 607 to 683, new
`quality/dp_provenance.py` 602; shrinks `quarantine.py` 645 to 638 and legacy
`profiling/model_pack/trainer.py` 715 to 712. Recorded in
`docs/decisions/module-size-census-history.md`.

H-1 moved code: E1 touched none of `execution/_chunked_fk*`, `native/_draw_sites*`,
`out_of_core/_stream_join*` or their sources (`git diff --name-only 1b3a9229 aa424af4 -- src`
intersected with `git show --name-only a6e5c252 -- src` is empty), so there was no E1 edit to
re-apply to moved code.

Field keys: the two E1 config positions that named the retired namespace (`storm.model_pack`,
`storm.name_hints`) are `pre_scan.model_pack` and `pre_scan.name_hints` (the close-out's name for
the profile/detect phase); `tests/sentry/test_no_residual_removed_surface.py` was red on them and is
green now.

galileo's `fileaccess/adapters/arrow.py` `read_schema` hunk: left out. No test in the merged tree
calls it (the three `read_schema` uses under `tests/unit/execution` call `pyarrow.parquet` directly).

## E1-4 inventory on the merged tree

`tests/sentry/test_file_access_boundary.py` found two sinks the registry did not cover, added one
at a time with rule and owner (no module-wide exemption), count 189 to 191:

1. `execution/_isolated_reason.py::commit_reason::Path.exists` (F29 cleanup evidence on the
   isolated route's `staging_dir`, behind `_require_ambient_for_paths`); `ambient_only`, and the
   helper module listed beside `_isolated_commit.py` in `file_access_modules.toml`.
2. `execution/_transactional_sink.py::ParquetTransactionalSink._cleanup_staging::Path.exists`
   (the ambient transaction's own stage path after abort; a strict transaction raises before any
   path exists).

## Golden provenance (regeneration disabled in the gate run)

The route-corpus golden was re-recorded from unmodified `origin/main` `a6e5c252` in a detached
worktree (`git worktree add --detach`, `uv sync --frozen --extra dev` from main's own lock, which is
byte-identical to this branch's), by copying only `tests/fileaccess/{__init__,route_corpus,
test_char_route_corpus}.py` and the golden file into it and running
`FILEACCESS_WRITE_GOLDENS=1 pytest tests/fileaccess/test_char_route_corpus.py` (11 passed). The
`no_companion` section it wrote is byte-identical to the branch's golden, so the file did not
change. The `companion` section was not touched (no compiled companion here). Every gate run below
ran with `FILEACCESS_WRITE_GOLDENS` unset.

## The two `test_char_error_text.py` failures (pyarrow 24)

Checked first, as required. The pyarrow 24.0.0 text is `Error creating dataset. Could not read
schema from 'bad.parquet'. Is this a 'parquet' file?: Could not open Parquet input source
'bad.parquet': ...`. It names the file path (twice); it names no value. The old text the test
pinned also named the path once. Then:

- Under a strict policy, a value-bearing canary (`CANARY-SSN-123456789.parquet`, malformed) at
  both boundaries the test pins, R1 `LazySource.to_table` and R4 `load_table("customer:...")`,
  raises `FileAccessReadError` with `file could not be read: source.path` /
  `file could not be read: joint_mask.reference` and nothing else: no cause, no context, the
  canary and the pyarrow text in none of str, repr, args or notes. The boundary already holds
  (`fileaccess/_errors.py::guarded` raises the typed error `from None` after the handler closes),
  so no fix was needed. The canary is now a test:
  `tests/fileaccess/test_policy_errors.py::test_the_pyarrow_24_dataset_text_never_reaches_a_strict_error`
  (two cells). Mutating `guarded` to pass the library error through turns both cells red.
- Under the ambient (no-policy) default the text is the caller's own relative path echoed with the
  library error, which `test_char_error_text.py` exists to pin as unchanged CLI behaviour. The two
  expectations were re-recorded from unmodified main under the lock: the same test file copied into
  the main worktree fails identically there with exactly the text now pinned.

Report: no accidental leak at either boundary under the V1 threat model; the ambient text is the
characterized CLI contract, not a boundary conversion.

## CI

`.github/workflows/ci.yml` still installs strace (line 116, "fails, never skips, without it") and
lists `tests/fileaccess` in the routing guard; the regression gate runs the full suite.

## Runs (from the lock, in one container, as root)

Environment: `uv 0.11.32`, `uv sync --frozen --extra dev --extra cloud --extra lint` from this
branch's `uv.lock` (identical to main's), pyarrow 24.0.0, strace 6.8, kernel 6.18 with `openat2`.
Three environments: `.venv311` (CPython 3.11.17), `.venv310` (CPython 3.10.20), and the main
worktree's own venvs (3.10 and 3.11) for baselines. The first set of lanes I ran was on 3.10 by
accident (uv picked 3.10 for the default venv); every lane below was then re-run on a venv created
with `--python 3.11`, and the 3.10 numbers come from `.venv310`. All runs are on head `59abbb16`
except where a commit is named.

| Lane | Command (prefix `uv run --frozen --no-sync`) | 3.11 | 3.10 |
|---|---|---|---|
| file-access suite + three sentries | `pytest tests/fileaccess tests/sentry/test_file_access_boundary.py tests/sentry/test_file_access_entry_points.py tests/sentry/test_locator_hygiene.py -q` | 673 passed, 2 skipped | 673 passed, 2 skipped |
| phase, sentry (incl. module size, reason registry, residual surface), unit reason registry | `pytest tests/unit/phase tests/sentry tests/unit/test_reason_registry.py -q` | 3709 passed, 1 skipped, 2 xfailed, 5 xpassed | 2 failed, 3707 passed, 1 skipped, 2 xfailed, 5 xpassed |
| F29 registry only | `pytest tests/sentry/test_reason_registry.py tests/unit/test_reason_registry.py -q` | 55 passed (in the lane above) | 55 passed |
| sink, progress, F29 contract, E1 sink/output | `pytest tests/unit/progress tests/unit/execution/test_transactional_sink.py tests/unit/execution/test_b6b_sink.py tests/integration/test_f29_failure_contract.py tests/fileaccess/test_char_sink_publish.py tests/fileaccess/test_policy_output.py -q` | 174 passed, 2 skipped (3.10 venv at `761eaedd`) | same |
| ruff | `ruff check src tests testflight scripts`; `ruff format --check src tests testflight scripts` | all checks passed; 1840 files already formatted | same binary |
| mypy (pinned 2.3.0) | `python -m mypy src/decoy_engine testflight` | 17 errors in 8 files (685 files checked); main `a6e5c252` on 3.11: 17 errors in 8 files (655 checked), the same error set modulo line numbers | 16 errors in 7 files; main on 3.10: 16 in 7 |
| module-size sentry | `pytest tests/sentry/test_module_size.py -q` | 754 passed (in the lane above) | 754 passed |

Skips: `tests/fileaccess/test_char_vault_in_place.py:35` (root ignores directory permissions),
`tests/fileaccess/test_x2_locators.py:366` (`lightgbm` absent), `tests/sentry/test_expression_safety.py:74`
(audited safe path).

The two 3.10 failures are `tests/unit/phase/test_v1_canaries.py::test_error_result_never_reads_the_exception_class_name_or_text`
and `::test_repr_and_wire_of_every_public_envelope_type_carry_no_marker`: the test calls
`exc.add_note`, a Python 3.11 API. The file is byte-identical to the STORM close-out branch's, so
this is a close-out defect on the 3.10 lane CI uses, not an E1 or merge effect; it is reported, not
patched (a 3.10 guard would weaken a canary). mypy's one extra error on 3.11 is the `tomli` import
in `quality/dp_provenance.py`, identical on main.

Pre-existing on unmodified main under the lock (not from this branch):
`tests/unit/execution/test_r3_failure_publication_trace.py` fails 2 of 48 on main `a6e5c252`
(`OocIneligibleError` where the trace expects `ConfigError`); the branch fails the same 2.

D10 cells that ran (real strace 6.8, all passed on both 3.11 and 3.10, 16 cells):
`test_policy_strace.py`: `test_a_streamed_run_touches_the_tree_only_through_the_policy`,
`test_a_policy_decided_denial_makes_no_syscall_naming_the_target`,
`test_a_kernel_decided_denial_never_opens_the_target`,
`test_cold_import_reads_the_packaged_grammar_and_the_detector_sees_it`,
`test_the_detector_sees_a_direct_open_and_read_of_the_target`;
`test_strace_selftest.py`: `test_an_in_process_open_is_seen`, `test_an_open_by_a_child_process_is_seen`,
`test_an_open_after_execve_is_seen`, `test_a_dir_fd_relative_open_is_attributed_by_name_and_directory`,
`test_a_directory_enumeration_is_seen`, `test_a_renameat2_publication_is_seen`,
`test_a_library_read_is_seen[duckdb|pandas|polars|pyarrow]`, `test_a_duckdb_write_is_seen`.

E1-4 registry count: 191.

## Mutation checks (each mechanism mutated once, test shown red, then reverted)

| Mutant | Test | Result |
|---|---|---|
| `_stored` returns before recording the size | `tests/unit/progress/test_stored_bytes.py` | 2 failed, 1 passed |
| cleanup verdict inverted (`confirmed` when the stage still exists) | `tests/integration/test_f29_failure_contract.py` | 4 failed, 3 passed |
| `guarded` passes the library error through under a strict policy | the pyarrow 24 canary | 2 failed |
| the `_isolated_reason.py` registry entry removed | `tests/sentry/test_file_access_boundary.py` | 2 failed, 55 passed |
| one E1-only code back to a string literal | `test_no_ad_hoc_reason_codes` | 1 failed |

## The STORM squash later

The close-out merges onto main clean (`git merge-tree --write-tree origin/main origin/claude/eager-clarke-14deh1`:
no conflicts). Simulating its squash (`git commit-tree <that tree> -p origin/main`) and merging
that synthetic commit into this head: a plain merge reports content conflicts in 7 files
(`CHANGELOG.md`, `execution/_chunked_output_sink.py`, `_job_estimate.py`,
`_pipeline_routing_signals.py`, `capacity.py`, `tests/sentry/test_module_size.py`,
`tests/sentry/test_reason_registry.py`), because the squash has no shared history with the
hand-composed hunks. With `-X ours` the merge result tree is exactly this head's tree
(`e326e98a`): content-identical, nothing new. So the second merge is trivial by taking the branch
side of every conflict, and that is the only delta: the squash carries no content this branch lacks.

## Not run

- The full engine suite outside the lanes above (CI's regression gate).
- Native-companion cells and the `companion` golden section (no compiled companion here).
- Coverage and mutation tooling beyond the five hand mutants above.
- Any platform test; any run as a non-root user (one skip); the `ml` extra (one skip).
- The owner's LOCAL cells in refold plan section 4.

## Open items for the gate

1. Cleanup evidence under a strict policy is recorded `unknown` after abort: the rooted
   transaction's discard swallows removal errors and exposes no path or descriptor to confirm
   against. If the gate wants `confirmed` there, `OutputTxn` needs a confirm hook (new E1 surface).
2. `FileAccessDenied` has no reason-registry identity (listed unclassified). The denial crosses the
   platform boundary as text only.
3. The close-out's `add_note` canaries fail on Python 3.10 (above).
4. `test_r3_failure_publication_trace.py` fails 2 on main under the lock (above).
5. The `DEFECT-CATALOG.md` classes seen: none new. Class 8 (overclaimed evidence) is why the lane
   table names the venv each number came from.

# B7 multi-table dispatch: build record

Status: record

Date: 2026-10-02. Plan: `docs/plans/2026-10-01-multi-table-dispatch.md` (revision 3, not edited by this record). Branch `feat/multi-table-dispatch`, rebased onto engine main `d438e47a` (B2 dispatcher auto-chunk #188 and the byte-estimate fix #189 are both on it). This record covers the build and its evidence. It is not the review gate verdict.

## What shipped

An independent multi-table `run_pipeline` job (no FK edge, no `relationships` block) now runs each mask table on the route it would take alone. A table that would auto-chunk as a single-table job masks through B2's `run_auto_chunk`, one table at a time in `tables:` config order. Every other table runs in one full-frame adapter call, as before. The new module is `src/decoy_engine/execution/_pipeline_multi_table.py` (314 lines, under the plan's 600-line cap). `run_pipeline` gains `multi_table_dispatch_enabled` (default `True`, the kill switch). A job stays whole when quarantine is enabled, a vault writer is passed, validators are configured, a mask column uses unseeded randomness, a generate table is present, or the dispatcher or auto-chunk is off.

`_pipeline.py` stays at its 679-line ratchet: the four lines the knob adds are paid for by tightening the `_provider_snapshot` docstring paragraph and one routing sentence. No ratchet moved.

## Commits

| SHA | What |
|---|---|
| `5654caca` | Tests first: the acceptance tests, the two superseded tests edited as plan test 11 states, the seam sentry's lists |
| `0b4e9639` | Fixture and helper corrections on first contact with the code (each a test mistake, listed below) |
| `5f1cf849` | The implementation, CHANGELOG, compatibility contract and CODEMAP |
| `915d187c` | Unit tests for the gates and merge rules; the seam sentry's permitted list gains `_pipeline_chunk_route.py` (docstring-only edit) |
| `65780986` | Tests that kill the first-pass mutation survivors; `_table_entry` keys its lane fields on the lane block alone |
| `1b303a53` | Pins the knob name in the `invalid_execution_knob` message (kills one hand-mutant) |
| `72c99d83` | Lazy-source test now poisons `LazySource.iter_batches` and `open_batches` (the first version spied on a method that does not exist) |
| `d8177fc8` | Benchmark driver and worker, and the 1M-row run artifact |

## Red before

At `5654caca` (tests only, no implementation), over the seven files that hold the acceptance tests and the two edited existing tests (`test_multi_table_gates.py`, `test_multi_table_when.py`, `test_multi_table_contract.py`, `test_multi_table_run.py`, `test_auto_chunk_routing.py`, `test_auto_chunk_dispatcher.py`, `tests/sentry/test_physical_seam_disconnection.py`): 251 failed, 166 passed, 1 skipped. The 166 passes are the no-split golden cases, which hold before and after by construction (the split never engages, so the run equals the split-off run). The failures fall into four groups:

- 31 per-table "when" cases fail at the first assertion, that the anchor table dispatched (`'full_frame' == 'chunked'`): today no multi-table table dispatches.
- The output-contract matrix (every case x companion present and absent x `native_threads` 1 and 4) fails at the assertion that the expected tables dispatched. The mixed-shape, side-channel, evidence, order, thread and memory tests fail on the missing `auto_chunk.tables`, the missing `chunked_route_by_table`, or a spy that records no `run_auto_chunk` call.
- The cases that need the new knob fail with `TypeError: run_pipeline() got an unexpected keyword argument 'multi_table_dispatch_enabled'`, including the two superseded tests, which now pass the kill switch.
- The seam sentry fails because `_pipeline_multi_table.py` does not exist yet.

Some of those failures also hid test mistakes, which only showed once the code ran: see "Test corrections on first contact".

## Existing suites with the split as the default

The plan asks for the existing execution, sentry and physical suites to run with the split as the default before the acceptance-test commit. The split cannot be the default before it exists, so this ran after the implementation, as a full-suite run (19,926 tests collected) at `5f1cf849` plus the unit-test commit. Two failures:

- `tests/unit/test_v2_cloud_sources.py::...::test_profile_gcs_source_via_mocked_client`: `No module named 'google'`, the known pre-existing failure.
- `tests/sentry/test_physical_seam_disconnection.py::test_production_execution_modules_are_byte_identical_to_origin_main`: my docstring edit to `_pipeline_chunk_route.py` was not on the sentry's permitted list. Fixed in `915d187c` by adding it with a docstring-only justification.

No existing test other than the two plan test 11 names needed an edit. Both were edited as test 11 states: each keeps its assertions verbatim with `multi_table_dispatch_enabled=False`, and each has a split-on sibling.

## Test corrections on first contact

Commit `0b4e9639`. Each is a mistake in my test, not a weakening of an assertion, with one disclosed loosening:

- The kill-switch case passed the knob twice (`mt.kw(**off_kw(), **knob)` when `knob` was the kill switch itself).
- Quarantine and validator configs were written in the wrong shape (quarantine needs `output_path`; validators are `name`, `columns`, `params`).
- A spy on `PandasExecutionAdapter.run` counted B1's per-chunk oracle calls for a dispatched table as the group starting. The spy now looks for a call that carries the group table.
- Quality metrics from two runs were compared with their wall-clock fields. The comparison now strips keys ending in `_ms`.
- The contract matrix did not apply the type rule for the standard `h` and `r` columns on cases that name no string-output set, and treated an all-null native Faker column as `string` on the companion-absent route, where B2 guarantee 3 (b) says its type equals the split-off type. Both now follow B2's per-route rule.
- Loosened, then restored (dennis MEDIUM-1): the warning test first asserted exactly two `fpe_join_group_active` warnings, then `>= 2`. The exact count is four (one per column per table) and is asserted again, see the dennis section.

The fixture builder for the contract matrix also drops `time64_ns_nonaligned`: that source cannot be converted by the full-frame run either (`ArrowInvalid: Value 7 has non-zero nanoseconds`), so no split-off reference exists for it. The aligned `time64[ns]` column is in the matrix.

## Date and time cases without the workaround

The plan said date, time and time-zone columns run with `use_byte_estimate_routing=False` because B2's byte-estimate defect was still on main. That defect is fixed on main (#189). The date and time cases were written without the workaround first: the `date64` and `time64[ns]` cases in plan test 3, the whole passthrough-type matrix in plan test 4 (`date32`, `date64`, `time32`, `time64`, tz-aware timestamps), and the `time64[ns]` case in test 4. All pass with the default `use_byte_estimate_routing=True`, so the workaround is dropped everywhere and the test helpers never pass the flag.

## Test results

Final run, command from the worktree root, companion venv, one process under the box-wide lock:

    PYTHONPATH=$PWD/src:$PWD /home/cam/bin/pytest-one /home/cam/.cache/decoy-native-venv/bin/python tests -q --no-header

Result at `HEAD` before this record's count update: 1 failed, 19719 passed, 168 skipped, 21 deselected, 59 xfailed in 1486 s (19,926 collected, 21 of them deselected by the repo's own marker config). The one failure is the known pre-existing `tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client` (`No module named 'google'`). The B7 files alone: `test_multi_table_gates.py`, `test_multi_table_when.py`, `test_multi_table_contract.py`, `test_multi_table_run.py`, `test_multi_table_units.py` add the new tests, and the sentry and the two edited existing files pass.

Targeted runs while building: the four B7 acceptance files, `test_multi_table_units.py`, `test_auto_chunk_dispatcher.py`, `test_auto_chunk_routing.py`, and `tests/sentry`, all green at every commit after the implementation.

`ruff check .`, `ruff format --check .` and `mypy src/decoy_engine testflight` (503 source files) are clean.

## Coverage

Branch coverage over `src/decoy_engine/execution` from the B7 acceptance and unit files plus `test_auto_chunk_routing.py` and `test_auto_chunk_dispatcher.py` (442 tests). pytest-cov and coverage came from a scratch `--target` directory outside the repo, so no project dependency changed. Changed lines are taken from `git diff d438e47a..HEAD`.

| Unit | Whole file, line | Changed lines | Changed-line result |
|---|---|---|---|
| `_pipeline_multi_table.py` | 100% line, 100% branch (99 statements, 28 branches, 0 missed) | all (new file) | 100% / 100% |
| `_pipeline_generate_mask.py` | 98.5% | 5 statements | all covered, no branch missed |
| `_pipeline_finalize.py` | 78.3% (rest is legacy finalize code B7 did not touch) | 5 statements | all covered, no branch missed |
| `_pipeline.py` | 93.0% | 1 statement (`require_bool`) | covered; the signature default, the two forwarded arguments and docstring lines are not statements |
| `_planner.py`, `_pipeline_routing.py`, `_pipeline_chunk_route.py` | n/a | docstring only | no executable change |

## Mutation

mutmut was not used (its copy-the-tree model does not fit the lock and `PYTHONPATH` setup here). Instead, a small AST mutator over `_pipeline_multi_table.py` applied one mutant at a time to the worktree file that the tests import (comparison and boolean operators, `not`, unary minus, constants, `+=`, `+`, `-`, `//`, deleted call statements, negated `if`), deleting the module's cached bytecode before each run and restoring the file afterwards. The git tree was clean after each pass.

- First pass: 129 mutants, 114 killed, 15 survived. The survivors were five equivalent mutants (below) and ten that were real gaps: the `frozen=True` flag, the `__all__` entries, a branch of `_table_entry`, and the `full_frame.append` in `decide_multi_table_split` (nothing read `split.full_frame`). `65780986` added tests that kill them and simplified `_table_entry`.
- Second pass: 128 mutants, 123 killed, 5 survived. All five are equivalent: negating the `if TYPE_CHECKING:` guard (the imports it guards are only used in annotations), and four type-annotation `None` constants (`-> None`, `| None` in three signatures), which `from __future__ import annotations` never evaluates.

Hand mutants for the wiring that the AST pass does not cover (the `run_generate_and_mask_steps` call site, `stamp_execution_metrics`, `run_pipeline`) and for argument forwarding inside the module: 27 mutants, 26 killed on the first pass. The survivor was "`require_bool` names the knob wrongly" (the message carried a different knob name); `1b303a53` pins the name and a re-run killed it. Killed: split never decided, `vault_writer_present` constant false, `split_enabled` constant true, `dispatcher_enabled` constant true, `auto_chunk` constant true, threshold forwarded as 1, `caller_sources` replaced by the materialized sources, `native_threads` forwarded as 1, chunk size forwarded as 1000, `key_provider` dropped, unconfigured-column policy forced to warn, split not passed to the stamp, split row errors dropped, split branch disabled, stamp branch disabled, stamp merge order swapped, stamp threshold and chunk size swapped, knob default flipped, `require_bool` dropped, knob not forwarded, classification on the unrestricted config, group handed every source, dispatcher flag false to `run_auto_chunk`, wrong source passed to a table, and table entries in dispatched-first order. A further hand mutant, making the module read a `LazySource`, was killed by the poisoned-reader test after that test was rewritten (`72c99d83`).

## Benchmark

Run ID `B7-BENCH-2026-10-02-1M`. Raw JSON: `docs/records/b7-bench-2026-10-02/b7-bench-1m.json`. Driver `scripts/bench-multi-table/bench_multi_table.py`, worker `bench_worker_multi_table.py`. Made under the box-wide test lock (`flock /home/cam/.cache/pytest-one.lock`), in the companion venv, from the worktree.

Setup, per plan section 11 unchanged: three independent tables built with B2's generator (`t1` and `t2` 1,000,000 rows, `t3` 50,000 rows, seeds 20261001 to 20261003, namespaces `bench_ns_1` to `bench_ns_3`), default knobs (threshold 100,000 rows, chunk size 50,000), no vault writer, no validators, global seed 42. 7 interleaved rounds in an order shuffled per round (seed 20261001, orders in the JSON), one discarded warmup per configuration, a fresh subprocess per trial, split-off references from separate unmeasured subprocesses plus split-on references for b, c and d1, every comparison in the parent. Intel Core i5-7500 (4 cores), Python 3.11.15, pyarrow 25.0.1, pandas 2.3.3, engine 0.7.0, companion 0.1.0. Source `Table.nbytes`: `t1` 80,000,000, `t2` 80,000,000, `t3` 4,000,000 bytes (the `extra` variant adds 14,000,000 bytes to each of `t1` and `t2`).

Configuration d uses the unconfigured-column trigger (B8 is not on main): `t1` and `t2` each gain an unconfigured column `extra`, so B1 reroutes both to its oracle route with `uncovered_columns`.

| Config | Variant | Split | Threads | p50 s | max s | p50 RSS MiB | max RSS MiB |
|---|---|---|---|---|---|---|---|
| a | base | off | 1 | 24.15 | 26.26 | 1826 | 1859 |
| b | base | on | 1 | 4.23 | 5.44 | 757 | 757 |
| c | base | on | 4 | 3.01 | 3.24 | 757 | 757 |
| d0 | extra | off | 1 | 25.00 | 27.04 | 2004 | 2010 |
| d1 | extra | on | 1 | 22.25 | 23.39 | 797 | 804 |

Bars, all met: b and c have lower p50 and lower max wall time than a (p50 speedup 5.7x at one thread, 8.0x at four) and p50 and max RSS no higher than a; d1 p50 wall is 0.89 times d0's (limit 1.10) with p50 and max RSS no higher than d0's; every trial stayed under the frozen 4.0 GiB ceiling (highest 2010 MiB); correctness held in every measured trial (`t3` equal to the split-off reference with `check_metadata=True`; `t1` and `t2` equal to the split-off reference in values, types and nullability, with no schema metadata, the passthrough field equal to the source field with `check_metadata=True`, and equal to the split-on reference with `check_metadata=True`; `t1` and `t2` `native_admitted` with no oracle column in b and c, rerouted with `uncovered_columns` in d1; `t3` in the full-frame group). No problems were logged.

Reading RSS: a worker that only builds the sources holds 687,562,752 bytes (`base`) and 791,343,104 bytes (`extra`), so the split-on trials sit 70 to 75 MiB above the source-build floor and the split-off trials 1.1 to 1.2 GiB above it. The memory bars compare split-on with split-off on identical sources, so the shared floor cancels. The measured figures are the numbers above under run `B7-BENCH-2026-10-02-1M`; the sprint and testing ledger row is not written here (the ledger is outside this repo).

## Deviations from the plan

1. **Table gate via `classify_job`.** The plan calls `_planner._chunked_rejection` with the job's `work` and `ordered_work`. `decide_multi_table_split` calls `classify_job` once per table on the config restricted to that table, passing only that table's source (or `{}` when it has none). `classify_job` runs the same `_chunked_rejection` with the same arguments, so the gate is the planner's own, and the dispatched reason and the rejection reason are the single-table job's by construction, instead of copied from a string. The cost is one `build_work_list` per table.
2. **Per-table evidence built in the executor.** The plan's `split_reproducibility_stamp(split, *, chunk_size_rows, auto_chunk_threshold_rows)` has no sources, so it states the six reproducibility keys, and `run_multi_table_split` puts the lane keys and the `tables` list in the partial `auto_chunk` block that `merge_lane_stamp` merges. The result has the shape section 8 describes.
3. The one loosened assertion in my own test was restored after the gate, see the dennis section.
4. **Registry sentry coverage.** Plan test 2's registry sentry runs two calls per fixture for 23 of the 24 `SCALAR_HANDLERS` strategies (the chunk-admitted fixtures plus new fixtures for `shuffle`, `categorical` and `nested` unseeded variants, `formula`, `derived`, `derived_aggregate`, `grouped_series`, `joint_mask`). `geo_generalize` has no fixture because its `h3` dependency is not installed here. A source scan test over `execution/_strategies` (any unseeded `default_rng()`, `random.Random()`, global `random` call or `uuid4`) pins that only the `categorical` and `shuffle` modules draw from an unseeded generator, so a new unseeded path fails CI either way. The runtime sentry fails if a registry strategy has neither a fixture nor an entry in `UNFIXTURED`.
5. **Mutation tooling.** mutmut was not run; see Mutation.
6. **Docs outside this repo.** The roadmap and shipped log (`decoy-platform/docs/ROADMAP.md`) and the sprint and testing ledger are not updated by this build.
7. **Thread test needs the companion.** The kernel-level thread-budget assertions (`derive_batch` receives `native_threads`) skip when the companion is not installed, as B2's do. The budget forwarding to `run_auto_chunk` and the no-overlap checks run everywhere.

## Notes for the gate

- A split call reports `auto_chunk.mode == "chunked"` even when some tables ran full-frame, so the platform's route label reads `legacy_chunked`. This is a known issue in the plan (B3 reads `auto_chunk.tables`).
- The group's `adapter.run` still sees plan nodes for dispatched tables and skips them because it has no frame for them. Tests 4 and 5 pin that the group output equals the split-off output.
- `explain_plan` does not show the per-table split, as the plan says.

## dennis gate and dispositions

Verdict GO with fixes: 0 blocker, 0 high, 3 medium, 3 low. All six applied in this worktree.

| Finding | Disposition |
|---|---|
| MEDIUM-1: warning assertion loosened to `>= 2` | Restored to exactly four `fpe_join_group_active` warnings. `fpe_a` and `fpe_b` now share one join group and namespace and the same column names, so two warnings share a key; a `Counter` of keys shows count 2 for each key in both the split and the split-off run. No table-agnostic warning that both a dispatched unit and the full-frame group can emit was found: a `fpe_join_group` table is always rejected by the chunked gate, so it only runs in the group, and deterministic Faker tables with a two-entry pool emit no warning on either route. There is therefore no cross-unit duplicate case to add; the duplicate-kept-twice contract is pinned across two group tables. |
| MEDIUM-2: false "cannot disagree" comments | The comment in `_pipeline.py` and the `decide_chunk_route` docstring now say `execution_plan` is the static job-level classification and a split is reported in `auto_chunk.tables`. No behavior change; `_pipeline.py` stays at 679 lines. |
| LOW-1: no Design 8 log line | `run_multi_table_split` logs one INFO line naming the dispatched tables and the full-frame group (names only). A caplog test pins the exact line and that no value appears. |
| LOW-2: try/except shape in the relationships test | The test now asserts the actual outcome: the job succeeds, never enters the split, and equals the split-off run. |
| LOW-3: pool and corpus loading contract | The module docstring states that per-unit pool and code-set loading relies on the S5 F2 deterministic pool-build contract. |

Re-run after the fixes: the five B7 test files, `test_auto_chunk_dispatcher.py`, `test_auto_chunk_routing.py` and `tests/sentry`: 2652 passed, 1 skipped. `ruff check`, `ruff format --check` and `mypy src/decoy_engine testflight` are clean.

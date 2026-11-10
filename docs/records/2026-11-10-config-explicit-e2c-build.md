# CONFIG-EXPLICIT E2c build record

Status: record

Slice: roadmap row 22. `profile_source`, `run_pipeline` and `run_pipeline_isolated` refuse an
undeclared source column (`source_file.undeclared_column`) and a source with no `tables` entry
(`source_file.undeclared_table`) before any row is read. Branch `feat/config-explicit-e2c-run`,
merged with main `b63c0fb9`. Source change is about 55 added lines in 4 files; tests carry the rest
of the diff (76 test files).

## What changed in code

- `profile/_source.py`: `profile_source` takes a keyword-only `declared`; the gate is its first
  statement.
- `execution/_pipeline_context.py`: `_run_schemas` plus the gate, after the F06 preflight and F13
  admission and before the first reader and `refuse_duplicate_run_sources`. File is exactly 600 lines.
- `execution/_pipeline.py`: `run_pipeline(source_loader_schemas=...)`; a loader and its schemas go
  together, otherwise `ValueError`.
- `execution/_isolated_run.py`: parent gate after `_require_ambient_for_paths` (659 lines, pin 672).
- Tests: `tests/unit/profile/test_declaration_gate.py`, `tests/sentry/source_config/test_declaration_gate.py`
  (AST ordering check, READERS allowlist, seeded mutation).
- Docs: `docs/compatibility-contract.md`, `CHANGELOG.md` (Breaking for consumers), `CODEMAP.md`.

## Fixture churn

Fixtures only add column declarations. Helpers declare once through `tests/_declared_schema.py`
(`declare_rest`, `file_schemas`); shared builders were changed in place rather than every call site.
Loader callers pass `source_loader_schemas`. The E2a codemod was not touched.

## Flip table

Each row: the old state cannot be reached any more because the gate refuses it first.

| File::test | Old assertion | New assertion | Why the old state is unreachable |
|---|---|---|---|
| `tests/parity/test_ooc_temporal_ns_range.py::test_unseeded_parent_through_the_pipeline_matches_full_frame` | parent key absent from config, run completes equal to full-frame, forced gate raises `out_of_core_parent_seed_missing` | every gate/mode cell raises `source_file.undeclared_column` | a column missing from the config is refused. A declared passthrough key is in the plan and out-of-core accepts it, so "seed missing" cannot occur via `run_pipeline`. The route-level check stays covered by `test_unseeded_parent_never_reaches_the_out_of_core_join`. Parity is kept by the new `test_a_declared_passthrough_parent_key_matches_full_frame` |
| `tests/unit/execution/test_multi_table_contract.py::test_split_job_output_contract[extra_caller_source-*]` | split output equals the off run | run raises `source_file.undeclared_table` | a supplied table with no `tables` entry is refused |
| `tests/unit/execution/test_b6a_split_worker.py::test_a_split_with_a_resident_group_or_extra_frame_stays_resident[extra_resident_frame]` | stays resident, reason `split_extra_sources_present` | raises `source_file.undeclared_table` | same |
| `tests/unit/execution/test_b6a_split_worker.py::test_the_split_executor_refuses_a_sink_it_cannot_honor_before_masking[extra_resident_frame]` | removed case | none | a declared extra frame takes the `split_full_frame_present` path, which the `full_frame_small` case already covers; `split_extra_sources_present` cannot occur. The defence-in-depth executor refusal stays covered by `full_frame_small` |
| `tests/unit/execution/test_auto_chunk_routing.py::TestFailClosed::test_extra_loaded_source_table_stays_full_frame` | full-frame route, extra output echoed | raises `source_file.undeclared_table` | undeclared loaded table |
| `tests/physical/test_shadow_full_frame.py::test_extra_resident_source_preserved_and_projection_warning_parity` | oracle run equals shadow outputs and projection warnings | shadow and direct-adapter parity kept; oracle `run_pipeline` raises `source_file.undeclared_table` | the oracle run is a gated run |
| `tests/unit/execution/test_auto_chunk_dispatcher.py::test_unconfigured_column_is_admitted_to_the_native_route` | `"extra" not in backends` | `backends["extra"][0] == "arrow_python"` | the column is a declared passthrough and has a lane |
| `tests/unit/execution/test_multi_table_units.py::test_the_declaration_gate_refuses_before_the_group_call` | (new flip) fixture strips the `extra` column entry | raises `source_file.undeclared_column` | fixture-only change |
| `tests/physical/test_unified_slice_admission.py::test_cheap_admission_declines_undeclared_passthrough_column` | admission declines a config that omits a column | same, but profile with the declared config first, then strip the column and call the admission check directly | the gate refuses the config before a real run; the check stays a second guard |
| `tests/unit/execution/r3_within_route_baseline.json` (`ff_generate_mask`) | two `undeclared_output_columns` warnings | `warnings: []` | the columns are declared, so no projection warning. Only this key changed |
| `tests/acceptance/test_f06_route_parity.py::_int_only` | CSV kept `start` | CSV rewritten without `start` | config and file must list the same columns |
| `tests/unit/execution/test_engine_transforms_exact.py::TestExactViewShapes::test_neutral_transform_equals_no_transform` | `len(seen) == 2`, `seen[0] == seen[1]` | `len(seen) == 2 * nodes`, `seen[:nodes] == seen[nodes:]` | see below |

### Why `seen` grew in `test_engine_transforms_exact`

The spy fires on `PandasExecutionAdapter._dispatch_mask_node`, once per mask node. The fixture used
to configure one column; the other source columns were unconfigured and had no node. Now every source
column is declared, as passthrough, and each is a node, so each run dispatches once per declared
column. The gate does not validate or profile twice: the test asserts `len(seen) == 2 * nodes` where
`nodes` is the number of declared columns, and the two runs still see identical frames. No code change.

## Not changed by design

DE-03 (the undeclared output-column policy) stays below the gate as defence in depth.

## Test results

`scripts/dev/gates.sh --keep-going --baseline` on a Linux VM, selection of the 1067 test files changed
against origin/main plus the always-on pins (not the full suite). Every step passed, so the baseline
rerun on main was skipped:

- env, ruff, format, mypy (`src/decoy_engine testflight`): pass.
- sentry: 4781 passed, 0 failed.
- licenses: 63 passed, 0 failed.
- tests: 35469 passed, 0 failed, 5335 skipped.

An earlier full-suite run on a snapshot from before the last fixture fixes had 7 failures; six were
fixture gaps fixed in the same branch. `test_determinism_goldens::TestGoldenGateCoverage` failed only
inside that xdist run and passes in isolation on both main and this branch (485 passed with
`test_f06_route_cells.py`); it did not fail in the gates run. The coordinator runs the full suite
against a fresh baseline.

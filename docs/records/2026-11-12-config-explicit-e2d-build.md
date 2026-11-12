# CONFIG-EXPLICIT E2d build record

Status: record

Slice: roadmap row 22. `run_mask_pipeline_chunked`, `run_mask_chunked` and
`run_native_or_oracle_chunked` refuse an undeclared table or column
(`source_file.undeclared_table`, `source_file.undeclared_column`) before any chunk is profiled,
compiled or masked. Branch `feat/config-explicit-e2d-chunked`, based on main `c96e2855`
(E1 #409, E2a #441 and E2c #471 are ancestors). Not stacked: E2c is already on main.

## What changed in code

- `execution/_chunked.py`: `run_mask_pipeline_chunked` takes the required keyword-only
  `source_schema` and calls `prepare_declared_sources(DeclarationRequest.for_chunks(...))` after
  the subset refusal and before `_oracle_preflight`. File is 600 lines (was 590), so no census entry.
- `execution/_chunked_oracle.py`: `_oracle_preflight` and `_oracle_preflight_admitted` take a
  required `declared`. Right after the first chunk is pulled and the duplicate-field refusal, the
  rest of the stream goes through `declared.checked_stream(...)`: the first chunk is checked now,
  later chunks lazily before each masks. Nothing is profiled, compiled or wrapped before it.
- `execution/native/_chunked_entry.py` and `native/_dispatch.py`: `run_mask_chunked` and
  `run_native_or_oracle_chunked` prepare the config-level capability (`for_chunks(config, table,
  None)`) and pass it to `_run_chunked`, which requires `declared`. Signatures of the two public
  functions do not change.
- `_pipeline_auto_chunk._run_legacy` passes `source_schema=source.schema`;
  `MaskPipelineChunkedAdapter.run` takes and forwards `source_schema`.
- `profile/_declared_columns.py`: `DeclarationInput.within` and the `source_schema` columns kept by
  `PreparedDeclaredSources`, so a chunk column outside the caller's `source_schema` is refused with
  `source_file.undeclared_column`; `checked_stream(table, first, rest)` is the one place that turns
  a pulled first chunk into names (`arrow_columns`, which drops a stored pandas index).
- Docs: `docs/compatibility-contract.md` (section 3.4), `changelog.d/config-explicit-e2d.md`.

## Deviations from the handoff (re-anchored on main)

1. E2a's `PreparedDeclaredSources` did not keep the `source_schema` columns or check chunks
   against them (the handoff describes it as doing so). E2d adds `within` and the schema memory in
   `profile/_declared_columns.py`, a file outside the E2d Files list, because AT-E2d-2 needs it.
2. `checked_stream` is new. The handoff calls `arrow_columns` and `check_names` from
   `_chunked_oracle.py`; `arrow_columns` is internal to the profile package, so the oracle goes
   through one public method instead of a private import (defect class 6). The sentry looks for
   `checked_stream(` instead of `arrow_columns(first.schema)` plus `check_names(`.
3. Fixture churn is larger than the handoff estimate. About 130 call sites in 27 test files
   (not 30 in 11) assert how an unconfigured or undeclared column behaves on the entries, so they
   call the layer below the gate. The codemod `source-schema` covered 154 calls; hand rows and
   entry-by-value call sites were fixed by hand. See "Retargets".
4. `tests/native/_b8_support.py` holds the below-gate helpers
   (`run_mask_chunked_below_gate`, `run_native_or_oracle_chunked_below_gate`,
   `run_mask_pipeline_chunked_below_gate`). The pipeline entry has no private body, so its helper
   answers the gate call with a real capability for the duration of the eager call.
   `tests/_declared_schema.py` gains `chunked_entry_kwargs` for tests that loop over the entries.
5. The `declare-columns` probe was run over the failing set; its frames named only helper files
   and B8 tests, so no config literal was edited by the codemod. Tests whose config was merely
   incomplete (dup-name matrix, categorical weights) got hand-written passthrough declarations.

## Retargets (assertions unchanged)

- Below the gate: `tests/native/_b8_support.py` `run_one`; `_rev9_support.py` `run_entry`,
  `run_public`; `_c8_i_support.py`; and the direct calls in `test_c8_i_when_*`,
  `test_chunked_entry_{evidence,rev7,rev8,rev9_adapter,rev9_profile,rev9_read,rev9_shapes,
  side_channels,values_schema}`, `test_chunked_group_key_admission`, `test_chunked_nondet_*`,
  `test_chunked_categorical_admission`, `test_c6b_i_text_mask_chunked`,
  `test_c8_iii_d2_native_positional_when`, `test_column_access_surfaces`,
  `test_composite_admission`, `test_native_dispatch`, `test_unconfigured_passthrough{,_read}`,
  `tests/security/test_fk_key_value_free_errors.py`, `tests/unit/execution/test_chunked.py`
  (unknown table: the compatibility check lives below the gate) and
  `test_chunked_mutation_kills.py` (DE-03 stays as defence in depth).
- Declared the missing columns or sources: `test_duplicate_field_names_matrix.py`,
  `test_categorical_weight_admission_routes.py`, `test_bucket_perturb_chunked.py` and
  `test_code_set_chunked.py` (the parent-only config also drops the child source).
- Pass `source_schema` only: the adapter tests, `test_c8_iii_c_boundaries`,
  `test_derived_route_matrix`, `test_subset_run_refused`, `test_engine_transforms_{exact,routes}`,
  `tests/fileaccess/test_policy_routes.py`, `tests/secret_providers/*`.

## Red first

Stub step: `source_schema` accepted and ignored, `_oracle_preflight` not yet gated. The new
tests in `tests/unit/profile/test_declaration_gate.py` then gave 12 failed, 22 passed:
undeclared table (3 entries), undeclared `source_schema` (pipeline), undeclared first-chunk column
(3), column first appearing in chunk 3 (3), a chunk column outside `source_schema`, and the legacy
auto-chunk lane. `test_chunked_requires_source_schema` passes on the stub because the keyword is
already required there; on the E2c base it fails with `DID NOT RAISE` (the call succeeds).

## Mutations (head 87e2430b, each reverted)

| Mutation | Red test |
|---|---|
| drop the `checked_stream` call in `_oracle_preflight_admitted` | first-chunk and chunk-three cases (all entries), outside-`source_schema` case, both sentry walks |
| move that call below `compile_plan` | first-chunk cases (all entries), both sentry walks |
| give `source_schema` a default | `test_chunked_requires_source_schema` |
| drop the `within` intersection | `test_pipeline_entry_refuses_a_chunk_column_outside_source_schema` |
| `first.schema.names` instead of `arrow_columns` | `test_fixed_schema_capture_survives` |
| drop the `source_schema` forward in `MaskPipelineChunkedAdapter.run` | `test_chunked_adapter_forwards_source_schema` |
| wrap `chunks` in a generator before `_run_chunked` | `test_fixed_schema_capture_survives` |

## Test results

See the report that came with the push: T1 selection and gates.sh result on the pushed head.

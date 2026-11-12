# Config-explicit E2b-r1: created-column contract and supplied-source profiling

Status: record

Branch `feat/config-explicit-e2b-created` from origin/main `a001f4d7`. Tests ran on Linux VMs through `vm-run`; no native companion is installed there. `tests/sentry/layout/test_import_layers.py` needs import-linter, which the vm-run venvs lack, so it is left out of every count (hosted CI runs it).

## Red runs

The new APIs do not exist on the base, so a base run is an import error, not an assertion failure. Red evidence is therefore the mutation runs below, each an assertion failure on the final tree.

## Green runs

- Contract file, `tests/sentry/source_config`, `tests/native/test_column_access_surfaces.py`: 234 passed.
- Supplied-only cells cover the five strategies; `derive` is excluded because the transform builds its column from the supplied frame regardless of the profile (it stays green under the no-`supplied_sources` mutation, so it could never be red).
- Final-head runs: contract file, source_config sentry and column-access surfaces 234+ passed; impact run (tests/unit/execution, tests/unit/profile, source_config, surfaces, 25 files calling profile_source or build_run_context): 8723 passed, 1 failed (`TestMemCapOom`, passes alone: memory-cap test under -n 4).

## Mutations (assertion failures, reverted)

| Mutation | Test that went red |
|---|---|
| mark `faker` created | `test_created_target_descriptor_is_entry_sensitive` (creating set differs) |
| `group_key` row reads nothing | same test, `('group_key', 0, 'tgt')` |
| restore `source_slice=batch.select([column])` | `test_created_target_route_matrix[group_key-native]` |
| stop passing `supplied_sources` | `test_supplied_only_table_gets_its_created_column[...]` |
| (override mutation dropped: see Named limits) | n/a |
| add `derived` to the chunk-admitted set | `test_created_target_route_matrix[derived-chunked]` |
| scratch handler with no declaration | inventory test fails inside the descriptor test |

## Size

Hand-written additions about 494 plus docs and this record; the coordinator accepted the overrun. `_pipeline_context.py` stays at 596 lines.

## Named limits

- `own_target` defaults to `source` (fail-closed) rather than being mandatory.
- A created column that reads another created column is not exempt.
- `derive` expression reads come from the pandas tokenizer and include function names, so such an expression is not exempt.

## V2-LOG

Function names in a `derive` expression count as column reads (`execution/_column_access.py`, `_transforms`).

## Not verified

Mac run; X-id behavior under the reverse rule (E2b-r2).

## Deviation from the plan: descriptor wins

The plan has a supplied table replace a same-name descriptor. `test_m1_resident_smaller_than_exact_profile_warns_and_routes_on_resident` pins that the profile reads the descriptor's file while the resident table is a pre-filtered view, and routing warns on the disagreement; `test_full_frame_value_unrepresentable.py` supplies tables whose values pandas cannot hold. Replacing the descriptor breaks all three ids, and the stop rule forbids editing them. So only a supplied table with no descriptor is profiled from the supplied data. The override case in AT-E2b-6 is therefore not pinned; E2b-r2 should rule on it.

# Config-explicit E2b-r2: declared-but-missing refusal build record

Status: record

Branch `feat/config-explicit-e2b-reverse`, stacked on #514 (`feat/config-explicit-e2b-created`, merged forward at `66bc0751`). Tests ran on Linux VMs through `vm-run` (`VMRUN_LINUX=1`). The VM venvs lack import-linter, so the 10 failures in `tests/sentry/layout/test_import_layers.py` are expected.

## Runs

- Red: the tests-only commit (registry plus tests, no behavior) gave 73 assertion failures over the 11 touched files. A second red commit (when and registry cells) gave the two `TestWhenRejection` X ids and `test_a_when_keeps_the_entrys_own_target_claim` red. Fix round 1: five new tests red before the source change (`test_direct_profile_source_applies_only_the_forward_rule` and four derive-chain tests).
- Green (fix round 1 head, see the SHA below): `tests/unit/profile unit/execution unit/scripts`, `tests/physical native sentry`: 10 failed (the 10 import-layer ids, expected on nodes without import-linter), 20004 passed, 3924 skipped.

## Mutations (fix round 1, SHA 04f1f6bf, each in a throwaway detached worktree, assertion failures)

| Mutation | Red test |
|---|---|
| no missing stage in `declaration_findings` | `test_declared_column_missing_from_source_refused` |
| `check_names` returns early | `test_chunked_refuses_a_later_chunk_missing_a_declared_column` (3) |
| `_creates_from` ignores `reads <= have` | `test_created_target_self_reference_refused` (22) |
| codemod without the `missing` branch | `test_missing_record_is_hand` |
| `_with_created` exempts nothing | `test_created_target_route_matrix` (24) |
| `_with_when` drops the own-target keep | `test_a_when_keeps_the_entrys_own_target_claim` |
| `run_pipeline_isolated` skips the boundary | `test_descriptor_routes_refuse_missing_declared_before_rows[isolated, isolated_inproc]` |
| D1: `created` defaults to an empty frozenset, not None | `test_direct_profile_source_applies_only_the_forward_rule` |
| D2: drop `\| made` in `_derive_created` | `test_derive_reading_an_earlier_derive_target_counts_as_created`, `test_derive_chain_with_a_masked_target_runs_and_masks_it` |

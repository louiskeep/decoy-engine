# Mutation grading: `execution/_mem_estimate_schema.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`). `_mem_estimate_schema.py` holds
the adapters that build a `TableSizeSpec` from the engine's schema types:
`table_size_spec_from_profile` (mask tables, from `TableProfile`) and
`table_size_spec_from_generate_table` (generate tables, from `TableConfig`). Not
crypto/RI, so the bar is 75%.

## Numbers

**Re-graded (`scripts/tq_mutate.py`): 118/120 killed = 98.33% LOGIC, 0 unresolved.**
Baseline was 108/120 (90%); this sweep killed 10 of the 12 survivors.

| Function | Mutants | Killed | Survived |
|---|---|---|---|
| adapters (both) | 120 | 118 | 2 |

## Killed this sweep (name + column-completeness gap)

The pre-sweep tests asserted per-column WIDTHS / `unpriceable` on single-column
tables, so they missed two mutation classes:
- `ColumnSizeSpec(name=col.name, ...)` / `TableSizeSpec(name=table.name, ...)` ->
  `name=None` (9 mutants across every branch of both adapters): the spec's
  column/table NAMES were never asserted.
- `continue` -> `break` in the generate adapter's numeric branch (1 mutant): a
  single-column table can't observe an early loop exit.

Killed by two multi-column, multi-branch oracles in
`tests/unit/execution/test_mem_estimate.py` (`TestMemSchemaNamePreservation`):
`test_from_profile_preserves_all_names_across_every_branch` (id/email/city/notes
spanning fixed-width / declared-width / sample-width / unpriceable, asserting
every `ColumnSizeSpec.name` + `TableSizeSpec.name`) and
`test_from_generate_table_preserves_names_and_emits_all_columns` (numeric column
first so `break` would drop the trailing columns; asserts all names + full column
list).

## EQUIVALENT (2)

`table_size_spec_from_generate_table` mut_3 / mut_4: the `row_count is None`
`ValueError` MESSAGE text (an `XX`-wrap and a case change). The raise itself and
the `ValueError` type are pinned by the pre-existing
`test_table_size_spec_from_generate_table_requires_row_count`
(`pytest.raises(ValueError)`); only the human-readable message differs, so these
are message-prose equivalents (kill via machine fields, not prose -- policy).

## Candidate findings

None. 0 product bugs.

## Regenerate

`[tool.mutmut]` `only_mutate = ["src/decoy_engine/execution/_mem_estimate_schema.py"]`,
`pytest_add_cli_args_test_selection = ["tests/unit/execution/test_mem_estimate.py", "tests/unit/execution/test_byte_estimate_routing.py"]`,
then `python scripts/tq_mutate.py --run`.

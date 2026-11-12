Breaking: `PLAN_VERSION` is 4 (was 3). A plan this engine writes carries `plan_version: 4`, and an older engine does not check the version at load: it loads the plan and fails when a run reaches a `lookup` column. This engine still loads version 2 and 3 plans. A plan with no `lookup` column differs from its version-3 form only by the version line.

### Added (`lookup` generate type, GEN-TYPES E3, 2026-11-12)

`type: lookup` draws each row's value from a list kept in a file or shipped with the engine. Give
exactly one of `file` (a `.csv` or `.parquet` path) and `codeset` (one of the shipped code sets:
`hcpcs`, `icd10`, `mcc`, `ndc`). `value_column` names the column that holds the values (the first
column for a file, `code` for a code set) and `weight_column` names an optional column of weights.
CSV cells are read as exact text, so `NA`, `null` and `007` stay as written; Parquet keeps string,
integer, float and bool values. The draw is the same weighted `random.Random(...).choices` call
`categorical` makes, `null_probability` and `output_type` apply as on other types, and `unique` is
refused. A `lookup` column is resident only: a streamed run declines it, and a pinned compile
refuses it (`pinned_lookup_unsupported`).

The file is read once at compile time and its bytes are stored in the plan under `generation.lookups`
with a sha256, like a statistical snapshot. A run never reopens the path, so moving or deleting the
file after compile changes nothing, and the seed hashes the file's content digest instead of its path.
A saved plan re-checks every stored digest and re-runs the lookup checks on load; a plan that lacks a
configured lookup, or whose bytes were edited, is refused. Code sets are versioned with the engine.

New reasons, all in the `plan` namespace and `since` unreleased: `lookup_not_pinned`,
`lookup_pinned_digest_mismatch`, `lookup_file_unreadable`, `lookup_file_format_unsupported`,
`lookup_file_empty`, `lookup_file_too_large`, `lookup_column_missing`, `lookup_too_many_entries`,
`lookup_weight_invalid` and `pinned_lookup_unsupported`. A refusal names the table and column and
never a cell, a weight or a path. `FileRole.LOOKUP` is the role a lookup file is read under, and
`decoy_engine.profile.read_csv_bytes` is the one CSV reader for bytes already in memory.

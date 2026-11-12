### Added (per-parent aggregates, GEN-REL E3b, 2026-11-12)

A generated `derived_aggregate` column can now aggregate each parent row's own children.

- New options `from_table` and `via`: `from_table` names a child table and `via` the child's
  `reference` column back to this table; `column` is then a column of the child. Each parent
  row gets `op` (`sum`, `count`, `mean`, `min` or `max`) over the children whose foreign key
  names it, as SQL `GROUP BY` over the child with a `LEFT JOIN` from the parent. A same-named
  column of the parent table is never read. Null values are skipped and a child with a null
  foreign key belongs to no parent.
- A parent with no children gets 0 for `sum` and `count` and a null for `mean`, `min` and `max`,
  never NaN. The column is filled after every table exists and the returned parent table holds
  the filled values; a declared `output_type` applies to them.
- `via` must be a `reference` to this table whose key is an earlier `sequence` column with no
  `null_probability` (`config/generate_parent_lookup_key_not_unique`); a `via` through a
  self-reference is refused (`config/generate_parent_lookup_self_reference`). `via` without
  `from_table` is an unknown option, and `from_table` without `via` is missing.
  `null_probability` on the aggregate is refused (`config/generate_per_parent_null_conflict`).
- Any column input, child `anchor` or `reference` that names a per-parent aggregate is refused
  at validate (`config/generate_aggregate_read_before_fill`): it would read the column before it
  is filled.
- A per-parent aggregate runs in memory on the whole-table route, as every `derived_aggregate`
  does. Pinned mode refuses it with the existing `pinned_relational_option_unsupported` codes at
  compile and at execute.
- The option registry declares `derived_aggregate.column` and `derived_aggregate.via` as inputs
  of the `from_table` table, read after it is filled. Numeric typing and the reproducibility
  report read the child's column and foreign key: an unrelated numeric parent column of the
  same name keeps its numeric type, and the aggregate is not reported reproducible when its
  child column or foreign key is not.
- A generated `derived_aggregate` whose `column` is missing is now refused at plan compile with
  `generate_column_forward_reference`, the same code as an input generated later.
  `derived_aggregate_missing_column_ref` remains the refusal of a masked column.
- `decoy_engine.config` exports `aggregate_problems` and `is_per_parent_aggregate`.

Every config without `from_table` keeps its bytes.

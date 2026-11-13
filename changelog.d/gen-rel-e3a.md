### Added (parent-anchored dates and coded date failures, GEN-REL E3a, 2026-11-12)

A generated `windowed_date` column can now be anchored to a date of its parent row.

- New option `anchor_via` names an earlier `reference` column of the same table; `anchor` is
  then a column of the table that reference points at. Each child date is the parent row's
  anchor plus the usual `min_days` to `max_days` offset. A same-named column of the child table
  is never read. A null foreign key gives a null date. The same seed gives the same dates.
- The lookup column must be a `reference` to another table whose key is a `sequence` column
  with no `null_probability` (`config/generate_parent_lookup_key_not_unique`); a reference to
  the column's own table is refused (`config/generate_parent_lookup_self_reference`).
- At run time a foreign key with no parent row is refused
  (`runtime/parent_lookup_key_missing`), as is a parent key that occurs twice
  (`runtime/parent_lookup_key_not_unique`); both are `GenerationError`. A parent row whose
  anchor is null is refused, not nulled (`runtime/windowed_date_parent_anchor_null`).
- Every `windowed_date` failure, generated or masked, now has a registered code and a fixed
  message with no row position, type name or cell value: `runtime/windowed_date_anchor_invalid`,
  `runtime/windowed_date_anchor_out_of_range` and `runtime/windowed_date_offset_out_of_range`.
  `WindowedDateError` carries the code and is reported under it instead of as an unexpected
  error; the two range failures are subclasses of it.
- A generated `windowed_date` column whose `anchor` is missing is now refused at plan compile
  with `generate_column_forward_reference`, the same code as an anchor generated later.
  `windowed_date_missing_anchor_ref` remains the refusal of a masked column.
- An `anchor_via` column runs in memory on the whole-table route. Streaming admission declines
  it, and pinned mode refuses it with the existing `pinned_relational_option_unsupported` codes
  at compile and at execute.
- Which column each generate option reads is now declared once in the option registry and
  resolved by `decoy_engine.config.iter_generate_input_refs`. The plan check, numeric typing and
  the reproducibility report use those resolved inputs, so an input in a parent table is
  judged against the parent column: an unrelated numeric child column of the same name keeps
  its numeric type, and a child date is not reported reproducible when its parent anchor or its
  foreign key is not. A `derived` expression's inputs are the columns it reads; a column name
  that only appears inside a string literal is no longer counted as an input.
- `decoy_engine.config` exports `GenerateInputRef`, `iter_generate_input_refs`,
  `parent_key_problem` and `parent_lookup_problems`. An input read through a foreign key
  carries its parent table and the parent key column (`GenerateInputRef.source_key`).
- `decoy_engine.expressions.column_refs` returns the column names a compiled expression reads.
  The derived-column plan check, the generate-input resolver and numeric typing all use it; the
  private `transforms.derived._get_column_refs` is removed.

Every config without `anchor_via` keeps its bytes.

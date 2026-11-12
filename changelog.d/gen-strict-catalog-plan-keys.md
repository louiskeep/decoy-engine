### Changed (generate option catalog and schema list plan-checked keys, GEN-STRICT, 2026-11-11)

`generate_option_catalog()` lists `derived_aggregate`'s `op` and `column` as required, and the new
`GenerateTypeOptions.required_at_plan` names the required keys that plan compile checks instead of
config parse. The registry holds them as `TypeOptions.plan_required`, so the catalog, the closed
column schema and the `docs/strategies.md` table test read one list. Config parse is unchanged: a
missing `op` or `column` is still refused at plan compile with `derived_aggregate_op_missing` or
`derived_aggregate_column_missing`. The column schema also allows `pooled` only on `faker`
columns (other types may carry `pooled: null`), matching what config parse accepts.

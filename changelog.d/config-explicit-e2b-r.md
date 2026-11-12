Breaking: a column the config declares for a mask table but the source lacks is now refused before any row is read; remove the entry or fix the name.

### Changed (declared-but-missing columns, CONFIG-EXPLICIT E2b-r, 2026-11-12)

The declaration gate refuses a column the config declares for a mask table that the source does
not have (`source_file.declared_column_missing`, registry unreleased), from the header, footer or
schema and before any data row is read. It covers every route the E2c and E2d gates cover.
`run` used to fail late on these configs with a bare `KeyError`.

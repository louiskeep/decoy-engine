### Added (exact column facts, POSTSCAN-STREAM PS-5, 2026-11-12)

`decoy_engine.profiling.column_distinct` counts the distinct non-null values of one column read as
Arrow batches. The post-scan column suite gains `column_facts`, which computes identity counts,
`bytes_identical`, distinct counts and detector rates over batches and equals the whole-table
pandas values for any batch layout. Nothing reads these facts yet.

#### Changed (POSTSCAN-STREAM PS-5, 2026-11-12)

On the streaming column suite, residual-PII digit detectors (zip, NPI and the like) can now report
on integer columns that hold nulls. Such a column is read as whole numbers ("10005") instead of
floats ("10005.0").

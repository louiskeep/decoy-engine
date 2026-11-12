### Added (chunked progress events, RESULT-E3b, 2026-11-12)

`run_mask_pipeline_chunked` and `run_mask_chunked` now report a `mask` phase for their table on the active progress channel: it starts when the stream is first pulled and finishes with `rows_out` equal to the rows the caller received. Closing the stream early after a good chunk finishes the phase; a producer error leaves it open for the run's own scope to close as an error. Before this the oracle entry reported nothing and the native entry only reported rate samples.

A table that fails while loading its source now reads `failed` in the run record's collector facts instead of `not_run`, because its `mask` phase starts before the load. The sequential route's start event no longer carries `rows_in` (the finish event still does). The out-of-core route now reports `rows_out` on every table's `mask` finish, so tables on the reorder driver have a row count.

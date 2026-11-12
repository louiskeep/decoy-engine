Breaking: `run_mask_pipeline_chunked` requires the keyword `source_schema`, and the three chunked entries refuse an undeclared table or column before masking.

### Changed (CONFIG-EXPLICIT E2d: declaration gate on the chunked entries, 2026-11-11)

`run_mask_pipeline_chunked`, `run_mask_chunked` and `run_native_or_oracle_chunked` refuse an
undeclared table or column (`source_file.undeclared_table`, `source_file.undeclared_column`) before
any chunk is profiled, compiled or masked. `run_mask_pipeline_chunked` takes the required keyword
`source_schema` and checks it before it touches `chunks`. The native entries check the config at
entry and the first chunk's columns right after it is read; later chunks are checked before each
masks. No registry code is added.

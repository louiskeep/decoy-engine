Breaking: a source column or table the config does not declare is now refused by `profile_source`, `run_pipeline` and `run_pipeline_isolated` before any data row is read; list such columns as `passthrough`.

### Changed (CONFIG-EXPLICIT E2c: declaration gate on profile, run and isolated run, 2026-11-10)

`profile_source`, `run_pipeline` and `run_pipeline_isolated` refuse a source column the config does not
declare (`source_file.undeclared_column`) and a source with no `tables` entry (`source_file.undeclared_table`)
before any data row is read, staged, profiled or masked. The check reads headers, footers and schemas, and
covers supplied tables and loader tables. `run_pipeline` gains `source_loader_schemas`, required with a
`source_loader` and refused without one. `profile_source` gains the keyword-only `declared`. A config that
relied on the unconfigured-column `warn` pass-through now lists those columns as `passthrough`. No registry
code is added.

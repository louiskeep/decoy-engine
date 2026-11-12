Breaking: `run_sequential` and `run_fk_out_of_core` take a required keyword-only `config`; pass the raw pipeline config

### Added (post-scan route guard, SCAN-ML M5, 2026-11-12)

The entries that mask without running the post-scan (`run_sequential`, `run_fk_out_of_core`,
`run_mask_pipeline_chunked`, `run_mask_chunked` and `run_native_or_oracle_chunked`) refuse a config
that turns on `post_scan.integrity` and asks for a model post-scan detector or for
`ner_findings: fail`. The refusal comes before any source is read, as the new public
`PostScanRouteError` with reason `route.post_scan_models_unsupported` (`since` unreleased). Its text is
the fixed registry summary and it carries no exception chain. Regex-only `integrity: true` keeps
its behavior on these routes, and `run_pipeline` is unchanged: its resident auto-chunk lane runs the
post-scan itself. `run_sequential` and `run_fk_out_of_core`, and the wrappers that forward to them
(`PandasExecutionAdapter.run_sequential`, the physical sequential and out-of-core adapters), now
require a keyword-only `config`, so a direct caller cannot leave out what the guard reads.

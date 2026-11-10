### Added (record library, engine side, LIB-L1a, 2026-11-10)

`decoy_engine.records` holds the envelope the CLI's local record library and the platform importer share: `build`,
`encode` and `decode` for `decoy-record/v1`, one canonical JSON form and content-addressed ids, a kind registry
(`evidence`, `run-result`, `sample-run/v1`, `scan-set`; `publication` and `checkpoint` reserved), the
`decoy-library-export/v1` codec and the `decoy-job-log/v1` header and reader. A record of an unknown kind or a later
major decodes to an opaque record or `RecordNeedsNewer` instead of an error. Seven reasons join the registry at
1.20.0: `runtime.record_invalid`, `runtime.record_corrupt`, `runtime.record_kind_reserved`,
`warning.library_write_failed`, `warning.job_log_write_failed`, `warning.library_degraded` and
`warning.library_delete_skipped`.

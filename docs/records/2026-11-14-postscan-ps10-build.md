# POSTSCAN-STREAM PS-10 build record: the gate reads the staged sink

Status: record

Plan: `docs/plans/2026-11-12-postscan-stream.md` on `plan/postscan-stream` (4.3, 4.5, 4.6, 4.7; AT-15, AT-16, AT-20, AT-27 to AT-29). Branch `feat/postscan-ps10`, cut from `origin/main` 81dbfe00.

## What was built

- `ParquetTransactionalSink.staged_tables()` and `verification.postmask.from_staged` (schema and rows from the footer, row-group reads through `fileaccess.adapters.arrow.staged_parquet`).
- `PostScanGate.before_commit` judges the staged files; an unreadable sink or `post_scan.integrity` with a staged commit is refused (fail closed). `run_resident` no longer marks a streamed run deferred: the commit reads it.
- `PostScanGate.attach_cast_run` at the three `open_cast_run` sites; the raw-input re-read applies `cast_batch(record=False)` through `with_cast`. Declared casting no longer defers a bounded route.
- Readability: `post_scan_sink_kind`, `post_scan_output_readable`, and `post_scan_resident_reason(config, sink_kind=)` with the new route reason `route.post_scan_keys_resident` (since UNRELEASED). Runtime sites (`resolve_execution_route`, `decide_output_mode`) and the physical compiler pass the sink kind; estimators keep the default.

## Deviations

- `check_rejections_before_preparation` (the pre-preparation mirror) keeps the default readable sink: it only decides what to refuse before transforms run, and the real routing refuses the same job one step later.
- The streamed full-frame lane is covered by an `OutputPublish` unit cell, not an end-to-end run.
- AT-15 peak-memory is measured on `from_staged` directly; the 8MB spill cell stays with the key-core tests.
- The PS-8 deferral cells for sinks the gate cannot read now assert refusal or resident routing.

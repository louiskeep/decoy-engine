### Changed (loader and transformed tables are checked on the sequential route, POSTSCAN-PS11, 2026-11-14)

A sequential run whose tables come from a `source_loader`, or whose lazy tables declare transforms, now gets the blocking key check instead of the report-only deferral. The gate wraps the route's own loader and copies each key table's key and declared-cast columns, as the route read them (after transforms), into scratch Parquet. The baseline is read from that copy, so the loader is still called once per table and a table filtered or reshaped by its transforms is compared after the transforms. A key the run breaks blocks and commits nothing. The scratch copy is removed when the run ends.

Loader and transform-bearing runs on the full-frame, out-of-core and mixed routes keep the deferral, and their result still carries `quality_metrics["post_scan_gate_deferred"]`.

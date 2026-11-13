### Changed (post-scan gate wired into run_pipeline, POSTSCAN-STREAM PS-8, 2026-11-13)

`run_pipeline` now runs the post-scan gate on the full-frame, sequential, out-of-core and mixed routes before the output becomes visible. When relationships are declared, a primary key that gains duplicates or nulls, or a foreign key that gains orphans the inputs did not have, withholds the run with `PostScanBlockedError`. With `post_scan.integrity: true`, a failing column finding also withholds the run; before this it was only reported. The report lands in `quality_metrics["post_scan_integrity"]` on every route that ran the gate.

Jobs that declare relationships and write to a sink, read inputs through a `source_loader`, or declare casting or lazy transforms stay on the resident route until the staged-output slices land, so an explicit sequential or out-of-core request for such a job is refused. New reason `route/post_scan_keys_resident`. `decide_execution_route` takes `post_scan_resident` in place of `post_scan_integrity`, and `compute_post_scan_integrity` is removed.

A relationship whose parent table is generated treats the orphans its input child rows already carried as inherited, so `preserve` and `warn` runs on generated parents keep passing.

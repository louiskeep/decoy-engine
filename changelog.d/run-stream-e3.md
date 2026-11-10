### Added (route summary, RUN-STREAM E3, 2026-11-10)

`route_summary(quality_metrics)` returns a frozen `RouteSummary` with the route, its reason code
and text, the input and output modes, the configured thread and memory caps, and the values the
route applied: the native thread clamp, the distinct DuckDB threads and memory limits, and the
native status (`admitted`, `not_installed` or `declined: <code>`). A configured cap is never
reported as applied; a route that opened no DuckDB connection or native kernel reports nothing
applied. Missing blocks give `route="unknown"`. The caller passes the cap source labels, since it
chose the caps. It is exported from `decoy_engine`.

The out-of-core route's `quality_metrics["resources"]["out_of_core"]` block also carries
`memory_cap_bytes`, the budget the run was given, whenever the block is written.

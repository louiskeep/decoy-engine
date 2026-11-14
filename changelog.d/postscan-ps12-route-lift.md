### Changed (regex integrity streams, only a model ask stays resident, POSTSCAN-PS12, 2026-11-14)

`post_scan.integrity: true` with regex detectors no longer keeps a run on the resident route. It takes the sequential, out-of-core, chunked and unified-slice routes when they are eligible, and `execution_mode: stream` no longer refuses it. The gate runs the column suite over the staged output before the commit, so a failing finding blocks the run and nothing is published.

A model detector, or `post_scan.ner_findings: fail`, still needs resident values. Those runs stay resident with the new reason `route/post_scan_models_resident`, and stream mode still refuses them. The reason `route/post_scan_integrity_requested` is retired and points at it.

A column finding of severity `fail` now withholds the whole run, as a failed key check does. Before, `integrity: true` only reported it. A sink the gate cannot read (a class other than the engine's own) keeps its route and is delivered through with the gate deferred, so for such a sink the integrity check is report-only and the result carries `quality_metrics["post_scan_gate_deferred"]`.

Sink runs with relationships were held back while the staged gate, the tee and this change landed; with this change every route is gated and the `POSTSCAN-STREAM` hold ends.

### Changed (regex integrity streams, only a model ask stays resident, POSTSCAN-PS12, 2026-11-14)

`post_scan.integrity: true` with regex detectors no longer keeps a run on the resident route. It takes the sequential, out-of-core, chunked and unified-slice routes when they are eligible, and `execution_mode: stream` no longer refuses it. The gate runs the column suite over the staged output before the commit, so a failing finding blocks the run and nothing is published. A `fail` finding withholds the run on every route, as a failed key check does; now it does so also on the streamed routes.

A model detector, or `post_scan.ner_findings: fail`, still needs resident values. Those runs stay resident with the new reason `route/post_scan_models_resident`, and stream mode still refuses them. The reason `route/post_scan_integrity_requested` is retired and points at it.

An explicit integrity check is never run unchecked. With `post_scan.integrity: true`, a sink the gate cannot read back (anything but the engine's own Parquet sink), or a deferred gate while a sink is present, refuses the run before any sink is written or route reads a source, with `runtime/post_scan_integrity_sink_unreadable`. A transform-bearing table on a staged commit refuses before the commit with `runtime/post_scan_integrity_transforms_unsupported`, because its rows no longer line up with the raw source. Integrity with no sink and a deferred gate still runs report-only. Declared keys without integrity keep deferring on such a sink, and the result carries `quality_metrics["post_scan_gate_deferred"]`.

Sink runs with relationships were held back while the staged gate, the tee and this change landed; with this change every route is gated and the `POSTSCAN-STREAM` hold ends.

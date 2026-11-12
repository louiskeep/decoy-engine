### Added (run-result progress collector, RESULT-E3a, 2026-11-12)

`decoy_engine.run_result` exports `RunResultCollector`, a progress sink that keeps per-table counters from the phase and rate events and returns them as `CollectedFacts` through `snapshot()`. Only the `mask` and `generate` phases change per-table facts. A table-less `mask` error marks the whole run, and `resolve` and `publish` are timing only. It never raises from `on_event`. Nothing writes the `decoy-run-result/v1` document yet.

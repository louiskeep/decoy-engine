### Added (run-result progress collector, RESULT-E3a, 2026-11-12)

`decoy_engine.run_result` exports `RunResultCollector`, a progress sink that keeps per-table counters from the phase and rate events and returns them as `CollectedFacts` through `snapshot()`. Only the `mask` and `generate` phases, and rate samples, change per-table facts. A table-less `mask` error marks the whole run unless the route event names a per-table route (sequential or out-of-core). `resolve` and `publish` are timing only. It never raises from `on_event`. Nothing writes the `decoy-run-result/v1` document yet.

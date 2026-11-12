### Added (run result builder, RESULT E2b, 2026-11-12)

`decoy_engine.run_result.build_run_result` builds the `decoy-run-result/v1` document from the per-table projection, the run's quality metrics, an optional `FailureInput` and the caller's exit map. Status and exit code come from `derive_outcome`, and `blocking` is the caller's `blocking_failed`. Checks, route and warnings carry codes and counts only. The input types `FailureInput`, `RunFacts`, `ConfigRef`, `Links`, `LogLink`, `EvidenceLink`, `BaselineLink` and `NativeRoute` are exported from `decoy_engine.run_result`.

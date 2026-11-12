### Added (canary scan entry point, F34-E3, 2026-11-12)

`decoy_engine.verification.canary.scan_canary_artifacts` scans the batches of up to 64
artifacts for supplied canaries and returns a `CanarySpanReport` of counts, reasons and
per-column findings. Invalid input gives a `REJECTED` report. Row and text budgets,
a column cap and the artifact cap surface as `canary_resource_limit`. `MAX_SCANNED_ARTIFACTS`
is exported beside it.

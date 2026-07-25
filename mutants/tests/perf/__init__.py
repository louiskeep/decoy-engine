"""PV-2 (2026-06-01) performance budget tests.

CI-gated wall-clock + memory budget regressions for the strategies +
runners that have non-trivial throughput characteristics. Each cell
fails if the strategy regresses past the documented budget so a PR
that introduces a perf regression cannot land silently.

Source: docs/audit/qa-process-improvements-2026-06-01.md P5.
"""

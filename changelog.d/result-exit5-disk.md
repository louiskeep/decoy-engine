### Changed (reorder disk shortfall code, RESULT-E-CAP follow-up, 2026-11-12)

`runtime/out_of_core_reorder_disk_budget_too_small` is a new reason, pending release. A reorder edge whose disk ledger exceeds the remaining disk budget now raises it in place of `runtime/out_of_core_reorder_budget_too_small`, so that disk case is no longer a capacity verdict (exit 5) and is excluded like `runtime/out_of_core_temp_disk_exceeded`. A memory ceiling too small for the sorter still raises the old code, which stays in `CAPACITY_REASON_CODES`.

### Added (memory-error reason, E-OOM, 2026-11-12)

`decoy/process_out_of_memory` is a new reason, pending release, retry class `environment_repair`. `reason_from_exception` returns it for a bare `MemoryError` in place of `decoy/unexpected_error`, and `CAPACITY_REASON_CODES` now holds it, so a caller that routes capacity failures sees it.

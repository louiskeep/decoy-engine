### Fixed (staging writer capacity errors, SUB-RUN E2, 2026-11-12)

The shared staging writer now raises `out_of_core_temp_disk_exceeded` only when a scratch write fails with `ENOSPC` or `EDQUOT`, so a full scratch disk during declared-casting staging gives the capacity code too. Source read errors and other scratch errors keep their own types, and the writer closes the source batch producer itself when staging fails.

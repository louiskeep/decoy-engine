### Changed (out-of-core scratch root on the arena, F25 S2a-1, 2026-11-12)

`run_fk_out_of_core(temp_dir=...)` now runs in a fresh private (0700) scratch arena made inside the caller's directory, ambient policy only as before, and removes that arena when the run ends. The caller's directory keeps its own mode: it is no longer tightened to 0700, and one that already holds other files is no longer refused. A missing `temp_dir` is still created 0700. With no `temp_dir` the run opens its arena in the system temp directory. A symlinked `temp_dir` is still refused.

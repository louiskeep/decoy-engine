### Added (fitted reference fan-out execution, FK-FANOUT E2b, 2026-11-10)

A `reference` generate column with `distribution: fitted` and a `snapshot_file` made with a fan-out block gives
each parent a child count drawn from the fitted degree histogram, instead of independent uniform picks. The
snapshot is read once at compile and pinned, so deleting or editing the file after compile changes nothing, and
seed derivation and the null mask never reopen it. The column takes the whole-table route. A `fitted` column
with no pinned fan-out stops with `reference_fanout_missing`, and child rows against an empty parent pool stop
with `reference_parent_pool_empty`. Other distributions generate the same cells as before.

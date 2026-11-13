### Fixed (non-UTF-8 statistical snapshot, DUP-DEFECTS E1, 2026-11-13)

A statistical `snapshot_file` holding a byte that is not valid UTF-8 is now refused with `statistical_snapshot_unreadable`, the same code a truncated snapshot gets. `decoy validate` no longer reports an internal error and `decoy run` exits 1 instead of 3. The message names the field and the path and carries no byte of the file. New `decoy_engine.generation.statistical.parse_snapshot_json` is the one parse step for snapshot bytes.

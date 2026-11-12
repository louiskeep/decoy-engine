### Changed (out-of-core scratch subtrees made through the arena, F25 S2a-1a, 2026-11-12)

The out-of-core route now makes its per-table relation, multi-parent merge, join and per-edge join directories as children of its scratch arena (0700 under any umask, read back before use), instead of leaving each callee to create them by path. Output bytes and scratch modes are unchanged; each table's join and merge directories now exist from the start of its pass, empty when unused. Callees not yet on the arena still receive these directories as paths.

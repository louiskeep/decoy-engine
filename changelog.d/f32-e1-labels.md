### Added (`LabelAllocator` and the support no-network sentry, F32 E1, 2026-11-12)

`decoy_engine.support` gains `LabelAllocator`, which hands out the ordinal `Label` values (`t1`,
`t1.c2`, `n3`) for the tables, columns and nodes of one bundle build, so a bundle never carries a
real name. A new sentry, `tests/sentry/test_support_no_network.py`, fails if the package imports a
network module. Nothing builds or sends a bundle yet.

Breaking: a generated relationship parent whose key is not provably unique is now refused at validate and run; set `unique: true` on the key column or use a `sequence`.

### Changed (generated relationship parent keys, POSTSCAN-STREAM PS-3, 2026-11-12)

A relationship whose parent table is generated is refused with `plan/relationship_parent_not_unique` unless the parent key is provably unique: `row_count` of at most one, a `sequence` with a non-zero `step`, `unique: true`, or a `unique_together` entry that equals the declared key columns. `decoy validate` and `decoy run` refuse alike, before any read. The message names the table and key columns and carries no values.

### Added (self-reference trees for generated reference columns, GEN-REL E2, 2026-11-12)

A generated `reference` column may now name its own table, so manager and employee or
category trees can be generated.

- The draw is a random recursive tree: the first `root_count` rows (default 1) are roots with a
  null value, and every later row takes the key of an earlier row. Every chain ends at a root,
  so there is no cycle. An optional `max_per_parent` caps the children of each key. The same
  seed gives the same tree.
- The key must be an earlier `sequence` column of the same table with no `null_probability`
  (`config/generate_self_reference_key_invalid`). Only `distribution: random` and
  `max_per_parent` are accepted: `sequential`, `weighted`, `fitted`, a positive
  `min_per_parent`, `null_probability`, `unique` and `unique_together` membership are refused
  (`config/generate_self_reference_option_unsupported`). `root_count` outside 1 to the table's
  `row_count` is refused with the existing `generate_option_range`, and `root_count` on a
  reference to another table with the existing `generate_option_unknown`; a table with
  `row_count: 0` ignores it.
- Only the self-edge is exempt from the reference-cycle check. Two tables that reference each
  other are still refused.
- A self-reference runs in memory on the whole-table route. Pinned compile refuses it
  (`plan/pinned_relational_option_unsupported`, a `PinnedModeError`), and pinned execute
  refuses it again for a plan that did not come from pinned compile
  (`runtime/pinned_relational_option_unsupported`, a `GenerationError`).
- Pinned execute now refuses a recorded `reference_policies` field of the wrong type or with an
  unknown policy with `runtime/pinned_relational_option_unsupported` (a `GenerationError`)
  instead of `pinned_plan_malformed`. Decoding a serialized plan with such a field still
  refuses with `pinned_plan_malformed`.
- A capacity probe holds an explicit `root_count` to its scaled row count.
- `decoy_engine.config` exports `is_self_reference` and `self_reference_problems`.

Every config without a self-reference keeps its bytes.

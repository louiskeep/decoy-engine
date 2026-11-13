### Added (conditional categorical, GEN-INHERIT E2, 2026-11-13)

A generated `categorical` column can now declare its category mix per value of an earlier column,
with no fitted snapshot.

- New options `condition_on` and `by`, given together. `condition_on` names an earlier column of
  the table; `by` maps a value of it to an entry with `weights` (for the top-level `categories`)
  or with its own `categories` and optional `weights`. Each row draws from the entry its parent
  value selects; the parent is matched by its text (`str`), the rule a fitted `condition_on`
  uses, so `1`, `true` and a null parent match the keys `"1"`, `"True"` and `"None"`.
- A row whose parent value has no entry draws from the top-level `categories` and `weights`. The
  whole-table route reports it once per column with `warning/generation_condition_parent_unseen`,
  `provider: categorical`, with the same value-free detail keys as a fitted `condition_on`.
  Pinned mode draws the same rows without a warning. The streaming route declines the column and
  the run stays on the whole-table route.
- Config checks: a `by` key must be a quoted string (a YAML number or bool key is
  `config/generate_option_type`, located by position and with a hint to quote it); an entry key
  other than `categories` and `weights` is `config/generate_option_unknown`; an entry with
  neither, a non-list or empty `categories`, or a `by` that is not a mapping is
  `config/generate_option_type`; entry weights follow the `weights` rule against the entry's own
  categories, else the top-level ones (`config/generate_weights_invalid`); either key without the
  other is missing; `unique: true` with `condition_on` is `config/generate_unique_conflict`.
  Locations never name a `by` key. A later or missing `condition_on` column is
  `plan/generate_column_forward_reference`, and the top-level and entry categories together must
  fit one Arrow type (`plan/generate_categories_mixed`).
- New `runtime/categorical_condition_column_unavailable`: a conditional column whose
  `condition_on` column is absent from the generated data (a config that skipped the checks)
  is refused on the whole-table and pinned routes, instead of sending every row to the fallback
  mix.
- The column consumes one draw per row from its one seeded stream, as a plain column does; its
  seed hashes `condition_on` and `by`. Its type is that of every category it can draw. The
  parent column is read as text by numeric typing, so a `sequence` parent stays text, as under a
  fitted `condition_on`. A pinned `windowed_date` anchored on a conditional column checks the
  entry categories too. The streaming width bound and the memory estimate price the column by
  the widest category it can draw, entry categories included.
- `decoy_engine.config` exports `categorical_categories`.

Every `categorical` column without `condition_on` keeps its bytes and its type.

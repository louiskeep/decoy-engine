# 12. Generation

Status: plan

## What it covers

`mode: generate`: building tables from a schema with no source data. Column types
`sequence`, `faker`, `categorical`, `reference`, `formula`, `derived`,
`derived_aggregate`, `constant`, `distribution`, `windowed_date`, `group_key`,
`grouped_series` and `statistical` (section 13). Options `unique`, `unique_together`,
`pooled`, `null_probability` and `locale`.

## Expected behaviour

- Same seed (or master key and label) and same config give byte-identical tables.
- Each column's stream depends on the column's own name and config, so two columns with
  identical settings do not produce identical values (roadmap row 24i).
- Numbers stay numeric inside the generator: arithmetic in `derived` and ordering in
  `grouped_series` work on numbers, not strings (row 24j).
- The schema is strict per type. A misspelled option, a bad `faker_type`, negative
  weights or a duplicate column name is refused at validate (row 24k).
- `reference` columns draw only from generated parent keys. There are no orphans.
- `unique: true` is accepted on `sequence`, `categorical`, `faker` and `reference`, and
  refused elsewhere with `config.generate_unique_unsupported`. A domain too small for
  the row count is refused at validation.
- `unique_together` makes listed pairs of `reference` columns distinct.
- A `faker` column on an eligible type pools automatically at 50,000 rows or more
  (about 10,000 values, so it repeats). `pooled: false` turns that off. `pooled: true`
  does not force it on.
- Generation runs on Python and NumPy only. The Rust companion does not affect it.
- `--rows N` previews a generate pipeline at small size and writes nothing.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| GEN-01 | Generate a three-table schema twice with the same seed, in two processes. | Byte-identical output files. |
| GEN-02 | Two `faker` columns with identical settings (`first_name`) in one table. | The two columns differ row by row. |
| GEN-03 | `sequence` with `start: 100`, `step: 5`; then `step: 0`. | 100, 105, 110 and so on; then refused at validate. |
| GEN-04 | `categorical` with weights 0.7, 0.2, 0.1 on 100,000 rows; then weights that are all zero; then a negative weight. | Shares within sampling error; then both bad configs refused at validate, naming the column. |
| GEN-05 | `reference` child of a generated parent, 60 children over 30 parents. | Every child key exists in the parent table. Row count is exactly 60. |
| GEN-06 | `reference` with `min_per_parent: 2`, `max_per_parent: 4`; then bounds the row counts cannot satisfy. | Every parent has 2 to 4 children; then refused. |
| GEN-07 | `unique: true` on a `faker` `uuid4` column of 500 rows; on a `categorical` with 5 categories and 500 rows. | First has 500 distinct values. Second is refused at validation as a domain too small. |
| GEN-08 | `unique: true` on a `formula` column; `unique: true` with `pooled: true`; `unique: "true"`. | `config.generate_unique_unsupported`; `config.generate_unique_conflict`; `config.generate_option_type`. |
| GEN-09 | `unique_together: [[item_id, shelf_id]]` with 600 rows over 50 by 20 parents; then 1,200 rows. | 600 distinct pairs; then refused, since only 1,000 pairs exist. |
| GEN-10 | `faker` `first_name` at 200,000 rows; the same at 500 rows; the same with `pooled: false`. | The first has at most about 10,000 distinct values. Record the distinct counts for all three, and confirm the same seed reproduces each. |
| GEN-11 | `derived` column `price * quantity` over generated float and sequence columns. | Numeric result, correct to the row. Not string concatenation. |
| GEN-12 | `null_probability: 0.25` on 100,000 rows; then `null_probability: 1.5`. | About 25% nulls; then refused at validate. |
| GEN-13 | A column option misspelled (`weigths`), an unknown `faker_type`, a duplicate column name. | Each refused at validate with exit 1, naming the column. |
| GEN-14 | Generate `city`, `state` and `zip` as three `faker` columns. | Record how often the three agree. See the open point: coherent geography is not available in generate mode yet. |

## Open points

- **Coherent composites are not reachable from generate mode.** The 2026-11-08 audit
  found generated New York ZIPs starting with 5, 0, 4 and 7, and `composite_person` as a
  `faker_type` producing plain words. Roadmap row 24p (GEN-PROVIDERS) is not started.
  GEN-14 documents the gap; it is expected to fail until 24p ships.
- A generated child does not inherit a parent attribute (`claims.member_state` matched
  its member's state 6% of the time in the audit). No build row was found for this.
- `distribution` in `docs/strategies.md` means "sample from a snapshot dict", while
  roadmap row 24q adds parametric `distribution` (uniform, poisson, zipf) and `constant`
  in a PR not yet merged. Confirm which is in V1 before writing checks for them.
- A `formula` column that reads a later-declared `formula` sibling sees a null
  placeholder, silently. Confirm validate refuses forward references.
- Rows 24n (GEN-REL: parent-anchored date windows, self-reference hierarchies) is not
  started. Leave those out of V1 QA unless it lands.

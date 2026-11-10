# Generated numeric typing: F0 golden re-freeze

Status: record

Generated numeric columns are now typed. An unformatted `sequence` (no prefix, suffix or
`pad_length`) is int64. A stock faker provider is int64 or float64. Formatted sequences, categorical text and every other
faker provider stay string. The decision is made once at plan time by `numeric_plan` in
`generation/_numeric_typing.py`; the declared schema (`_admission.py`) and both emitters read it.

A column is numeric only when all of these hold, otherwise it stays string as before:

- Its values provably fit int64. A sequence needs `start` and `start + (row_count - 1) * step` in
  range. The faker int providers are an allowlist: `pyint` and `random_int` need `min`/`max`
  inside int64, `random_number` needs `digits` of 18 or fewer, `random_digit` and
  `random_digit_not_null` are always in range. Faker float providers are float64.
- It is not a stock-provider name overridden by a custom provider.
- Every reader of it is numeric-safe, per the closed registry `READERS` in
  `generation/_numeric_typing.py`. Formula references, aggregate input, group labels, window
  anchors and statistical conditioning keep the column text. A `derived` expression is judged per
  use: arithmetic and numeric comparisons are safe; `+`, `==` and `in` against strings are not.
  A `reference` column is checked together with its parent.
- A relationship is safe only when every paired column is generated and has the same planned
  type. A masked or source end, or a text-typed generated end, keeps both sides text.
- The check repeats until nothing changes, because removing one column can make a reader of
  another unsafe. A reader or column type missing from the registry keeps the column text, and
  `tests/sentry/test_numeric_reader_registry.py` fails when one appears.

The pinned-plan path (`_pinned_execute`) passes no numeric type and keeps text, as before.

## Re-frozen goldens

Each moved only in its declared and emitted type. The check compared every F0 fixture before and
after: same tables, same column order, same row counts, same null rows, and the printed text of
every cell equal. No value, order or null position moved.

| Fixture | Table | Column | Change |
| --- | --- | --- | --- |
| `sequence` | `seq` | `plain`, `neg` | string to int64 |
| `live_faker` | `live` | `num` | string to int64 |
| `reference` | `ref_child` | `cid` | string to int64 |
| `nulls` | `nulls` | `seq_partial` | string to int64 |
| `nulls` | `empty` | `id` | string to int64 (zero-row table keeps its declared type) |

Files: `tests/parity/_f0_expected.py` (captured with `_f0_capture`), `tests/fixtures/phase_f_fr0/manifest.json`
(regenerated with `_fr0_refreeze`), `REFROZEN` in `_fr0_refreeze.py`.

## Test changes that follow from the type

- The semantic gate checks int64 for numeric sequences and string for formatted ones. The
  `lost_declared_type` mutant stays; `lost_numeric_type` is added to kill a mutant that turns a
  numeric column back into text.
- The audit witness marks an int sequence by offset instead of a text prefix.
- The core-selection witness mutant draws ints.

## Output text

CSV, fixed-width and Parquet get the same cell text for non-null values. Parquet columns are now
int64/double instead of string. A nullable int column converted with a plain `Table.to_pandas()`
becomes float64 and prints `1.0`; `generated_table_to_pandas` keeps pandas nullable ints so it
prints `1`.

## CLI repin

An engine release containing this change must ship together with the CLI switch to
`generated_table_to_pandas`; releasing the engine alone makes the CLI print nullable ints as `1.0`.

The engine exports `decoy_engine.generated_table_to_pandas(table)` (also in
`decoy_engine.generation`). At repin the CLI replaces `table.to_pandas()` with it wherever a
generated or masked output table becomes a frame that is written as text. A plain `to_pandas()`
turns a nullable int64 column into float64 and prints `1.0`.

Sites on CLI `origin/main` (grep `to_pandas()` to confirm line numbers at repin time):

- `src/decoy/api.py:563`, `outputs = {name: table.to_pandas() ...}` in `mask()`.
- `src/decoy/cli/io/_targets.py:336` (fixed_width frame), `:364` (CSV write) and `:461` (chunked
  mask CSV write).
- The writers reached from `_write_mask_outputs` in `src/decoy/cli/run.py`, the demo output
  write in `src/decoy/cli/demo.py` and the unmask read in `src/decoy/cli/unmask.py`; each one that
  calls `.to_pandas()` on an engine table changes the same way.

Also needed: pin the engine release that carries this change, and add a CLI test that runs a
generated nullable plain sequence (`null_probability` above 0) through the CSV writer and asserts
the cells are `1`, `2` and so on and never `1.0`.

The engine does not edit the CLI repo; this list is the whole change.

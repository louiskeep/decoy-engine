# Generated numeric typing: F0 golden re-freeze

Status: record

Generated numeric columns are now typed. An unformatted `sequence` (no prefix, suffix or
`pad_length`) is int64. A stock faker provider annotated `int` or `float` (pyint, pyfloat,
random_int and similar) is int64 or float64. Formatted sequences, categorical text and every other
faker provider stay string. The rule lives in `generation/_numeric_typing.py`; the declared schema
(`_admission.py`) and the emitted values come from the same function.

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
- Mixed-route fixtures that join a text FK to a generated parent now give the parent `pad_length`,
  so the parent key stays text.
- The core-selection witness mutant draws ints.

## Output text

CSV, fixed-width and Parquet get the same cell text for non-null values. Parquet columns are now
int64/double instead of string. A nullable int column converted with a plain `Table.to_pandas()`
becomes float64 and prints `1.0`; `generated_table_to_pandas` keeps pandas nullable ints so it
prints `1`.

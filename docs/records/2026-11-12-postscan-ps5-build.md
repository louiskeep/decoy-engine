# POSTSCAN-STREAM PS-5 build record (exact column facts)

Status: record

Built from engine `origin/main` 105291d8. Nothing calls the new code yet; PS-6 rewires the column
suite onto it.

## What changed

- `profiling.column_distinct(reader, *, scratch, memory_limit)`: exact distinct non-null count of
  one Arrow column in a DuckDB connection that spills under caller-owned scratch.
- `detection._scoring.counting_scope()`: collects each detector's `(id, hits, n, threshold,
  name_hint)` before the threshold test. `detection/detectors.py` re-exports it and
  `_confidence_bucket` as the public path (one file and two names beyond the Placement rows,
  approved by the coordinator; the file stays at its 920-line pin).
- `verification/postmask/_column_facts.py`: `column_facts(source, output, columns, ...)` returns a
  `ColumnFacts` per column. Source and output are re-cut to equal batch lengths. Detector hits and
  non-null counts are summed over batches, then decided with the same threshold and bucket.

## Batch independence

Columns are converted per batch with `to_pandas(integer_object_nulls=True)`, so integers keep one
rendering whether or not a batch holds a null. The whole-table oracle in the tests uses the same
conversion. Today's plain `to_pandas()` renders an integer column that has nulls as `"1.0"`; the
facts render it as `"1"`, in every layout.

## Evidence

Red first: commit "test(postscan): column facts red tests and API stubs" fails on the stubs.
Mutations, each run in a throwaway worktree, one at a time, against
`tests/unit/detection_verification/postscan/test_column_facts.py`:

| Mutation | Result |
|---|---|
| average per-batch detector rates | 3 failed (both batch-7 cells and the uneven-layout test) |
| plain `to_pandas()` (batch-dependent int rendering) | 5 failed |
| pair batches with `zip` instead of re-cutting | 1 failed (the uneven-layout test) |
| identity counts `!=` instead of `==` | 5 failed |
| `count(k0)` instead of `count(DISTINCT k0)` | 6 failed |

The 250,000-row table with batches of 7 (35,714 detector passes per column) is skipped in the
suite for time; the same table with 100,000-row batches and the 10,000-row table with batches of 7
run. No file-access registry entry was needed: the DuckDB execute sits in the existing `_count`
helper.

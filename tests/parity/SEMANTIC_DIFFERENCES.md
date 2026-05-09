# Pandas ↔ Polars semantic differences

> **Status:** living document. Updated as Phase 3 + 4 + 6 surface divergences.
> **Last reviewed:** 2026-05-10.

This is the running list of behavior differences between the pandas and polars implementations of relational ops, in cases where both are correct under their own semantics. Each row is a documented divergence — not a bug — and each one has a downstream-handling note (or "no action; both are correct").

| # | Behavior | Pandas | Polars | Decision / note |
|---|---|---|---|---|
| 1 | Empty string in CSV column | Loaded as `""` | Loaded as `null` | Phase 4 (DuckDB source.file). Document at the connector boundary. |
| 2 | NaN in numeric column | `float64` `NaN` | `null` (no NaN concept) | Normalize at Arrow conversion boundary; preview path translates both to JSON `null`. |
| 3 | Sort tie-break for equal keys | `kind="mergesort"` (stable) | `maintain_order=True` (stable) | Equivalent. Both ops set the stable flag explicitly. |
| 4 | `derive` column name with double-quotes | `df.eval` parses ambiguously | SQLContext quoting rejects | Validator could constrain column names to no `"`; not seen in real configs. |
| 5 | `filter` predicate with Python-only operators (`is`, `in`) | Works via `engine='python'` | SQLContext rejects | Documented; the canvas's predicate builder doesn't emit these. |
| 6 | `dedupe` row order when `keep='first'` on unsorted input | Stable input order, first wins | `maintain_order=True` matches | Equivalent for the cases parity tests cover. |

## How to add a row

When a parity test catches a divergence:

1. Decide whether one side is wrong (= bug; fix). If both are correct under their own semantics, it's a documented difference.
2. Add a row to the table above with the behavior, the two outputs, and the downstream handling decision.
3. If the difference is data-shape-specific, add a parametric parity test that asserts the divergence explicitly so a future change can't silently cross the line.

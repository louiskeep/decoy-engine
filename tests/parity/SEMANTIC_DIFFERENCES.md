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

## v2 strategy-substrate parity (engine-v2 S12)

The rows above cover the V1 graph-engine relational ops (pandas vs duckdb/polars). This section covers the v2 EXECUTION-adapter strategy parity (`test_strategy_substrate_parity.py`): the v2 pandas adapter vs the v2 polars adapter, for the same masking `(plan, sources)`.

The v2 parity gate is **value-level**: `assert_frames_semantically_equal` compares `outputs[table].to_pydict()` (per-column values + null positions), not Arrow schema or buffer identity. The accepted differences:

| # | Behavior | Pandas adapter | Polars adapter | Decision / note |
|---|---|---|---|---|
| v1 | Arrow type width after the pa -> pl -> pa boundary | e.g. `string`, `binary`, `list`, `dictionary<int32,..>` | widens to `large_string`, `large_binary`, `large_list`, `dictionary<uint32,..>` | Accepted; Polars 1.x widens on `from_arrow`/`to_arrow`. Values are preserved (Dennis S11 review, 14-dtype probe). The parity gate compares values, not Arrow type. |
| v2 | `redact` / `truncate` output column dtype | object -> Arrow `string` | Utf8 -> Arrow `large_string` | Accepted (same as v1). Both emit the same strings + nulls. |
| v3 | deterministic `shuffle` permutation | `numpy.random.default_rng(seed).permutation` | SAME shared primitive (container-only migration) | No divergence: the permutation primitive is shared, so the permuted values are byte-identical for a given seed. |

Non-deterministic strategies (unseeded `shuffle`, etc.) are NOT in the parity set: their output varies per run by design, so a cross-adapter equality assertion is meaningless. Parity fixtures use deterministic mode.

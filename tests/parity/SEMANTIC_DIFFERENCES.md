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
| v4 | IEEE NaN in a float column | `to_pandas()` in, `pa.Table.from_pandas()` out folds NaN -> null | out-of-core route never touches pandas, so a passed-through / passthrough-masked float NaN stays NaN | Accepted; both mean "missing". The out-of-core FK parity harness (`test_out_of_core_fk_parity.py`) folds NaN -> None before comparing, same class as v1/v2 Arrow-boundary drift. A MASKED NaN FK key is a different matter: it MUST mask to null (not a hashed/redacted/"nan" token), enforced by `kernel/_scalar._is_missing` and pinned by `test_nan_parent_key_preserved_as_missing`. A wrong masked token is a string, so the NaN->None fold never hides it. |

Non-deterministic strategies (unseeded `shuffle`, etc.) are NOT in the parity set: their output varies per run by design, so a cross-adapter equality assertion is meaningless. Parity fixtures use deterministic mode.

### Pandas FK oracle int-past-float-precision handling (SC2 CF3, CLOSED by DE-10)

For an FK output column whose type resolves to `float64` (because a float value was possible on either side), a whole-number integer key beyond `2**53` cannot live in a double without loss. Before DE-10, the two routes handled this differently, and the pandas oracle was the one that silently corrupted:

- **Pandas oracle (pre-DE-10):** rounded the key on the pandas round trip (e.g. `9007199254740993` -> `9007199254740992.0`). This was a silent **referential-integrity drift** -- the emitted FK key no longer equalled the source key -- not a crash. So the oracle was not a clean "ground truth" for this exact shape.
- **Out-of-core route:** fails closed with `ExecutionError(code="out_of_core_fk_key_dtype_unsupported")` rather than publish a rounded (drifted) key. Enforced in two places: the cross-batch cast (`_join.py::cast_fk_chunk`) and, since CF3, the per-batch build (`_join.py::_append_output_batch`, where a matched-float + orphan-int-past-`2**53` mix in one batch would otherwise raise a raw, uncoded `ArrowInvalid` from `pa.array(..., from_pandas=True)`).

**DE-10 closes the gap CF3 left open.** `execution/_fk_keys.py`'s lossless-typing contract (`to_pandas_fk_safe` at ingestion, `lossless_fk_int_values` at FK write-back) is now shared by the full-frame, sequential, and chunked routes (all three run through `PandasExecutionAdapter`):

- A null-bearing integer FK key column (parent or child) now round-trips through pandas' nullable `Int64` extension dtype instead of numpy's float64-on-null default, so it survives EXACTLY on every route, including magnitudes beyond `2**53`. This was the common case (no genuine float ambiguity anywhere in the column) and is the main fix.
- The one shape that genuinely cannot be resolved losslessly -- a literal float value (not a value that merely folds to a whole int) sharing an output column with an integer beyond `2**53` -- now raises the SAME `ExecutionError(code="out_of_core_fk_key_dtype_unsupported")` on the pandas oracle that the out-of-core route already raised. Classification matches the out-of-core route's own matched/orphan split (a matched value keeps its source dtype; only an orphan/preserved value folds through `fk_key_value`), so the two routes agree on exactly which shapes are lossless vs. must reject.

Pinned by `tests/unit/execution/test_de10_fk_lossless_typing.py` (exact survival across all three routes, route-parity, and the fail-closed mix) and updated in `test_out_of_core_fk_parity.py::test_matched_float_and_int_orphan_beyond_precision_fails_closed` (now asserts the oracle raises the same code instead of rounding) and `test_non_representable_int_orphan_float_parent_fails_closed` (unaffected -- was already a fail-closed case on both routes' terms, still is).

### Composite FK child masked as independent scalar seeds is gated, not admitted (SC2 CF2)

A composite (multi-column) FK edge can reach the out-of-core route in two shapes:

- **`composite_fk_group` (canonical, compiler-produced):** one `GroupSeed` over the whole child key. **Full oracle-parity** across matched / orphan / partial-null / fully-null rows under every orphan policy (both routes treat a partial-null composite key as fully null). Admitted. Pinned by `test_composite_fk_group_orphan_and_partial_null_parity`.
- **Independent `scalar` seeds on the child FK columns (double-masking):** each child FK column carries its own scalar strategy AND is a composite-FK child. Here the routes **diverge**: the pandas oracle scalar-masks each column *before* resolving the FK (FK children resolve last), so a PRESERVE/WARN orphan (and a partial-null key's non-null components) keeps the **scalar-masked** value, while the out-of-core route joins on and preserves the **raw** source key -- a **raw-value leak**. This is NOT admitted: the compat gate rejects it fail-closed with `out_of_core_composite_fk_scalar_child_unsupported`, so the job falls back to full-frame. Pinned by `test_composite_fk_scalar_child_gate_rejected`.

This supersedes the earlier "composite partial-null orphan parity divergence" note in `out_of_core/_batch_join.py`: the divergence is real only for the scalar-double-masking shape, which is now gated; the canonical group shape is parity. Single-column scalar FK children are unaffected (they are FK-resolution-owned, not double-masked, and are covered by the single-edge parity property test).

### v2 output-FILE-bytes drift (S11 review M1; S13 disposition)

The value-level parity above covers in-memory `outputs`. The platform's evidence manifest, however, hashes the WRITTEN output-file bytes (the manifest's `outputs[].hash` is a tamper-evident byte-hash of the file THIS run produced, not a cross-substrate logical-equality digest). Polars 1.x widens five Arrow types on `from_arrow`/`to_arrow` (`large_string`, `large_binary`, `large_list`, `dictionary<uint32,..>`, `time64[ns]`), so a file written by the polars writer can carry a different parquet/IPC schema than the pandas-path file for the SAME logical data, and the manifest hash therefore differs across substrates for those types.

**Disposition (S13 M1): ACCEPT and document** (Dennis-confirmed Session 52; final PO sign-off on the readiness report). Rationale (the load-bearing framing is WITHIN-substrate reproducibility, not substrate count): the evidence manifest's `outputs[].hash` is the tamper-evident byte-hash of the file THIS run produced, not a cross-substrate logical-equality digest, and the R3 contract has no cross-run/cross-substrate invariance clause. Within-substrate reproducibility holds (the polars writer is deterministic for a given input). The polars-default flip lands PRE-GA, so every customer-held manifest is polars-written: there is no pandas-era production manifest to reproduce or compare against, and the only pandas-written files exist transiently during the migration window. The drift is bounded to parquet/IPC (CSV has no Arrow-type encoding); logical data is identical in all cases, only Arrow type width differs. Normalize-at-write in `write_target_polars` is a correct V2+ hardening, not a ship gate.

This disposition is recorded in the S13 release-readiness report's known-limitations section (the canonical ship-decision home); this doc is the accepted-differences cross-reference.

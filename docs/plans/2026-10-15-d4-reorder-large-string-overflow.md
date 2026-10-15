# D4: fix the reorder-lane Arrow 2GB offset overflow (large-string internally)

Status: plan
Roadmap item: Phase D4 (FK scale to 100M), reorder/external-reorder lane
Author: Opus
Date: 2026-10-15
Rules consulted: 00-universal, development-loop, risk-and-exceptions, autonomous-operation, feature-dev, testing

## Problem

At 100M rows the out-of-core FK join with `reorder=on` fails with `ArrowInvalid`.
The batch path completes the same job (2787s, 3.9GB RSS, parity ok), so the data and
the masking are correct; only the reorder lane overflows.

Root cause: the external sorter materializes a whole column into one contiguous Arrow
array during its flush and merge. The masked parent payload column
`__decoy_parent_masked_{idx}` enters the sorter as `pa.string()`, which uses 32-bit
offsets (max 2^31 bytes per single array). At 100M wide-string rows one flush run's
payload exceeds 2^31 bytes, so the de-chunking `take`/`combine_chunks` overflows.

Confirmed throw sites (same column, same limit), from the read-only FRAME on main:
- `out_of_core/_external_sort.py:365` `table.sort_by(self._sort_key_column)` in `_flush`
  (internal `take` de-chunks the full run). First and most likely.
- `out_of_core/_external_sort_bounding.py:120` `table.combine_chunks().to_batches()`
  via `_write_run`.
- `out_of_core/_external_sort.py:480` `pa.concat_tables(emit_parts).sort_by(...)`
  on the merge side (smaller bound, secondary).

The batch path is safe because it assembles output per single input batch and never
materializes a whole column (`_batch_join.join_batch`). The per-row width guard
(`_key_width.py`) is orthogonal; it bounds per-row size, not the aggregate column.

## Approach (A, chosen by Cam 2026-10-15)

Carry the string/binary masked payload columns through the external-sort lane as
Arrow's 64-bit `large_string` / `large_binary` so combine and sort can exceed 2GB.
Output stays byte-identical `string`, because it is narrowed back on emit by code that
already exists.

Why this is surgical: every downstream guard already accommodates large types.
- The sorter's orderable-type allowlist already accepts `large_string`/`large_binary`
  (`_external_sort.py:22-23,264-265`; `_external_sort_bounding._is_supported_key_type`
  :64-71). Note the SORT KEY is `__decoy_row_nr` (int64), not the payload; the payload
  rides along as a non-key column, so no key-type concern arises.
- `_min_row_bytes` already counts the int64 offset for large types
  (`_external_sort_bounding.py:49-51`), so the run-bytes width estimate stays correct.
- The slim-schema guard `_assert_slim_sorter_schema` (`_stream_join_cursors.py:384`)
  only rejects raw child columns; it is agnostic to the masked column's type, so a
  large_string masked column still passes.
- On emit, `resolve_batch` (`_stream_join.py:639`) assembles output per bounded
  payload-store batch: `_combine_join_slices` repackages columns zero-copy with
  `pa.record_batch(...)` (no `combine_chunks`), then `_append_output_batch` re-boxes
  matched values and `_cast_chunks(..., output_types[idx])` casts to the fixed output
  type. The output is produced per bounded batch, never as one whole column, so it can
  never re-hit the 2GB limit, and the final type is the fixed `string` the oracle emits.

So the large type is a transient, internal-to-the-sorter representation. The only place
a whole column is materialized contiguously is inside the sorter, which is exactly where
the overflow happens and exactly what this fix widens.

## Change (minimal)

1. `out_of_core/_stream_join.py`, `run_ordered_join` drain loop (around :573): before
   `_assert_slim_sorter_schema(batch)` and `sorter.write(batch)`, cast each batch's
   masked payload columns to the large variant:
   - a `string` masked column -> `large_string`
   - a `binary` masked column -> `large_binary`
   - leave `__decoy_row_nr` (int64) and `__decoy_parent_match` (bool) untouched.
   Implement as a small named helper (e.g. `_widen_payload_offsets(batch) -> pa.RecordBatch`)
   with a docstring that says WHY (the int32-offset 2GB limit on the de-chunked run),
   citing the Arrow offset-width behavior as the source pattern per the repo methodology
   rule. Select the columns by name prefix `__decoy_parent_masked_` AND by type being
   string/binary (both conditions), so a future non-string masked component is untouched
   unless it is string/binary.
   - The cast is a pyarrow `.cast()` applied after the DuckDB arrow fetch. Do NOT try to
     force the width through the SQL `SELECT`: DuckDB exports `VARCHAR` as int32 `string`
     regardless of SQL cast, so the robust lever is the pyarrow cast at this boundary.
   - Prefer casting before the schema assertion so the sorter's input schema and the
     asserted schema are the same object.

No other production change is expected. The following are VERIFY-only items, not edits,
unless a check below fails:
- `_assert_slim_sorter_schema` still passes with a large_string masked column.
- sorter allowlist + `_min_row_bytes` already correct (cited above).
- emit cast-back already narrows to `string` (cited above).
- `run_bytes_cap` sizing (`_reorder_budget.py:194`, 0.15 * process ceiling) needs no
  change; large offsets add 4 bytes/row on the payload column only, trivial against the
  multi-GB arrays unblocked.

## Acceptance tests (define before build; no later contributor weakens them)

1. Cast-contract unit (cheap, deterministic): the batch handed to `sorter.write` inside
   `run_ordered_join` has every `__decoy_parent_masked_*` string column typed
   `large_string` (and binary -> `large_binary`), while `__decoy_row_nr` stays int64 and
   `__decoy_parent_match` stays bool. Assert by capturing the batch the sorter receives
   (spy/subclass or capture the helper's output), not by reading the source.
2. Byte-parity unit (moderate size, in CI): a reorder-path FK run whose masked parent
   values include empty strings, nulls, and multi-byte unicode produces output
   byte-identical to the batch-path and the pandas oracle (digest match). Guards the
   large_string -> string round-trip for the edge cases that break naive casts.
3. Sorter large_string ordering unit (cheap, in CI): a `BoundedExternalSorter` fed a
   `large_string` non-key column that spills across at least two runs emits globally
   sorted order byte-identical to an in-memory reference sort. If an equivalent
   parametrized sorter test already exists for `string`, extend it to `large_string`
   rather than adding a parallel test.
4. Forced-overflow integration (opt-in, NOT in default CI): an env- or marker-gated test
   (`@pytest.mark.slow` or a `DECOY_RUN_HEAVY` gate) that forces a single flush run whose
   payload exceeds 2^31 bytes (wide strings + a tuned low ceiling) and asserts the run
   completes with no `ArrowInvalid` and sorted order holds. Gated off by default because
   the devbox runs one test agent at a time under a memory cap (see the devbox memory
   constraint); this test needs multi-GB RAM. The real-world proof is the 100M GCP
   re-run below, so this test is a documented reproducer, not a gate.
5. Regression: the existing out-of-core parity suite and the 10M smoke stay green; every
   existing byte-parity digest is unchanged (no fixture churn).
6. Post-merge integration (not a CI unit): the D4 100M `reorder=on` GCP run completes
   with parity ok. Re-run after merge; 200M follows if 100M is clean.

## Harness (pairs with this fix)

Add log-hygiene-safe traceback-FRAME capture to `scripts/fk-scale/bench_worker_fk.py`
`_error_record` (currently returns `{"type", "code"}` only, deliberately no message or
traceback for log hygiene). Add a frame list of `file:line:function` ONLY, with NO
locals, NO message, NO data/expression text, so a future overflow reports where it threw
without leaking payload. dennis must confirm no data leak; the log-hygiene sentry must
stay green. Keep this in the same PR so the bench can self-describe the next failure.

## Risk and gates

- Primary risk is byte-parity: the large_string -> string round-trip must be identical
  for nulls, empty strings, and unicode. Test 2 is the gate for it.
- Memory impact is negligible (int64 offsets on one column).
- Module-size census: if any touched module crosses 600 LOC, add/update the exact-LOC
  census entry in `tests/sentry/test_module_size.py` in this PR.
- Gate chain: Opus VERIFY -> dennis (fresh-context adversarial) -> Codex final gate
  (after dennis) -> merge on full green (dennis + Codex + CI), Slack before merge-to-main
  -> re-run 100M (+200M) on GCP -> Slack results. This plan goes to the Codex PLAN gate
  BEFORE the Sonnet build starts.

## If this proves insufficient (fallbacks, not planned work)

- B: make the sorter chunk-aware so it never materializes a single >2GB array (sort via
  indices + chunked take, bounded emit per sub-2GB slice). More plumbing, same tests.
- C: route reorder-eligible jobs whose estimated per-column run bytes exceed 2GB to the
  batch path (or decline with a clear reason) and document the ceiling. Smallest change,
  caps scale.

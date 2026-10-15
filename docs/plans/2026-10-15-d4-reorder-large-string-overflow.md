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
- On emit, `resolve_batch` (`_stream_join.py:639`) assembles output per payload-store
  batch (bounded by ROW COUNT): `_combine_join_slices` repackages columns zero-copy with
  `pa.record_batch(...)` (no `combine_chunks`), then `_append_output_batch` re-boxes
  matched values (`_join.py:274-310`) and `_cast_chunks(..., output_types[idx])`
  (`_batch_join.py:525-533`) narrows to the fixed output type. The re-box recovers an
  ordinary `string`/`binary` (`_python_roundtrip_type` maps large variants back,
  `_batch_join.py:500-503`), so the sorter's large type never leaks into the resident or
  sink schema; an all-null resident column may legitimately recover `null`
  (`_emit.py:263-280`), and binary stays `binary`.

So the large type is a transient, internal-to-the-sorter representation; it is narrowed
back on emit.

## Scope and the per-batch ceiling (what A fixes and what it does not)

Approach A removes the AGGREGATE overflow: the whole-column materialization inside the
sorter (`sort_by`/`combine_chunks`/`concat_tables`) that fails when 100M rows of masked
strings exceed 2^31 bytes in one run. This is the D4 failure.

It does NOT lift a separate, much higher per-batch ceiling, and the plan must not claim
it does. The emit re-box is bounded by row count, not bytes: a single resolved batch
(default 65,536 rows, `_join.py:40`) rebuilds a plain int32 `string`/`binary` array, so
its data span must fit 2^31 bytes. That is ~32 KB per non-null value at the default
batch size (UTF-8 bytes for strings). The same per-batch int32 bound applies EARLIER, at
the DuckDB Arrow fetch (`_stream_join.py:516`), which runs BEFORE the proposed widening,
so widening cannot rescue a fetch batch that is already oversized on export.

This fix is therefore scoped to workloads whose per-batch fetched and resolved arrays
stay int32-representable (every realistic masked value, and specifically the D4 fixture:
masked width 4 over 65,536-row batches is ~256 KB per batch column, six orders of
magnitude under the per-batch limit). Arbitrary per-value width is out of scope and left
as a documented limitation; the recommended follow-up is byte-aware emit batching, or a
clear fail-closed error on an oversized single value rather than a raw `ArrowInvalid`.
This limitation is recorded in the module docstring and in the D4 record.

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
- emit cast-back already narrows to `string`/`binary` (cited above).
- `run_bytes_cap` sizing (`_reorder_budget.py:194`, 0.15 * process ceiling) needs no
  change; large offsets add 4 bytes/row on the payload column only, trivial against the
  multi-GB arrays unblocked.

Complete allocation inventory for the masked payload column on the reorder path (so the
build verifies every contiguous materialization, not just the three throw sites):
- Covered by widening (these combine/sort the masked column while it is still
  large-offset, so A protects them):
  - sorter flush `sort_by` (`_external_sort.py:365`),
  - sorter run write `combine_chunks` (`_external_sort_bounding.py:120`),
  - sorter merge `concat_tables` + `sort_by` (`_external_sort.py:480`),
  - bounded-view `pc.take` (`_external_sort_bounding.py:90`),
  - cursor cross-run assembly `JoinRowCursor.take` -> `_concat_join_row_batches` ->
    `combine_chunks` (`_stream_join_cursors.py:248,438`): the cursor's masked columns are
    still large-offset here, so the widening carries through the combine.
- Subject to the per-batch int32 bound (these return to int32 on emit, governed by the
  Scope section, NOT by widening): the re-box `pa.array(...)` (`_join.py:310`) and the
  `_cast_chunks` `concat_arrays` (`_batch_join.py:533`).
- Not an issue: `concat_tables` preserves chunks (the following `sort_by` gathers);
  run readers load individual stored batches (`_external_sort_run.py:54-56`,
  `_external_sort.py:510-513`); there is no separate payload-building merge heap.

## Acceptance tests (define before build; no later contributor weakens them)

1. Wiring contract at the REAL boundary (cheap, deterministic): spy on the actual
   `sorter.write` call inside `run_ordered_join` (subclass/patch `BoundedExternalSorter`
   or capture the batch passed to `.write`), and assert every `__decoy_parent_masked_*`
   string column is `large_string` (and binary -> `large_binary`), `__decoy_row_nr` stays
   int64, `__decoy_parent_match` stays bool, AND the slim column set is exactly those.
   Checking only the helper's return value is insufficient (a regression that stops
   calling the helper would still pass). Include the negative: a test that removes/bypasses
   the widening call makes this boundary assertion FAIL (lock the wiring, not just the
   helper).
2. Byte-parity through real spills (moderate size, in CI): a reorder-path FK run that
   actually spills across >=2 sorted runs and crosses a cursor batch boundary, for BOTH a
   `string` and a `binary` masked payload, producing output byte-identical to the
   batch-path and pandas oracle (digest match). Cover nulls, empty values, multi-byte
   unicode, embedded NUL, and non-UTF-8 binary bytes, and distinguish a matched-null
   parent from an unmatched (orphan) child. Assert the explicit FINAL schema for both the
   sink and resident output (plain `string`/`binary`, with the legitimate all-null -> `null`
   recovery per `_emit.py:263-280` allowed), alongside value/digest parity. Moderate size
   alone does not require widening (arrays never reach the failing size), so this test
   proves correctness of the round-trip, not the overflow fix; test 4 proves the latter.
3. Sorter large_string/large_binary ordering unit (cheap, in CI): extend the existing
   generic sorter parametrization (`tests/unit/execution/test_ooc_external_sort_generic.py`),
   which varies the KEY type while the payload stays binary, to also carry a
   `large_string` AND `large_binary` NON-KEY payload that spills across >=2 runs, and
   assert globally sorted order byte-identical to an in-memory reference sort. Extend the
   existing test rather than adding a parallel one.
4. Forced-overflow reproducer (opt-in, NOT in default CI): a test gated by an explicit
   environment `skipif` evaluated BEFORE any heavy allocation
   (`@pytest.mark.skipif(not os.environ.get("DECOY_RUN_HEAVY"), reason=...)`), never a
   bare marker. (`slow` is not a registered marker and `pyproject.toml:402` does not
   deselect it, so a marker alone would run in the default suite.) The test must force ONE
   pre-sort run whose single masked payload column exceeds 2^31 bytes: use a run cap LARGE
   enough to hold one over-limit payload column plus the other columns, offsets, and
   sort copies/indices (NOT a low cap, which just makes smaller runs), feed individually
   int32-representable batches that accumulate into that one run, and assert the actual
   pre-sort run's payload bytes exceed the limit. Demonstrate the SAME configuration
   raises `ArrowInvalid` WITHOUT the widening and completes WITH it (A/B inside the test).
   Document the opt-in command and the multi-GB memory requirement; it is off by default
   because the devbox runs one test agent under a memory cap. The real-world proof is the
   100M GCP re-run (test 6).
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

- Primary risk is byte-parity: the large -> plain round-trip must be identical for
  nulls, empty values, unicode, embedded NUL, and non-UTF-8 binary. Test 2 is the gate.
- Known limitation (not a regression; see Scope): the per-batch int32 ceiling on emit and
  on the DuckDB fetch remains. A single 65,536-row batch whose masked column exceeds 2^31
  bytes is out of scope and would still raise. Documented in the module docstring and the
  D4 record; follow-up is byte-aware batching or a fail-closed clear error. The D4 fixture
  is far under this ceiling, so it does not block the scale proof.
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

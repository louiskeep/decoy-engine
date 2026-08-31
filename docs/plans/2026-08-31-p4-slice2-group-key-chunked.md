---
Status: plan
---

# Phase 4 Slice 2: group_key on the chunked route

Part of Part 2 Phase 4 (docs/plans/2026-08-31-part2-phase4-plan.md). Second dependency-closed
slice after slice 1 (windowed_date). Admits the `group_key` masking strategy onto the bounded
pandas chunked route so a large single-table group_key job streams in O(chunk) memory instead of
running full-frame on the oracle. Byte-identical to the pinned pandas oracle, or it stays on the
oracle (Cam's hard rule, 2026-08-31: each strategy exactly as its current Python code).

## 1. Why this slice, what it changes, and the exact contract it must preserve

`group_key` derives a stable synthetic key for every row that shares a `group_by` sibling-column
value (household-coherence, Mask M4). Its entire computation (`transforms/group_key.apply_group_key`)
is:

```
for raw_val in df[group_by_col]:
    out = config.prefix + derive(seed, "group_key/<col>", str(raw_val).encode())[:n].hex()
```

The output of each row is a pure function of `(job_seed, column-namespace, that row's own group_by
cell)`. It is NOT position-keyed and NOT whole-column-stateful: the `key_cache` in `apply_group_key`
is per-call memoisation of an idempotent function, not accumulated state. So for any chunking of the
rows, each row computes the identical key it would in a full-frame run, and concatenated chunk output
equals full-frame output. This is the same value-keyed contract the chunked route already guarantees
for `hash`/`date_shift`/`bucketize` (module docstring, `_chunked.py`), except group_key keys on a
SIBLING column rather than its own column.

The design table (part2 plan, line 162) classifies group_key as `PORT (clean)`: "draw is per-row
source-keyed; grouped into the cross-row set by convenience, NO correctness blocker." It sits in
`out_of_core/_compat.py::_CROSS_ROW_STRATEGIES` today, which is a conservative over-rejection on the
OUT-OF-CORE route (that route's per-column kernel does not receive same-row sibling context, the same
gap that defers `derived`). This slice does NOT touch the OOC route. It targets the pandas CHUNKED
route, exactly as slice 1 did, because that route already exists, already bounds memory, and runs the
real `apply_group_key` handler unchanged per chunk, giving byte-parity by construction rather than by
re-implementation. A later slice can port group_key to the native columnar route (P4-C) once the
native `_dispatch.py` decomposition it owes is done; that is out of scope here.

What this slice changes (all additive; no existing admitted strategy changes behaviour):

1. Admit `group_key` on the chunked route via a NEW admitted set, `CHUNK_SIBLING_KEYED_STRATEGIES`,
   kept DISJOINT from `CHUNK_SAFE_STRATEGIES`, and folded into the `_CHUNK_ADMITTED_STRATEGIES`
   union `check_chunked_compatibility` already scans.
2. Union each group_key column's `group_by` sibling into the lossless nullable-Int64 ingest set on
   the pandas adapter and the sequential runner, mirroring `top_code_columns` exactly.

The contract it must preserve, unchanged:

- Byte-identity to the pinned pandas oracle (`run_pipeline` full-frame) for values, order, nulls,
  and warnings, on the real `run_pipeline(auto_chunk=True)` route (mode == "chunked").
- FK referential integrity: group_key must NOT become admissible as an FK self-mask key.
- Cross-substrate value parity (polars adapter), per tests/parity/SEMANTIC_DIFFERENCES.md.

## 1a. The correctness traps (each is a gate item)

- **Trap A - FK self-mask exclusion.** `CHUNK_SAFE_STRATEGIES` is reused verbatim by
  `_chunked_fk.gate_fk_child_edges` as the FK-self-mask allowlist, which assumes every member is
  keyed on the column's OWN value (so parent and child compute the same masked bytes from the same
  raw key). group_key is keyed on a SIBLING (`group_by`) value; a group_key FK child would derive
  from its own group_by cell under its own column-namespace, which generally differs from the
  parent's, silently breaking RI for matched keys. Therefore group_key goes in a SEPARATE set
  (`CHUNK_SIBLING_KEYED_STRATEGIES`), never `CHUNK_SAFE_STRATEGIES`, so an FK edge keyed on a
  group_key column stays fail-closed rejected by the existing `chunked_fk_parent_strategy_not_safe`
  gate exactly as before this slice. This mirrors slice 1's separate `CHUNK_DGRN_STRATEGIES`.

- **Trap B - int+null group_by float64 widening (chunk-boundary hazard).** group_key does
  `str(raw_val)` on the group_by cell. If group_by is an integer column that carries nulls, a bare
  `to_pandas()` widens int+null to float64 in some chunks and not others (a chunk is only widened if
  IT carries a null), so `str(5)` vs `str(5.0)` diverges by chunk boundary, breaking byte-identity.
  This is the identical hazard `top_code` faces (`top_code_columns`, HC-3b). Fix: a new
  `group_key_group_by_columns(plan, registry)` helper (mirroring `top_code_columns`) whose columns
  are unioned into `fk_columns_for_table(...)` on both the pandas adapter and the sequential runner,
  so the group_by column ingests losslessly (nullable Int64) on every route and every chunk. Genuine
  float / string group_by columns are unaffected (float64 on every route already; strings never
  widen). NOTE this is a lossless-ingest union ONLY, NOT a pre-mask snapshot: unlike date_shift's
  group anchors (which must read pre-mask), group_key on the chunked route replicates the oracle's
  own per-frame work ordering per chunk, so if the group_by column is itself masked, both the chunk
  and the full frame read it at the identical point in the order.

- **Trap C - output dtype is chunk-invariant (verify, expected clean).** group_key returns a string
  (`prefix + hex`) for EVERY row, including a null group_by (`str(NaN)` -> "nan" -> a derived key),
  so the output column is always a string column regardless of chunk content. Unlike `bucketize`
  (which falls through to the original value on null, making its dtype content-dependent), group_key
  has no null-fallthrough and needs no runtime source gate. Confirm no `concat_masked_chunks` schema
  disagreement can arise.

- **Trap D - `when` predicate parity (verify, expected admit).** Unlike windowed_date (position-
  keyed, so `when`-filtered enumeration desyncs, hence slice-1 rejected it), group_key is value-keyed
  on the sibling, so a `when`-gated group_key masks only matching rows, each still deriving from its
  own group_by cell, position-independent -> byte-parity holds. The auto-chunk planner independently
  rejects ALL `when` for auto-routing (`when_predicate_not_chunk_stable`), so `when` never reaches
  the auto route; the manual `run_mask_pipeline_chunked` entry must be confirmed to handle a value-
  keyed `when` column exactly as it already does for `hash`/`date_shift`. No group_key-specific
  `when` rejection is added (that would diverge from the value-keyed strategies' behaviour).

## 2. Tasks (ordered; each keeps the tree green)

1. **`CHUNK_SIBLING_KEYED_STRATEGIES` set + admission.** In `_chunked_fk.py` (beside
   `CHUNK_SAFE_STRATEGIES`) or a small sibling, define
   `CHUNK_SIBLING_KEYED_STRATEGIES = frozenset({"group_key"})` with a docstring stating WHY it is
   disjoint from `CHUNK_SAFE_STRATEGIES` (Trap A). Extend the `_CHUNK_ADMITTED_STRATEGIES` union in
   `_chunked.py` to include it. No change to `gate_fk_child_edges` (it keeps using
   `CHUNK_SAFE_STRATEGIES`), so the FK fail-closed behaviour is unchanged by construction. Watch the
   `_chunked.py` LOC ceiling (648, at ceiling now): the union line already exists; adding one set to
   it and one import is the only `_chunked.py` growth, so put the set + its docstring in
   `_chunked_fk.py` (or a `_chunked_sibling.py` sibling) to keep `_chunked.py` at/under 648.

2. **`group_key_group_by_columns(plan, registry)` helper.** In `_runner.py`, directly mirroring
   `top_code_columns` / `date_shift_group_columns`: walk `build_work_list`, for each scalar
   `group_key` node read `provider_config.group_by` (a validated non-empty string, guaranteed by
   `check_group_key_refs`) and collect `{table: {group_by_col, ...}}`.

3. **Lossless ingest union (pandas adapter).** In `_pandas_adapter.run()`, union
   `group_key_group_by_columns(plan, registry)` into the per-table lossless set alongside
   `group_anchor_cols` and `top_code_cols` (the existing `fk_columns_for_table(...) | ... ` union).
   No pre-mask snapshot (Trap B note).

4. **Lossless ingest union (sequential runner).** The same union on the sequential FK route
   (`_sequential.py`), mirroring how `top_code_columns` is already unioned there, so the non-chunked
   sequential route ingests group_by losslessly too (keeps cross-route parity; sequential is a
   parity oracle for FK jobs). Confirm whether `_sequential.py` needs it (only if a group_key job can
   route sequential); if the sequential route never carries group_key group_by lossless-sensitive
   data, document why it is a no-op rather than adding a dead union. LOC ceilings: `_pandas_adapter.py`
   (662) and `_sequential.py` (641) are allowlisted; keep each addition to the minimal union line +
   the helper import, and if a ceiling is crossed, prefer moving the helper call rather than raising
   (raise only with a documented justification per the ratchet, as slice 1 did for the 2-line
   plumbing).

5. **Confirm the auto-chunk planner needs no group_key gate.** `_planner._reasons_*` reuses
   `check_chunked_compatibility` (line 376), so admitting group_key there auto-admits it for
   `run_pipeline(auto_chunk=True)`. Verify group_key introduces no whole-column-state hazard needing
   an entry in `_whole_column_state_rejections` (it does not: output always string, group_by handled
   losslessly, no format detection). Add a one-line code comment at the group_key admission site
   pointing to this reasoning; do NOT add a planner rejection.

## 3. Tests (the parity + mutation bar)

Parity is asserted on the REAL `run_pipeline(auto_chunk=True)` route with `result.mode == "chunked"`
(never a hand-rolled chunk loop), against the same config run full-frame, byte-comparing the output
table (values, order, dtype) and the warnings. New file `tests/unit/execution/test_group_key_chunked.py`:

1. **Byte-identity across chunkings.** A single table with a group_key column (group_by = an entity
   id with repeated values across chunk boundaries) at chunk sizes 1 / 7 / 500 vs full-frame: assert
   identical output and that the same group_by value in different chunks yields the identical key
   (household coherence survives chunking).
2. **int+null group_by (Trap B).** group_by = nullable Int64 with values >= 2**53 and nulls placed so
   that some chunks carry a null and some do not; assert chunked == full-frame == exact-integer-keyed
   (a red test against the un-unioned baseline: without the lossless union, a widened chunk keys on
   "5.0" and diverges).
3. **FK exclusion (Trap A).** A config where a group_key column is an FK parent/child key: assert the
   chunked route rejects it (`chunked_fk_parent_strategy_not_safe`) and the job runs full-frame, and
   that adding group_key did NOT admit a group_key FK self-mask (the RI regression guard: a
   set-membership test that group_key not in CHUNK_SAFE_STRATEGIES, plus an end-to-end FK job).
4. **group_by itself masked (Trap B ordering).** group_by column is also masked by a chunk-safe
   strategy: assert chunked == full-frame (both read group_by at the identical point in the work
   order).
5. **`when`-gated group_key (Trap D).** A group_key column with a `when` predicate on the manual
   `run_mask_pipeline_chunked` entry: assert chunked == full-frame (matching rows keyed, non-matching
   passed through), confirming value-keyed `when` parity (contrast: the auto route rejects `when`).
6. **Cross-substrate (Trap C/polars).** The same jobs under the polars adapter: assert value-equal to
   the pandas oracle (Arrow-schema differences per SEMANTIC_DIFFERENCES.md allowed, values/keys equal).
7. **Output dtype invariance (Trap C).** All-null group_by chunk vs mixed: assert
   `concat_masked_chunks` raises no schema disagreement and the output column is string on every chunk.

Mutation bar (Phase 2-3 discipline): full-grade the NEW units to zero unadjudicated survivors on
changed lines - `group_key_group_by_columns` and `CHUNK_SIBLING_KEYED_STRATEGIES` admission logic.
The changed lines in the large allowlisted files (`_pandas_adapter.py`, `_sequential.py`,
`_chunked.py`) are minimal union/threading; hand-verify their changed-line mutants (same standard as
slice 1's 6 changed-line mutants).

## 4. Acceptance

- BYTE-IDENTICAL output + warnings to the pinned pandas oracle on the real chunked route, across
  chunkings, int+null group_by, group_by-masked, and `when` cases. Exact, or group_key stays on the
  oracle.
- The intended chunked route provably ran (`result.mode == "chunked"`); oracle completion or silent
  fallback on an admitted job is a gate failure.
- FK RI unchanged: group_key stays a fail-closed FK-key MISS; no existing parity or FK contract
  weakens.
- ruff + format + mypy (CI py3.10) clean; module-size sentry green (no ceiling raised without a
  documented, load-bearing justification).
- dennis + Codex per-slice gate green.

## 5. Risks (explicit review gates)

- **A hidden whole-column dependency in the group_key handler** would break chunk-parity silently.
  Mitigation: parity asserted on the real route; the handler is read-verified as pure per-row.
- **The sequential-runner union (Task 4) could be a no-op or a real requirement** depending on whether
  a group_key job ever routes sequential with a lossless-sensitive group_by. Resolve by tracing the
  routes at build time; do not add a dead union, and do not omit a needed one.
- **LOC ceilings.** Keep the sets/helpers in siblings; do not raise a ceiling except for irreducible
  plumbing with a documented justification (slice-1 precedent).

## 6. Sequencing

Slice 2 of Phase 4. Stacked on feat/native-phase3 (holds slice 1 + design docs), HELD - per Cam
(2026-08-31) Phase 4 merges ONCE at the end after all testing is complete; no incremental merge.
Follow-on slices (Cam-sequenced): native-route ports of the OOC-already-streaming payload strategies
(P4-C, needs the `_dispatch.py` decomposition first), PREPASS strategies (P4-D: top_code, derived_
aggregate), FK streaming + BoundedExternalSorter (P4-A), grouped_series (P4-E, proof-gated).

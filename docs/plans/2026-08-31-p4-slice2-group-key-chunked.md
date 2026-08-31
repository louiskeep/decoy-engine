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

- **Trap C - output dtype is chunk-invariant WITHOUT `when` (verify, expected clean).** With no
  `when`, group_key returns a string (`prefix + hex`) for EVERY row, including a null group_by (which
  stringifies to a dtype-specific sentinel: `"<NA>"` for nullable Int64, `"nan"` for float64,
  `"None"` for object, etc. -- the exact sentinel is irrelevant to parity because the chunked route
  and the oracle ingest the group_by column identically, so both stringify the same null the same
  way). The output column is therefore a string column on every chunk regardless of content. Unlike
  `bucketize` (null-fallthrough makes its dtype content-dependent), group_key has no null-fallthrough
  in the no-`when` case. Confirm no `concat_masked_chunks` schema disagreement arises.

- **Trap D - `when` predicate is REJECTED on the chunked route (fail-closed).** A `when`-gated
  group_key masks only matching rows and passes NON-matching rows through with their ORIGINAL value
  and dtype. If the target column is non-string, a chunk of all-non-matching rows keeps the numeric
  dtype while a chunk containing matches becomes object/string -- a chunk-boundary-dependent output
  dtype, and `concat_masked_chunks` then raises `chunked_schema_mismatch` where the full-frame oracle
  produces one mixed column, a route divergence (Codex plan-gate HIGH). group_key is value-keyed so
  it does not have windowed_date's enumeration-desync reason, but it has this dtype-mixing reason, so
  it is rejected the same way: a new `reject_group_key_when` gate raises
  `chunked_group_key_when_not_supported` at the public `run_mask_pipeline_chunked` entry (the auto-
  chunk planner already rejects ALL `when` via `when_predicate_not_chunk_stable`, so this only has to
  cover the manual entry). Fail-closed -> the job runs full-frame on the oracle.

- **Trap E - equality-colliding heterogeneous group_by (cache collision), fail-closed guard.**
  `apply_group_key`'s `key_cache` keys on the RAW Python value but the derivation keys on
  `str(raw_val)` (`transforms/group_key.py`). Two values that are Python-`==` and hash-equal but have
  DIFFERENT `str()` (e.g. `True`/`1` -> `"True"`/`"1"`, or `1`/`1.0` -> `"1"`/`"1.0"`) collide in the
  cache: whichever appears FIRST in a call wins, so `1` after `True` in the full frame gets
  `derive("True")`, but `1` alone in its own chunk gets `derive("1")` -- a byte-parity break across
  chunk boundaries. This makes the cache observable per-call state, not pure memoisation, for such
  columns. The collision is REACHABLE even within ONE monomorphic type: a `float64` column can hold
  `0.0` and `-0.0`, which are `==` and hash-equal but stringify to `"0.0"` and `"-0.0"` (Codex
  re-gate BLOCKER; `NaN` is never `==` so it never dedups). The heterogeneous family (`True`/`1`,
  `1`/`1.0`) is unreachable from a single Arrow column, but the signed-zero case is not, so the guard
  must exclude FLOATING types (and float-valued dictionaries), not merely non-monomorphic ones. We do
  NOT change `apply_group_key` (that would change the oracle's output, violating the hard rule). The
  fix is a fail-closed EFFECTIVE-TYPE guard, two placements mirroring how `bucketize` is gated:

  - SAFE SET (O(1) schema-only): `integer`, `decimal`, `boolean`, `string`, `large_string`, `date`,
    `timestamp`. FLOATING is EXCLUDED (the `0.0`/`-0.0` collision). `dictionary` is safe only if its
    VALUE type is in the safe set, resolved recursively (a float-valued dictionary is unsafe). Any
    other / unknown type is unsafe.
  - EFFECTIVE type, not just source type: the guard checks the type of the group_by column AT THE
    POINT group_key reads it. If group_by is itself masked by another strategy, that is the masking
    strategy's STATIC output type (from the per-strategy output-type map, e.g.
    `_mem_estimate_schema`); if group_by is unmasked, its source Arrow type. If group_by is masked by
    a strategy whose output type is DYNAMIC / not statically known, treat it as unsafe (fail-closed).
    This covers a preceding mask that turns a safe source into a float. The common household-
    coherence case (group_by = a raw or hash-masked id column) has an integer / string effective type
    and stays admitted.
  - AUTO route placement: a rejection in `_planner._runtime_source_rejections` (which sees the source
    tables + the plan) rejects a table whose group_by EFFECTIVE type is unsafe, so the job runs
    full-frame on the oracle from the start (CLEAN fallback, no mid-stream error).
  - MANUAL `run_mask_pipeline_chunked` placement: a per-chunk guard RAISES
    `chunked_group_key_group_by_dtype_unsupported` fail-closed (the caller chose chunked explicitly;
    same shape as slice 1's per-chunk DGRN / FK dtype guards). At this layer the guard checks the
    group_by column's actual type in the chunk (which reflects any masking that has run) plus the
    plan's static output type for a not-yet-masked group_by.

  Both are O(1) per column/chunk. The decisive reachable counterexample -- `0.0`/`-0.0` in one
  float64 column split across a chunk boundary -- is a required test, alongside recursive-dictionary
  cases and an auto-route test proving an unsafe type selects the oracle BEFORE streaming.

## 2. Tasks (ordered; each keeps the tree green)

1. **`CHUNK_SIBLING_KEYED_STRATEGIES` set + admission + the two group_key gates, in a new
   `_chunked_group_key.py` sibling.** To keep `_chunked.py` at/under its 648 ceiling (it is AT the
   ceiling now) and mirror slice 1's `_chunked_dgrn.py`, put all group_key-specific logic in a new
   `_chunked_group_key.py`: define `CHUNK_SIBLING_KEYED_STRATEGIES = frozenset({"group_key"})` with a
   docstring stating WHY it is disjoint from `CHUNK_SAFE_STRATEGIES` (Trap A); and the two gate
   functions from tasks 1b/1c below. Extend the `_CHUNK_ADMITTED_STRATEGIES` union in `_chunked.py`
   to include the set (one term added to the existing union line), and call the two gates from
   `check_chunked_compatibility` / the per-chunk loop (one call site each). No change to
   `gate_fk_child_edges` (it keeps using `CHUNK_SAFE_STRATEGIES`), so FK fail-closed behaviour is
   unchanged by construction.

   LOC accounting (`_chunked.py` is AT its 648 ceiling now): the irreducible `_chunked.py` growth is
   one import of `_chunked_group_key`, one in-place term in the `_CHUNK_ADMITTED_STRATEGIES` union
   (no new line), and the two gate call sites (`reject_group_key_when` in
   `check_chunked_compatibility`, `reject_unsafe_group_key_group_by_dtype` in the per-chunk loop) --
   about 3 new lines. Prefer an extraction to stay at/under 648 (fold the two calls behind a single
   `_chunked_group_key` façade so only one call site is added at each of the two points; consolidate
   imports). If a small residual crossing remains after that, raise the ceiling by EXACTLY that
   amount WITH a documented justification naming the irreducible gate-call plumbing and the standing
   decomposition target (the slice-1 `_pandas_adapter.py` +2 precedent), never a silent bump. The
   build states the final `_chunked.py` LOC.

   1b. **`reject_group_key_when(table_cfg, table)` gate (Trap D).** Mirror
   `_chunked_dgrn.reject_windowed_date_when`: raise `PlanCompileError(code=
   "chunked_group_key_when_not_supported", ...)` when any `group_key` column carries a `when`
   predicate. Call it from `check_chunked_compatibility` (the public entry; the auto planner already
   blanket-rejects `when`). Message: the passthrough of non-matching rows makes the output dtype
   chunk-boundary-dependent.

   1c. **group_by EFFECTIVE-type guard (Trap E), two placements mirroring bucketize.** A shared
   predicate `group_by_type_is_safe(arrow_type) -> bool` for the safe set (integer / decimal /
   boolean / string / large_string / date / timestamp; FLOATING EXCLUDED; `dictionary` safe iff its
   value type is safe, recursively; unknown -> unsafe). The EFFECTIVE group_by type = the masking
   strategy's static output type if group_by is itself masked (dynamic/unknown -> unsafe), else the
   source Arrow type. (i) MANUAL entry: `reject_unsafe_group_key_group_by_dtype(chunk, plan, table,
   group_by_cols)` raises `ExecutionError(code="chunked_group_key_group_by_dtype_unsupported", ...)`
   for an unsafe effective type, called in the per-chunk loop of `run_mask_pipeline_chunked` alongside
   the existing per-chunk FK guards (O(1)). (ii) AUTO route: an effective-type rejection in
   `_planner._runtime_source_rejections` (which sees the source tables + plan) rejects a table whose
   group_by effective type is unsafe, so the auto route falls back to full-frame cleanly instead of
   erroring mid-stream. Both consume the group_by column set from task 2's helper and the per-strategy
   static-output-type map (e.g. `_mem_estimate_schema`).

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

5. **Auto-chunk planner wiring.** `_planner._reasons_*` reuses `check_chunked_compatibility`
   (line 376), so admitting group_key there (task 1) auto-admits it for `run_pipeline(auto_chunk=
   True)`, and the `when` rejection (task 1b) flows through too. group_key introduces no WHOLE-COLUMN-
   STATE hazard needing an entry in `_whole_column_state_rejections` (output always string, group_by
   handled losslessly, no format detection). It DOES need the runtime-source dtype rejection (task
   1c-ii) added to `_runtime_source_rejections` so an unsafe group_by dtype routes to the oracle
   cleanly. Wire task 1c-ii here.

## 3. Tests (the parity + mutation bar)

Parity is asserted on the REAL `run_pipeline(auto_chunk=True)` route with `result.mode == "chunked"`
(never a hand-rolled chunk loop), against the same config run full-frame, byte-comparing the output
table (values, order, dtype) and the warnings. New file `tests/unit/execution/test_group_key_chunked.py`:

1. **Byte-identity across chunkings.** A single table with a group_key column (group_by = an entity
   id with repeated values across chunk boundaries) at chunk sizes 1 / 7 / 500 vs full-frame: assert
   identical output and that the same group_by value in different chunks yields the identical key
   (household coherence survives chunking).
2. **Literal pinned expected key (anti self-confirmation, Codex MEDIUM).** Store a LITERAL expected
   key string (a hex constant generated ONCE from a fixed job_seed, namespace, and source value, then
   hardcoded in the test), and assert both the chunked and full-frame outputs equal that constant.
   A constant, not a `derive(...)` call in the test: the derive-in-test form is a useful ingestion
   check but is not an independent oracle (it recomputes rather than pins). This catches a shared-
   ingestion bug that a chunked == oracle comparison alone would hide.
3. **int+null group_by (Trap B).** group_by = nullable Int64 with values >= 2**53 and nulls placed so
   that some chunks carry a null and some do not; assert chunked == full-frame == exact-integer-keyed.
   A RED test against the un-unioned baseline (without the lossless union, a widened chunk keys on
   "5.0" and diverges).
4. **Null-sentinel per dtype (Trap B/C).** group_by nullable Int64 (null -> "<NA>"), float64 (null ->
   "nan"), and string (null -> the object sentinel): assert chunked == full-frame for each; the point
   is not the sentinel value but that both routes agree because ingest is identical.
5. **Cache-collision (Trap E, Codex BLOCKER) - decisive reachable counterexample.**
   (a) A `float64` group_by column containing `0.0` and `-0.0` split across a chunk boundary: assert
   the AUTO route routes this job to the ORACLE (not chunked) BEFORE streaming (unsafe effective
   type), and that a forced chunked run would diverge (documents WHY float is excluded). This is the
   reachable monomorphic collision, the decisive case.
   (b) A `dictionary`-typed group_by with a FLOAT value type: assert unsafe (recursive), routed to
   oracle; a dictionary with an integer/string value type: assert safe, chunked, parity holds.
   (c) group_by masked by a strategy with a float static output type: assert the effective-type guard
   rejects it (routes to oracle) even though the SOURCE type is safe.
   (d) Direct unit tests of `group_by_type_is_safe` (float -> False, int/string/date -> True,
   float-dict -> False, int-dict -> True, unknown -> False) and both gate wrappers (manual raises
   `chunked_group_key_group_by_dtype_unsupported`; the planner rejection selects the oracle).
6. **FK exclusion + group_by-as-FK-key (Trap A, Codex).** (a) A group_key column that is an FK
   parent/child key (single-column edge, `orphan_policy: remap`, so an earlier gate does not mask the
   intended rejection): assert `chunked_fk_parent_strategy_not_safe` and full-frame fallback, plus the
   RI guard that group_key is not in `CHUNK_SAFE_STRATEGIES`. (b) A DIFFERENT case where the group_by
   SIBLING is itself an FK key: assert chunked == full-frame (the FK column ingests losslessly and
   group_key derives from it consistently).
7. **group_by itself masked, mask-before-group_key (Trap B ordering).** group_by column is masked by
   a chunk-safe strategy AND ordered (via the work order) to run BEFORE group_key; assert chunked ==
   full-frame AND that the sibling mask actually executed first (assert the derived key is from the
   MASKED group_by value, not the raw one -- the opposite ordering would not be meaningful coverage).
8. **`when`-gated group_key is REJECTED (Trap D).** A group_key column with a `when` predicate:
   assert the manual `run_mask_pipeline_chunked` raises `chunked_group_key_when_not_supported` and
   (separately) that a non-string target column with matches and non-matches split across chunks
   would diverge (documents WHY the rejection exists), plus the auto route rejects it via the
   existing planner gate.
9. **Cross-substrate (polars).** The same admitted jobs under the polars adapter: assert value-equal
   to the pandas oracle (Arrow-schema differences per SEMANTIC_DIFFERENCES.md allowed, keys equal)
   AND that the intended chunked route actually ran (`result.mode == "chunked"`).
10. **Output dtype invariance, no-`when` (Trap C).** All-null group_by chunk vs mixed: assert
    `concat_masked_chunks` raises no schema disagreement and the output column is string on every chunk.

Mutation bar (Phase 2-3 discipline): full-grade the NEW units to zero unadjudicated survivors on
changed lines - `group_key_group_by_columns`, `CHUNK_SIBLING_KEYED_STRATEGIES` admission, and both
gate functions (`reject_group_key_when`, `reject_unsafe_group_key_group_by_dtype`). The changed lines
in the large allowlisted files (`_pandas_adapter.py`, `_sequential.py`, `_chunked.py`) are minimal
union/threading/call-site; hand-verify their changed-line mutants (same standard as slice 1's 6
changed-line mutants).

## 4. Acceptance

- BYTE-IDENTICAL output + warnings to the pinned pandas oracle on the real chunked route, for the
  ADMITTED cases: across chunkings, int+null group_by, safe-typed group_by (including a hash-masked
  group_by). Exact, or group_key stays on the oracle.
- The FAIL-CLOSED cases route correctly, not to a wrong output: a `when`-gated group_key rejects at
  the manual entry (`chunked_group_key_when_not_supported`) and the auto route falls back to the
  oracle (planner `when` gate); a float / unsafe-effective-type group_by falls back to the oracle on
  the auto route and raises `chunked_group_key_group_by_dtype_unsupported` at the manual entry.
- The intended route provably ran: for an admitted job `result.mode == "chunked"`; for a rejected
  job the oracle ran. Silent fallback on an ADMITTED job, or a chunked run of a REJECTED shape, is a
  gate failure.
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

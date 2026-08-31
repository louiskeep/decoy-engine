Status: plan

# Phase 4 Slice 1: durable row offset -> windowed_date on the chunked route

The first Phase 4 implementation slice, scoped by a repo architecture map and hardened by the Codex
plan-gate (4 BLOCKERs remediated below). It is the smallest dependency-closed, byte-identical slice:
consume the already-computed-but-inert durable global row offset so `windowed_date` runs on the
bounded-memory chunked route instead of forcing a full-frame oracle pass. It validates the Phase 4
build loop and the durable-row-number (DGRN) consumption pattern that later position-keyed strategies
reuse.

Design doc: `docs/plans/2026-08-31-part2-phase4-plan.md`. Hard rule (binding): the streamed output MUST
be byte-identical to the current pandas oracle, tested to the Phase 2-3 parity + mutation bar, or the
strategy stays on the oracle.

## 1. Why this slice, what it changes, and the exact contract it must preserve

Today a table with a `windowed_date` column cannot run on any streaming route: it is absent from
`CHUNK_SAFE_STRATEGIES` so `_chunked.py::check_chunked_compatibility` rejects it, and it is
`needs_global_row_identity=True` with no native kernel so `native/_dispatch` rejects it too. It always
runs full-frame (O(table) resident), which OOMs at scale.

`windowed_date`'s contract (`transforms/windowed_date.py:181-215`): for row `i`,
`row_seed = int.from_bytes(derive(mask_key, "windowed_date/{col}", i.to_bytes(8,"big"))[:8],"big")`, then
`default_rng(row_seed).integers(...)` for the offset, added to the anchor, formatted `%Y-%m-%d`. `i` is
the GLOBAL row index. `_chunked.py::run_mask_pipeline_chunked` already computes that global index as
`base_row_offset` (offset BEFORE the chunk) + within-chunk position, advancing by `chunk.num_rows` after
each chunk (`_chunked.py:404,449,638`, Task 1.1). The value is INERT: never passed to the adapter, never
read. Consuming it lets `windowed_date` produce byte-identical output on ordered, contiguous,
non-filtering chunks. No new algorithm, no sort, no join, no kernel.

Contract facts the Codex gate confirmed or corrected, which BOUND admission (get these wrong and the
output is not byte-identical):

- **`when:` is NOT admissible.** `run_with_when_gate` passes only MATCHING rows to the handler
  (`_when_gate.py:208`), so the oracle enumerates the FILTERED subset `0..matches-1`. A chunk enumerating
  from its physical `row_offset` diverges whenever preceding rows do not match. `windowed_date` + `when`
  MUST be rejected from the chunked route (a durable per-column matched-row counter, not DGRN, would be
  needed to support it). The auto-planner already rejects all `when`, but the PUBLIC chunked entry point
  does not, so this slice adds the explicit rejection.
- **Null anchors RAISE, they do not produce a value.** `apply_windowed_date` calls `strftime`
  unconditionally; `None`/`pd.NA`/`pd.NaT`/`NaN` raise `ValueError: NaTType does not support strftime` in
  the pinned environment. This is the EXISTING oracle behavior and this slice does NOT change it. So the
  parity story for nulls is a FAILURE-path story: BOTH routes raise and FAIL THE JOB on a null anchor. The
  difference is only WHEN the raise happens (full-frame: before any output; chunked: during iteration,
  possibly after earlier chunks were yielded to the caller). But `run_mask_pipeline_chunked` returns an
  ITERATOR and owns no sink; the caller (route-exec / `run_pipeline`) materializes it and only then writes
  its own sink. So a null-anchor chunk raises during iteration, materialization aborts, `run_pipeline`
  never reaches its sink write, and NO committed output results. The contract is "identical final result:
  both raise the same error and produce no committed output", not "identical byte stream up to the error".
  The slice VERIFIES this on the real route (a null-anchor `run_pipeline(auto_chunk=True)` raises and
  commits nothing), not by asserting a sink mechanism the chunked coordinator does not own.
- **`i.to_bytes(8,"big")` accepts only `0 <= i <= 2**64-1` (inclusive).** The entry point receives an
  arbitrary `Iterable[pa.Table]` with no row count, so the range cannot be validated up front.
  `base_row_offset` is validated at entry (`int`, `0 <= base <= 2**64-1`); the RANGE is then checked PER
  CHUNK as each chunk is masked (`chunk_base_offset + chunk.num_rows - 1 <= 2**64-1`), raising a DELIBERATE
  coded error at the offending chunk rather than an incidental `OverflowError`. No whole-stream buffering,
  no row-count parameter.

Explicit scope boundaries: target the `_chunked.py` PANDAS chunked route (bounded O(chunk) memory using
the SAME `apply_windowed_date` the oracle runs; the byte-identity argument is proof-by-construction).
Do NOT put `windowed_date` on the native Rust-kernel route, do NOT build the `global_row_number` prepass
(only a declared tag today), do NOT include `sequence` (generation-only, no streaming coordinator).
Those are separate follow-on slices.

## 2. Tasks (ordered; each keeps the tree green)

1. **`StrategyContext.row_offset`** (`execution/_adapter.py`): add `row_offset: int = 0`. Default 0 so
   every existing construction and the full-frame path are byte-unchanged. Update the public
   `ExecutionAdapter` protocol signature (`_adapter.py:237`) so both adapters conform.
2. **Thread the offset through BOTH adapters AND the polars pandas-fallback.** Add `row_offset: int = 0`
   to `PandasExecutionAdapter.run` (`_pandas_adapter.py:170-234`) and pass it into the `StrategyContext`
   construction (`:226`). `windowed_date` is NOT polars-native, so `PolarsExecutionAdapter.run`
   (`polars/_polars_adapter.py:120-236`) reaches it via `_run_via_pandas_oracle` -> `self._pandas.run(...)`
   (`_polars_adapter.py:318`). Thread `row_offset` the FULL path: `PolarsExecutionAdapter.run` ->
   `_run_via_pandas_oracle` -> `PandasExecutionAdapter.run` -> `StrategyContext`. Both entry points and
   the fallback must forward it, or the polars substrate silently restarts `i` at 0 per chunk.
3. **Pass the offset at the call site** (`_chunked.py:417-426`): pass `row_offset=row_offset` into
   `adapter.run(...)` (already computed and correctly ordered; today discarded after `_advance_row_offset`).
4. **DGRN domain guard** (two levels, because the entry point has no row count -- it takes an arbitrary
   `Iterable[pa.Table]`): (i) at entry, validate `base_row_offset` is an int with `0 <= base <= 2**64-1`;
   (ii) PER CHUNK, before masking a chunk whose base offset is `off`, check `off + chunk.num_rows - 1 <=
   2**64-1`. Either check failing raises a deliberate coded error (e.g. `chunked_row_offset_out_of_domain`)
   -- never an incidental `OverflowError` inside `i.to_bytes(8)`. No whole-stream buffering, no row-count
   parameter.
5. **Admit `windowed_date` via a SEPARATE set** (`_chunked.py`): add
   `CHUNK_DGRN_STRATEGIES = frozenset({"windowed_date"})` and admit it in `check_chunked_compatibility`'s
   column loop (`:228-239`). DO NOT add `windowed_date` to `CHUNK_SAFE_STRATEGIES`: that set is reused
   verbatim by `_chunked_fk.gate_fk_child_edges` as the FK-self-mask VALUE-keyed allowlist
   (`_chunked_fk.py:70,74-86`); `windowed_date` is POSITION-keyed, so a child FK column self-masking under
   it would compute a different row position than its parent and silently break referential integrity.
   Keeping the sets separate is a correctness requirement; the review must confirm it.
6. **Reject `windowed_date` + `when:` on the chunked route** in `check_chunked_compatibility` with a coded
   reason (mirror the auto-planner's rejection), so the public entry point cannot admit a `when`-gated
   `windowed_date` whose filtered enumeration DGRN cannot reproduce.
7. **Characterize + gate the null-anchor contract**: confirm the oracle raises on a null anchor
   (`ValueError` from `strftime` on NaT). This slice preserves that exactly: BOTH routes raise the same
   `ValueError` and FAIL the job. The contract to hold is "identical final result: both raise the same
   error and produce NO committed output". The chunked route raises DURING ITERATION (the caller
   materializes the iterator, and `run_pipeline` never reaches its sink write on a raise), so nothing is
   committed. The slice VERIFIES this on the real route (a null-anchor `run_pipeline(auto_chunk=True)`
   raises and commits nothing), not by asserting a byte stream up to the error or a sink mechanism the
   chunked coordinator does not own.
8. **Consume the offset in the handler** (`_strategies/_windowed_date.py:49-61`): read `ctx.row_offset`,
   pass to `apply_windowed_date`.
9. **Offset the enumerate** (`transforms/windowed_date.py:181-215`): add `row_offset: int = 0` and change
   `enumerate(anchor_series)` to `enumerate(anchor_series, start=row_offset)`. The single semantic change;
   everything else (seed, RNG, format, null-raise) is preserved verbatim.

## 3. Tests (the parity + mutation bar)

- **Handler unit**: `apply_windowed_date(..., row_offset=k)` on a chunk gives, for local row `j`, the
  SAME date the full-frame call gives for global row `k+j` (direct oracle-vs-offset equality).
- **End-to-end parity via the REAL route (route evidence)**: run a `windowed_date` table through
  `run_pipeline(auto_chunk=True, substrate="pandas")` with the chunk threshold BELOW the fixture size;
  assert `quality_metrics["auto_chunk"]["mode"] == "chunked"` (the planner actually chose the chunked
  route, not full-frame), and assert the output is BYTE-identical to `run_pipeline(auto_chunk=False)`.
  Cover the matrix: multiple chunk sizes (1, a prime not dividing the row count, > table size), `min_days
  < 0`, all three `distribution` values. A single differing byte fails.
- **Base-offset boundary (correct oracle)**: for a SMALL positive `N`, prove `base_row_offset=N` makes
  local row 0 mask as global row N by comparing against a TRUE full-frame run of an `(N + rows)`-row table,
  then selecting rows `N:` -- never against another offset-aware helper (which could reproduce the same
  bug). For the `2**64-1` region (a full-frame table is infeasible there), use a DIRECT derivation oracle:
  compute the expected date for a specific large `i` straight from `derive(seed, ns, i.to_bytes(8))` +
  `default_rng(...).integers(...)` (the transform's own primitives), not by materializing a giant table.
  Test that a negative / boolean / non-int offset, and a base+range that would exceed `2**64-1`, raise the
  coded domain error (task 4), not `OverflowError`.
- **`when:` rejection regression**: `windowed_date` + a `when` clause is rejected by
  `check_chunked_compatibility` with the coded reason (task 6), and the auto-route leaves such a table
  full-frame.
- **FK-child RI coupling regression (pins the sharp risk)**: configure BOTH parent and child keys as
  MATCHING `windowed_date` strategies, `orphan_policy: remap`, satisfying every other FK gate condition,
  and assert `gate_fk_child_edges` rejects with the exact code `chunked_fk_parent_strategy_not_safe`
  (condition (a), which checks the PARENT strategy). Also assert `windowed_date not in CHUNK_SAFE_STRATEGIES`.
  This is the test that actually fails if someone folds `windowed_date` into `CHUNK_SAFE_STRATEGIES`.
- **Null-anchor failure path**: on the REAL route, a null-anchor `run_pipeline(auto_chunk=True)` raises the
  SAME `ValueError` as full-frame and commits NO output (the iterator raises during materialization, so the
  sink write is never reached) -- not a silent skip, not a different error, not partial committed output.
  Place nulls at a chunk boundary and inside an otherwise all-null chunk.
- **Cross-substrate**: the parity check under `PolarsExecutionAdapter`, pinning the repo's contract
  (Arrow/schema equality WITHIN a substrate; value parity ACROSS substrates, since polars can widen string
  types). This proves the polars pandas-fallback threads `row_offset` (task 2).
- **Mixed-strategy fixture**: a table with `windowed_date` PLUS existing chunk-safe columns (`hash`, etc.)
  in one chunked run, byte-identical to the oracle -- not only the single-column case.
- **Mutation** on the changed units (`transforms/windowed_date.py` offset, `_chunked.py` admission +
  call-site + domain guard + `when` rejection, `_windowed_date.py` handler, both adapters + the polars
  fallback), five-field denominator, zero unadjudicated survivors. The mutation set MUST explicitly kill:
  offset reset to 0, a missing chunk-call-site forward, a missing polars-fallback forward, wrong advance
  arithmetic, an accidental union with `CHUNK_SAFE_STRATEGIES`, and an accidental admission of `when`.

## 4. Acceptance

- Byte-identical to the oracle across the full chunk-size x distribution x negative-offset matrix, on the
  REAL auto-chunk route (route evidence: `mode == "chunked"`), both substrates. One differing byte fails.
- Null anchors and `when`-gated / out-of-domain configs behave EXACTLY as the oracle: a null anchor makes
  both routes raise the same `ValueError` and commit NO output (the run raises before its sink write);
  `when` and out-of-domain configs reject with a coded reason. Never a silent divergence, and never
  committed output that differs from the oracle's committed result.
- A `windowed_date` table runs at bounded O(chunk) memory; peak RSS flat across chunk counts on a moderate
  tier (the memory win).
- `windowed_date` stays rejected as an FK-child key (RI preserved); `CHUNK_SAFE_STRATEGIES` byte-unchanged.
- The full existing suite stays green (the `row_offset=0` default keeps every other path byte-identical).
- dennis + Codex gate green; nothing weakens a Part 1/Phase 3 gate. Merge only on Cam's go + green CI.

## 5. Risks (explicit review gates)

- **CHUNK_SAFE_STRATEGIES coupling** (task 5): the sharpest risk; the FK-child RI test pins it; review must
  verify the sets stay separate and the test asserts `chunked_fk_parent_strategy_not_safe` with matching
  parent+child windowed_date.
- **`when:` and null contracts** (tasks 6-7): both are byte-parity traps (filtered enumeration; strftime
  raise). Admission must reject/match the oracle, tested explicitly.
- **Polars fallback threading** (task 2): non-native strategies go through `_run_via_pandas_oracle`;
  missing the forward silently resets the offset. The cross-substrate test pins it.
- **DGRN domain** (task 4): a semantic offset must fail cleanly at the boundary, not mid-stream.
- **Scope creep**: pandas chunked route only; do NOT touch `_dispatch.py`/native kernels or the
  `global_row_number` prepass.

## 6. Sequencing

Slice 1 of the Phase 4 build loop. On green + Cam's go it merges like Phase 3. Follow-ons (separate
activation records): native-kernel `windowed_date` port; `sequence` (needs a generation streaming
coordinator); FK-4a native admission; the OOC-B bounded external sorter + never-OOM FK join.

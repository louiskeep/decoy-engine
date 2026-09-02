# P4-A Task 6: sequential per-edge reorder driver

Status: plan (authored 2026-09-02, Opus; plan-gate round 1 NO-GO 2 HIGH + 2
MEDIUM remediated -- Option A confirmed byte-safe by the gate; re-gate round 2).
Held target branch `feat/native-phase3`; merges with the Phase-4 bundle.
Greenlit by Cam after the route A/B: the reorder route is ~flat wall time as
parent-key count grows while `_batch_join` scales super-linearly (4.5x slower at
10M parent keys), so it is worth building for THROUGHPUT at the 100M-row target
(peak RSS is a wash). This slice builds the STANDALONE driver; wiring it into the
live route is Task 7 (deferred).

## 1. Why this slice

`StreamFkJoiner.run_ordered_join` (the A.3 order-restore consumer, done+held) is
per-edge and needs a driver to stage child batches, mask non-FK columns, and
resolve FK columns per table. The current live driver `_runner.py::_stream_table`
drives `ChildFkBatchJoiner` (the `_batch_join` route). Task 6 builds a new driver
`_stream_driver.py::stream_table` that drives `run_ordered_join` instead. It is
NOT a mechanical port of the salvage `_stream_driver.py`
(`origin/fix/ooc-b-memory-streaming-join`): that driver's phase-3 uses the OLD
unordered `iter_join_rows` + `JoinRowCursor` (a per-batch ORDER BY shim) and
predates `run_ordered_join`. The FRAME recon (2026-09-02) established the exact
deltas below.

## 2. Scope

IN:
- A new module `src/decoy_engine/execution/out_of_core/_stream_driver.py`
  exposing `stream_table(...)` with the salvage driver's three-phase structure
  (phase 1 raw source pass: per-edge stage + mask non-FK + payload store; phase 2
  open the ordered join per edge; phase 3 resolve from the payload store), adapted
  to `run_ordered_join`. `_runner.py` is at its ~600-LOC orchestration cap
  (size-sentry pinned at 676, `tests/sentry/test_module_size.py`), so the driver
  MUST be its own module, matching the salvage structure.
- **Phase-2 swap (the core change), with the FAIL precount folded INTO the
  loop.** Replace the salvage phase-2 line `cursors =
  [JoinRowCursor(joiner.iter_join_rows(batch_rows), ...) for ...]` AND move the
  salvage driver's earlier all-edges FAIL precount
  (`origin/fix/ooc-b-memory-streaming-join:_stream_driver.py:323-327`) into a
  single per-edge loop. The salvage driver ran every FAIL `total_orphans()`
  BEFORE opening any cursor, but `total_orphans()`
  (`_stream_join.py:559`) opens the joiner's DuckDB connection at :571 and does
  NOT close it, so two FAIL edges would hold two connections open at once. The
  per-edge loop keeps exactly one connection live: for each edge k, in order,
  (1) if `edge.orphan_policy is FAIL`, run `total_orphans()` and RAISE if
  non-zero; (2) call `run_ordered_join(batch_rows, run_bytes_cap=...,
  merge_fan_in=...)` (EAGER: drains the unordered join into a fresh
  `BoundedExternalSorter` and CLOSES the joiner's DuckDB connection before
  returning); (3) wrap the returned `_OrderedJoinRows` in
  `JoinRowCursor(rows, join_columns=edge.child_columns)` and register it with the
  `ExitStack` IMMEDIATELY; (4) only then advance to edge k+1. Every FAIL precount
  still completes before phase 3 emits any output (preserving "FAIL before
  output"), because phase 3 runs after the whole phase-2 loop. Phase 3's body
  (`cursor.take` -> `resolve_batch` -> `_replace_fk_columns`, then
  `cursor.assert_exhausted()`) is UNCHANGED: `_OrderedJoinRows` is itself an
  `Iterator[pa.RecordBatch]`, so it drops into `JoinRowCursor` exactly like
  `iter_join_rows` did, already row_nr-ordered with its own contiguity guard.
- **Port the `_emit.py` / `_stage.py` salvage deltas (NOT "zero to port").** The
  current `emit_to_sink` (`_emit.py:39`) and `MaskedKeyStager.__init__`
  (`_stage.py:42`) LACK the salvage driver's `masked_observed_types` parameter
  and its seeded-observation behavior, and `_emit.py`'s annotations still name
  `ChildFkBatchJoiner` / `pa.Table | LazySource` instead of `StreamFkJoiner` /
  `ParentSource`. A literal salvage-body port would raise `TypeError` at the sink
  call, or, if the argument were dropped, lose pre-reconciliation type
  observations and produce incorrect outgoing parent-relation types for all-null
  / degenerate masked columns. Port ONLY the salvage deltas to `_emit.py` and
  `_stage.py`: `masked_observed_types` plumbing, the `MaskedKeyStager` seeded
  observations, the `ParentSource`/`StreamFkJoiner` typing, and `raw_parent_source`
  forwarding. Do NOT replace the current `_relation.py` or `_memory_estimate.py`
  (they carry newer fixes). A sink-chain regression with a degenerate (all-null)
  masked outgoing parent key guards the type-observation behavior.
- **Owning-lifecycle change.** Each `_OrderedJoinRows` must be closed. The N
  per-edge ordered iterators stay open across the whole of phase 3 (phase 3 takes
  from all cursors per payload batch), so hold them in a single `ExitStack` (or
  equivalent) that closes all N in a `finally`. Full drain self-closes each
  (`__next__` on `StopIteration`), but the abandonment/error path needs the
  explicit close. Mirror the A.3 owning-iterator discipline.
- **Budget params.** `stream_table` takes plain `run_bytes_cap: int,
  merge_fan_in: int = 16`, threaded straight into each `run_ordered_join` call.
  Do NOT thread `resolve_reorder_budgets` / `ReorderBudgets` here: computing the
  budget from the process ceiling + disk ledger and choosing reorder-vs-batchjoin
  is the Task-7 route-seam job. Keeping the driver on explicit ints matches the
  A.3 harness and keeps this slice decoupled.
- **Multi-edge = Option A (per-edge sorter).** For a table with multiple incoming
  FK edges, each edge gets its OWN `run_ordered_join` + its own sorter, and the
  per-row cross-edge merge happens in phase 3 exactly as the salvage driver
  already composes multiple edges (each edge an independent cursor, aligned
  positionally). This is byte-safe because `__decoy_row_nr` is unique per child
  row, so every edge's ordered output is a contiguous 0..N-1 run in the SAME
  source row order as the payload store, aligning positionally regardless of
  whether order came from `ORDER BY` or the bounded sorter. Because
  `run_ordered_join` closes its connection before returning, driving edges in a
  phase-2 loop keeps only one DuckDB join connection live at a time.

OUT (deferred, explicit non-goals):
- No route seam (`_pipeline_route_exec` / `decide_execution_route`); the live
  route stays on `_runner.py`/`_batch_join`. That is Task 7.
- No `ReorderBudgets`/`resolve_reorder_budgets` threading, no `require_disk`
  enforcement (Task 7).
- No change to `run_ordered_join`, `_batch_join`, `_runner`, or the oracle.

## 3. Behavior contract (what "correct" means)

- **Parity.** `stream_table`'s output is identical to the join oracle for every
  fixture shape (single edge, chain, deep chain, fanout multi-child; matched /
  orphan FAIL/WARN/PRESERVE/REMAP / null keys / empty child / cross-batch /
  cross-run boundaries). Two levels, named precisely (the plan-gate flagged that
  `to_pydict()` is only value parity): against the pandas oracle, VALUE parity
  under the suite's documented normalizations (NaN<->null, decimal scale);
  against `_batch_join`, the stronger contract asserts schema + Arrow type +
  metadata, row order, values, nulls, AND warning equality. The only behavioral
  change from a hypothetical `iter_join_rows` driver is the ORDER SOURCE (bounded
  sorter vs DuckDB `ORDER BY`), and `__decoy_row_nr` is a unique total-order key,
  so the restored order is identical.
- **Bounded memory.** Per edge, the reorder mechanism holds only its
  `run_bytes_cap`-bounded sorter working set (proven by the A.3 RSS test); the N
  sorters spill to disk. The driver adds no unbounded resident state.
- **All-or-nothing owning result.** Every `_OrderedJoinRows` is closed on normal
  completion AND on abandonment/exception (ExitStack `finally`), leaking no spill
  file or DuckDB connection.

## 4. Acceptance tests (authored before impl; no later contributor weakens them)

1. **Differential parity vs `_batch_join`** (primary,
   `tests/parity/test_out_of_core_fk_parity.py` fixtures): drive the SAME inputs
   through `stream_table` (reorder) and `run_fk_out_of_core` (`_batch_join`), and
   assert the STRONG contract -- equal schema + Arrow type + metadata, row order,
   values, nulls, and warnings (not merely `to_pydict()` value-equality). Because
   `_batch_join` is pinned byte-parity to pandas, this proves parity transitively.
2. **Anchor to the pandas oracle directly** on the four shapes (single edge,
   chain, deep chain, fanout multi-child): value parity under the suite's
   documented normalizations (NaN<->null, decimal scale).
3. **Orphan policies**: matched, orphan under FAIL / WARN / PRESERVE / REMAP,
   null FK, empty child, duplicate child keys, cross-batch and cross-run
   boundaries -- each parity-identical to the oracle.
4. **Combined decisive case (multi-edge x REMAP x multi-run).** A table with >=2
   incoming FK edges -- BOTH distinct AND overlapping child columns (asserting the
   later edge's overwrite), containing REMAP orphans, driven with a payload/join
   batch-boundary MISMATCH and a `run_bytes_cap` small enough to force MULTIPLE
   sorter runs per edge -- resolves every edge parity-identically to the oracle
   and aligns positionally with the payload. This is the interaction the separate
   policy/boundary/multi-edge tests do not cover.
5. **Lifecycle (three distinct paths).** `stream_table` returns None; the
   abandonment boundary is the SINK. Separate tests, each asserting every opened
   `_OrderedJoinRows` is closed, all final-run spill files are removed, and any
   opened FD is released: (a) a sink that consumes ONE batch and returns normally;
   (b) a sink that RAISES after one batch; (c) edge k+1's `run_ordered_join`
   raising AFTER edge k's `_OrderedJoinRows` has entered the `ExitStack`. The
   `ExitStack` wraps phase 2 AND the entirety of phase 3, registering each result
   immediately.
6. **FAIL-precount lifecycle**: a table with TWO FAIL edges asserts the maximum
   number of live DuckDB connections at any instant is exactly one (the folded
   per-edge precount, not the salvage all-edges-first precount).
7. **Sink-chain type observation**: a degenerate (all-null) masked outgoing
   parent key produces the correct outgoing parent-relation Arrow type via the
   ported `masked_observed_types` plumbing.
8. **Route evidence**: the driver provably ran `run_ordered_join` (not
   `iter_join_rows` / `_batch_join`) -- assert the reorder path executed.

VERIFY bar: parity suite + the four-shape oracle anchor + a bounded-RSS check
reusing the A.3 perf discipline (the driver adds no unbounded state) + mutation
on the CHANGED units (the phase-2 swap, the ExitStack lifecycle, the multi-edge
composition); ruff/format/mypy(3.12) clean; module <600 LOC.

## 5. Failure modes

| Condition | Behavior | Test |
|---|---|---|
| a per-edge ordered join raises mid-build | ExitStack closes all opened iterators; exception propagates; no partial output published | #5 |
| result abandoned without full drain | every `_OrderedJoinRows` closed in finally (spill + conn released) | #5 |
| contiguity guard trips (row_nr not 0..N-1) | `run_ordered_join`'s fail-closed guard raises (existing A.3 code) | inherited |
| multi-edge positional misalignment | caught by the multi-edge oracle parity test | #4 |

## 6. Tasks

- [ ] A. New `_stream_driver.py::stream_table` = salvage three-phase structure
  with the phase-2 swap to `run_ordered_join`, the FAIL precount folded into the
  per-edge loop, the `ExitStack` owning-lifecycle around phase 2 + phase 3, and
  plain budget params. Port the `_emit.py` / `_stage.py` salvage deltas
  (`masked_observed_types` plumbing, `MaskedKeyStager` seeded observations,
  `ParentSource`/`StreamFkJoiner` typing, `raw_parent_source` forwarding); leave
  `_relation.py` / `_memory_estimate.py` (newer fixes) untouched.
- [ ] B. Tests #1-#8.
- [ ] C. VERIFY (parity + oracle anchor + RSS + mutation on changed units) ->
  dennis REVIEW -> Codex FINAL gate. HELD, push, no merge.

## 7. Risks / open questions for the plan-gate

- Confirm Option A (per-edge sorter + positional phase-3 merge) is byte-identical
  to the oracle for multi-edge tables: does the payload-store row order match
  every edge's restored 0..N-1 order under all orphan policies, including REMAP
  (where an orphan child masks through the parent strategy)?
- Confirm the ExitStack holds all N `_OrderedJoinRows` open across phase 3 without
  co-residency issues (each edge's join connection is already closed by
  `run_ordered_join` before the next edge's opens; only the sorters' merge
  readers are concurrently open in phase 3, each bounded by `run_bytes_cap`).
- Confirm the salvage phase-1 / phase-3 bodies reused here need no adaptation
  beyond the phase-2 swap (the FRAME found the dependency set fully present; flag
  any symbol that moved between the salvage branch and `feat/native-phase3`).
- Confirm deferring the route seam (Task 7) leaves a coherently testable
  standalone driver (the A.3 consumer landed standalone the same way).

# P4-A.3 (minimal slice): order-restore reland — the sorter's first consumer

Status: plan APPROVED for build (authored 2026-09-02, Opus; Codex PLAN-gate:
round 1 NO-GO 7 findings → round 2 NO-GO 1 lifecycle MEDIUM → round 3 GO). Build
next (Sonnet, task-by-task against §7); then VERIFY + dennis + Codex final gate.
Held target branch `feat/native-phase3`; Phase 4 merges once at the end; no merge.

> Part 2 Phase 4, slice P4-A.3, **minimal sorter-consumer scope** (Cam decision
> 2026-09-02). The bounded external sorter (P4-A.2) is DONE + held with no
> consumer; this slice gives it its first real one: the OOC FK bounded-reorder
> `run_ordered_join`, proven byte-identical to the executable pandas oracle and
> memory-bounded. It relands ONLY what that consumer needs and defers the rest of
> the OOC-B Milestone-2 route (Tasks 4/5/6/7/8) to follow-on slices.
>
> Prior art: the 8-task Milestone-2 plan
> `docs/plans/2026-07-22-ooc-b-external-reorder-implementation.md` @
> `feat/ooc-b-external-reorder` (Tasks 2 + 3 are this slice's core), and the
> single-source-read streaming-join scaffolding preserved on
> `origin/fix/ooc-b-memory-streaming-join` (`_stream_join.py`, `_payload_store.py`).
> M1 primitives (`_external_sort.py`, `_reorder_budget.py`) are already relanded
> on `feat/native-phase3` — this slice CONSUMES them, does not rebuild them.

## 1. Why this slice exists (the invariant it changes)

The GCP 200M@8GB run proved a DuckDB *global* `ORDER BY __decoy_row_nr` external
sort cannot reach never-OOM (peak RSS ~21 GB; `memory_limit` does not bound
DuckDB's global sort). The bounded-reorder architecture (Codex architecture
consult, 2026-07-22, fixed — do not redesign) replaces that global ORDER BY with:
per FK edge, ONE unordered forced-parent-build hash join (no ORDER BY), then
Decoy restores `__decoy_row_nr` order with the M1 `BoundedExternalSorter`, and
the FK consumer wraps the ordered stream in a fail-closed 0..N-1 contiguity guard
before the existing `JoinRowCursor` → `resolve_batch` → sink.

`feat/native-phase3`'s current production OOC route is the resident-parent
`_batch_join.py` path (batch-local `ORDER BY`, no global reshuffle, no
order-restore step). This slice does NOT touch that path; it lands the
bounded-reorder consumer alongside it and proves it in isolation against the
executable oracle. Wiring it into the live route dispatch is Task 7 (deferred).

## 2. Scope

IN (this slice):
- Reland the streaming-join scaffolding needed by the consumer: `_stream_join.py`
  (`StreamFkJoiner`, `iter_join_rows`, `resolve_batch`, `JoinRowCursor`,
  `stage_keys`, `total_orphans`) and `_payload_store.py` (`SpillPayloadStore`,
  `RawParentKeySpill`, `SpillChildKeys`, `open_reader`) from
  `origin/fix/ooc-b-memory-streaming-join`, adapted to current
  `feat/native-phase3` APIs. NOTE (build finding, Task A done at `01725f19`): the
  scaffolding never called the M1 sorter -- `iter_join_rows` runs its own
  `ORDER BY` shim, and `ExternalRowNrSorter` only ever lived on the separate,
  never-merged `feat/ooc-b-external-reorder` branch (sorter, no consumer). There
  is therefore NO sorter call site to adapt here; the sorter is wired NEW in
  Task 3's `run_ordered_join` (constructed as
  `BoundedExternalSorter(..., sort_key_column="__decoy_row_nr")`). The reland was
  mechanical (one import-path change: `_resolve_output_types`/`_cast_chunks` moved
  from the deleted `_fixed_schema_typing.py` into `_batch_join.py`).
- **Task 2** — unordered join: `StreamFkJoiner._iter_unordered_join_rows(batch_rows)`
  (a copy of `iter_join_rows` with the `ORDER BY` dropped), lazy per-edge
  connection open (`_ensure_conn()`), the pinned-optimizer pragmas
  (`build_side_probe_side` + `join_order` disabled, `threads=1`), and
  `explain_join() -> dict` (parsed `EXPLAIN (FORMAT JSON)`), with a fail-closed
  guard that the plan carries no global sort operator. `iter_join_rows` (with its
  ORDER BY) stays live as a shim (its removal is deferred Task 6).
- **Task 3** — `StreamFkJoiner.run_ordered_join(batch_rows, *, run_bytes_cap, merge_fan_in=16)`
  returns an OWNING, closeable iterator (context-manager + idempotent `close()`;
  see §3): drain `_iter_unordered_join_rows` into a `BoundedExternalSorter`
  (constructed with an explicit `sort_key_column="__decoy_row_nr"`) while the
  DuckDB connection is live; **close the connection**; `finish()`; stream
  `iter_ordered()` wrapped in the fail-closed 0..N-1 contiguity guard (new code
  `out_of_core_fk_reorder_contiguity`, in the FK-consumer layer, NOT the sorter),
  against an N taken from the independent child-stage count. Blocking work runs
  eagerly inside the call; the connection is closed by the time the call returns
  (so join state and merge buffers are never co-resident).
- Budget wiring at the consumer boundary: `resolve_reorder_budgets` →
  `ReorderBudgets`; `require_disk(...)` before the blocking drain.
- Single-edge fixtures (`simple_edge_fixture`, `remap_edge_fixture`) and the
  acceptance tests in §5.

OUT (deferred to follow-on slices, explicit non-goals):
- **Task 4** spillable parent-relation split-dedup (stage/winners/join-back). This
  slice may reuse the scaffolding's existing parent-relation dedup, which can hold
  an O(parent) resident structure; the never-OOM claim here is therefore scoped to
  the REORDER (child/join-output) path only (see §4). Bounded-parent is Task 4.
- **Task 5** FAIL anti-join precount under the new lifecycle.
- **Task 6** sequential per-edge driver (`_stream_driver.py` route wiring), and the
  removal of the `iter_join_rows` ordered shim.
- **Task 7** route seam: `_pipeline_route_exec.py` dispatch, `process_ceiling`
  derivation, resident `_batch_join.py` fallback selection. This slice does NOT
  make the live route choose the reorder path; `run_ordered_join` is proven at the
  joiner/consumer level.
- **Task 8** route-level memory sentinel + plateau probes.

## 3. Behavior contract (what "correct" means)

- **Byte-parity.** For a single FK edge, `run_ordered_join`'s resolved output
  (values AND row order AND nulls AND warnings) is IDENTICAL to the executable
  oracle `_join.py::mask_child_fk` for that edge, and to the pinned pandas oracle
  wherever a full-route comparison is available. Row order is part of the
  comparison. `mask_child_fk` is the reference: this slice does NOT modify it.
- **Contiguity, against an INDEPENDENT N (Codex plan-gate HIGH 1).** The expected
  child row count `N` is taken from an INDEPENDENT source known before the join,
  NOT inferred from the joined output: the child stage's own row count (the
  `SpillChildKeys` / staged-key count, which equals the child's `__decoy_row_nr`
  domain size). The guard asserts the ordered stream is exactly that domain:
  exactly `N` rows, first row_nr 0, last `N-1`, every adjacent pair differs by 1,
  no duplicate or missing row_nr. Because `N` is independent, a lost suffix (fewer
  than `N` rows, or a last row_nr < N-1) fails closed instead of self-validating
  as a shorter dense range. Violations raise `out_of_core_fk_reorder_contiguity`,
  never silent.
- **All-or-nothing result (Codex plan-gate HIGH 2).** `run_ordered_join`'s
  iterator is an all-or-nothing contract: a raised `out_of_core_fk_reorder_contiguity`
  invalidates the WHOLE result. The guard cannot promise "no valid prefix was ever
  yielded" for a streaming iterator (a mid-stream gap is detected only when the
  post-gap batch arrives, before that batch is yielded, but earlier batches have
  already been handed to the caller). The contract is therefore explicit: the
  consumer MUST NOT commit any batch to durable output until the iterator
  completes without raising; the transactional sink commit that enforces this at
  the route level is Task 7 (deferred). Acceptance test #4 asserts the RAISE (a
  consumer draining to a list sees an exception and no complete result), not the
  absence of intermediate batches.
- **Unordered, forced-parent-build join (Codex plan-gate MEDIUM 4).** The join
  executed for the reorder path carries NO global sort operator AND builds its
  hash table on the (deduplicated) PARENT side with the child as the streamed
  probe. Both are verified structurally via the JSON plan (operator types + build
  side). A future optimizer change that re-injects a global ORDER BY, or flips the
  build to the child side (which would make the hash table O(child) resident and
  break the memory model), is a fail-closed gate failure, not a silent regression.
- **Single-source-read + REMAP semantics** from the scaffolding are preserved
  (`resolve_batch`, payload-batch-aligned resolution, `__decoy_parent_match` /
  `__decoy_parent_masked_i`), unchanged by this slice.
- **Connection + spill lifetime (Codex plan-gate MEDIUM 6 + round-2 MEDIUM).**
  Two distinct mechanisms, because a bare generator's `try/finally` does NOT run
  reliably when the caller abandons the returned iterator before its first
  `next()` or without closing it (cleanup would wait for nondeterministic GC):
  1. **Eager blocking phase.** The unordered-join drain → `sorter.finish()` runs
     EAGERLY inside `run_ordered_join`, wrapped in `try/finally`: the DuckDB
     connection is opened, drained, and CLOSED inside this block, and any failure
     (drain error, sorter failure, disk exhaustion after preflight, malformed
     EXPLAIN) closes the connection AND the sorter (unlinking spill) before
     propagating. By the time the function returns, the connection is already
     closed; the only resource the returned object still owns is the sorter's
     final ordered run on disk.
  2. **Owning, closeable result.** `run_ordered_join` returns NOT a bare generator
     but an OWNING iterator object (implements `__iter__`/`__next__`, `close()`,
     and the context-manager protocol) that holds the sorter. `close()` calls
     `sorter.close()` (idempotent, unlinks the spill registry) and is safe to call
     before the first `next()`, after partial consumption, or twice. The CONSUMER
     CONTRACT is to use it as a context manager (`with joiner.run_ordered_join(...) as rows:`)
     or to call `close()`; that is the reliable cleanup path for abandonment. The
     iterator also self-closes on normal exhaustion. No exit path leaks a
     connection, file handle, or spill file.

## 4. Memory contract (what this slice proves, and what it defers)

The REORDER step adds no resident structure that scales with row count: the
`BoundedExternalSorter` holds `run_bytes_cap`-bounded buffers + merge-fan-in
heads (proven by the M1 subprocess RSS test), and the DuckDB join is unordered +
spillable + closed before the merge. The slice's RSS proof (§5) demonstrates the
join→reorder path stays within a documented envelope on a fixture with a LARGE
child / join output and a SMALL parent, so the measured bound isolates the
reorder path this slice delivers.

**Exact RSS envelope (Codex plan-gate MEDIUM 5).** The proof drives the M1 budget
model: a named `process_ceiling_bytes` (the subprocess ceiling, e.g. 512 MiB as
in the M1 sorter RSS test) is split by `resolve_reorder_budgets` into
`duckdb_memory_limit_bytes` (F_DUCKDB = 0.55) and `run_bytes_cap` (F_SORT = 0.15),
leaving a >= 0.30 reserve. The two co-resident phases are: (1) DRAIN, where the
DuckDB unordered-join buffers (bounded by `duckdb_memory_limit_bytes`) and the
sorter write buffer (bounded by `run_bytes_cap`) are BOTH live, plus staging /
payload state and baseline interpreter RSS; and (2) MERGE, after the connection
is closed, where only the sorter's bounded merge state is live. The asserted bound
is `peak VmHWM <= ENVELOPE_FACTOR (1.35) * process_ceiling_bytes`, the same factor
and allocator pinning (`ARROW_DEFAULT_MEMORY_POOL=system`, `MALLOC_ARENA_MAX=2`)
the M1 sorter RSS test established; the `process_ceiling_bytes` is the single
named ceiling, and the reserve is what absorbs staging/payload/baseline. The proof
also asserts real spill occurred (multiple sorter runs) so it is not an
accidental in-buffer pass. Because the connection is closed before MERGE, DuckDB
join state and the sorter merge buffers are never co-resident.

The PARENT-relation resident cost (bounded only after Task 4's split-dedup) is
explicitly OUT of this slice's never-OOM claim. This slice claims: "the
order-restore reorder step is byte-identical and adds no unbounded resident
structure"; it does NOT yet claim the whole FK edge is never-OOM at arbitrary
parent count. That is Task 4 + Task 6.

## 5. Acceptance tests (authored before impl; no later contributor weakens them)

Landing tests (each must exist and pass; a red/xfail placeholder is not allowed):

1. **`test_run_ordered_join_byte_parity_simple`** — a simple FK edge (no remap):
   `run_ordered_join` resolved output == `mask_child_fk` oracle, compared via the
   parity harness fold (`tests/parity/test_out_of_core_fk_parity.py` helpers,
   value+order+null-exact). Uses `simple_edge_fixture`.
2. **`test_run_ordered_join_byte_parity_remap`** — a REMAP edge (parent key
   masked): same equality, exercising `resolve_batch` REMAP semantics through the
   reorder path. Uses `remap_edge_fixture`.
3. **`test_reorder_shuffled_input_restores_row_order`** — feed the unordered join
   a deliberately reshuffled batch stream (small run_bytes_cap forcing a real
   multi-run merge) and assert the resolved output row_nr sequence is exactly
   0..N-1 and values match the oracle. Proves the sorter is actually driving the
   order, not an incidental scan order.
3b. **Parity across the failure/edge surface (Codex plan-gate MEDIUM 3), each
   value+order+null+warning exact vs `mask_child_fk`:**
   - `test_parity_orphan_child_rows` — an edge with unmatched / null child FKs
     (orphans under the edge's orphan policy); the reorder path reproduces the
     oracle's orphan handling and any emitted `QualityWarning` byte-for-byte.
   - `test_parity_empty_child` — a zero-row child edge: the reorder path yields an
     empty, correctly-typed result matching the oracle (no crash, no spurious
     row), and the contiguity guard treats N=0 correctly.
   - `test_parity_across_batch_and_run_boundaries` — an edge sized so the join
     spans multiple `batch_rows` batches AND the sorter spans multiple runs, so a
     boundary bug in staging / resolution / merge surfaces; parity holds.
4. **`test_contiguity_guard_fails_closed`** — with N taken from the independent
   child-stage count, drive a stream that is short a suffix / has a duplicated /
   non-dense / wrong-count row_nr (via a crafted fixture or a test seam that drops
   a row post-join) and assert `run_ordered_join` raises
   `out_of_core_fk_reorder_contiguity`; a consumer draining the iterator to a list
   observes the exception and NO complete result (the all-or-nothing contract of
   §3). Includes the lost-suffix case that a join-output-inferred N would miss.
5. **`test_unordered_join_plan_pinned`** — `explain_join()` JSON plan asserts
   operator TYPES: a hash join with the pinned optimizers disabled, NO global
   ORDER BY / sort operator, AND the build side is the (deduplicated) PARENT with
   the child as probe (Codex plan-gate MEDIUM 4). Asserts on structure, not the
   view alias. A plan that regains a global sort or flips the build to the child
   reddens this test.
6. **`test_reorder_unbudgeted_fails_closed`** and
   **`test_reorder_insufficient_disk_fails_closed`** — a missing memory/disk
   budget raises `out_of_core_reorder_unbudgeted`; a disk ledger that cannot cover
   the edge raises via `require_disk`, before any blocking work.
7. **`test_join_rows_clear_sorter_key_contract`** — assert the join-row batch
   schema satisfies the sorter's contract: `__decoy_row_nr` present, int64,
   non-null, and `_min_row_bytes(schema) >= INDEX_BYTES` (the join-row batch is
   comfortably ≥ 8 bytes/row). Guards against a future schema change silently
   tripping the sorter's fail-closed guards. `run_ordered_join` constructs the
   sorter with an EXPLICIT `sort_key_column="__decoy_row_nr"` (Codex plan-gate
   LOW 7), not the default, so a future default change cannot silently mis-key.
8. **`tests/perf/test_out_of_core_reorder_memory.py`** (perf-marked, subprocess
   RSS) — a large-child / small-parent edge streamed through `run_ordered_join`,
   budgets from `resolve_reorder_budgets(process_ceiling_bytes=<named ceiling>)`;
   assert peak VmHWM ≤ `ENVELOPE_FACTOR (1.35) * process_ceiling_bytes` with the
   allocator pinned, and assert real spill occurred (multiple sorter runs), per
   the exact envelope in §4. The never-OOM proof for the reorder path.
9. **`test_stream_join_scaffolding_smoke`** — a smoke test over the relanded
   `_stream_join.py` / `_payload_store.py` on a single edge (stage keys → join →
   resolve) so the reland cannot silently rot before its consumers land.
10. **Resource-lifecycle / cleanup (Codex plan-gate MEDIUM 6 + round-2 MEDIUM):**
   - `test_drain_failure_closes_connection_and_spill` — an injected exception mid
     unordered-join drain (and, separately, an injected sorter failure) leaves the
     DuckDB connection closed and zero sorter spill files remaining in the reorder
     dir (the eager-phase `try/finally`).
   - `test_result_closed_before_first_next_cleans_up` — construct the owning
     result via `run_ordered_join`, then `close()` it (or exit its `with` block)
     WITHOUT calling `next()`; assert zero spill files remain and the connection
     is closed. This is the abandonment-before-first-iteration case a bare
     generator `try/finally` would not cover.
   - `test_result_closed_after_partial_consumption_cleans_up` — consume a few
     batches, then `close()` / exit the `with`; assert zero spill files remain. A
     second `close()` is a no-op (idempotent).
   - `test_malformed_explain_fails_closed` — a malformed / unparseable EXPLAIN
     result fails closed rather than proceeding on an unverified plan, and cleans
     up.

Verification bar (VERIFY phase, before REVIEW): full parity suite green; coverage
+ mutation graded on the CHANGED units (`_stream_join.py` new methods, the
contiguity guard, the budget wiring), 0 unresolved correctness-critical logic;
ruff/format/mypy(3.12) clean; the perf RSS test green.

## 6. Failure modes (each fails closed, none silent)

| Condition | Code / behavior | Test |
|---|---|---|
| Missing memory or disk budget | `out_of_core_reorder_unbudgeted` (raise before blocking) | #6 |
| Disk cannot cover the edge ledger | `require_disk` raise | #6 |
| Ordered stream not exactly 0..N-1 (N from independent child-stage count) | `out_of_core_fk_reorder_contiguity` (all-or-nothing) | #4 |
| Join plan regains a global sort, or flips build to the child side | JSON-plan guard raise (fail-closed) | #5 |
| Sorter key contract violated | sorter's own `out_of_core_sort_*` codes | #7 |
| Malformed / unparseable EXPLAIN | fail closed, do not proceed on an unverified plan | #10 |
| Exception mid-drain / sorter failure / disk exhaustion after preflight / iterator abandonment | close connection + unlink spill (try/finally + sorter close registry); no leak | #10 |
| Oracle raises for an input | route must also fail closed (parity-or-faithful-rejection rule) | #1/#2 harness |

## 7. Tasks (build order; each gate-able)

- [x] **A. Reland scaffolding** (`_stream_join.py`, `_payload_store.py` + single-edge
  fixtures) adapted to native-phase3 APIs. DONE at `01725f19` (mechanical port;
  no sorter call site existed, see §2 note). Test #9 (smoke) + full unit suite
  green (2 pre-existing unrelated failures noted, untouched).
- [ ] **B. Task 2** unordered join + pragmas + `explain_join()` + fail-closed
  plan guard. Land test #5.
- [ ] **C. Task 3** `run_ordered_join` + contiguity guard + budget wiring
  (`resolve_reorder_budgets` / `require_disk`). Land tests #1–#4, #6, #7.
- [ ] **D. Perf** RSS reorder proof (test #8).
- [ ] VERIFY (coverage+mutation on changed units, lint/type), dennis REVIEW,
  Codex FINAL gate. HELD — no merge.

## 8. Non-goals / risks

- Non-goal: wiring the live route to CHOOSE the reorder path (Task 7). The slice
  proves the consumer against the executable oracle; route dispatch is deferred.
- Non-goal: bounded-parent memory (Task 4). The RSS proof is scoped to the
  reorder path (large child, small parent) and says so.
- Risk: the scaffolding on `origin/fix/ooc-b-memory-streaming-join` may drift from
  current native-phase3 APIs (`_join.py`/`_relation.py`/`_duckdb.py` signatures).
  The reland (Task A) adapts to the CURRENT signatures; the smoke test #9 is the
  early tripwire. If adaptation proves larger than a mechanical port, pause and
  bring the delta to Cam rather than silently reshaping the scaffolding.
- Risk: the parity harness (`test_out_of_core_fk_parity.py`) compares the full
  route; this slice tests the consumer directly against `mask_child_fk`. If a
  single-edge harness seam does not already exist, add a thin one that reuses the
  existing fold helpers (do not weaken the comparison).

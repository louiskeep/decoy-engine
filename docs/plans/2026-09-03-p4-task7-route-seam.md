# P4-A Task 7: reorder-route live seam (auto-select by parent-key size)

Status: plan (DRAFT v3 — remediated after Codex plan-gate rounds 1 & 2 NO-GO).
Author: Opus, 2026-09-03. Held target branch `feat/native-phase3`; merges with
the Phase-4 bundle. Route-selection behavior change (R2).

Cam decision (2026-09-03): fix the reorder job's safety gaps now, then wire the
route live so large jobs use it — auto-selected by parent-key size, small tables
stay on `_batch_join`.

## 0. Guiding correction from two gate rounds

Both NO-GOs came from the same mistake: re-deriving resource models that already
exist and getting them subtly wrong. v3's rule is **wire the existing, validated
primitives; do not invent new bounds.** The primitives:

- `resolve_reorder_budgets(process_ceiling_bytes, remaining_disk_bytes, *, merge_fan_in)`
  → `ReorderBudgets(duckdb_memory_limit_bytes, run_bytes_cap, remaining_disk_bytes, merge_fan_in)`.
  Its `F_DUCKDB=0.55` / `F_SORT=0.15` / `≥0.30` reserve are the validated model;
  the reserve already covers the sorter's `SORT_OVERHEAD_FACTOR=2.2` flush
  transient and DuckDB overshoot.
- The measured memory contract is `tests/perf/test_out_of_core_reorder_memory.py`:
  peak RSS ≤ `_ENVELOPE_FACTOR (1.35) × process_ceiling`. That is the standard;
  Task 7 inherits it and adds a multi-edge RSS proof to the SAME envelope. No
  `0.70` hard-bound claim (rounds 1-2 error).
- `require_disk(budgets, *, mandatory_staging_bytes, estimated_output_bytes)` is
  the per-edge disk ledger: `staging + 3×output` (duckdb-temp + sorter-runs +
  merge-amplification). It exists but has zero production callers. Task 7 calls it.
- `memory_limit_for(budget_bytes, live_instances)` is the canonical
  bytes→DuckDB-`"NNMB"`-string helper (base-10, safe direction). Use it (there is
  no `_duckdb_limit_str`).
- Routing controls are runtime `run_pipeline` kwargs (`out_of_core_threshold_rows`,
  `out_of_core_budget_bytes`, …), explicitly never frozen `config` fields
  (`_pipeline.py:210`). The threshold override follows that pattern.

## 1. Why this slice

Task 6 built the standalone reorder driver (`_stream_driver.py::stream_table`):
~flat wall time as parent-key count grows where `_batch_join` scales
super-linearly (~4.5x slower at 10M parent keys). Nothing selects it
(`run_fk_out_of_core` calls only `_stream_table`, `_runner.py:202`), and its
LIVE resource budgeting was never built — Task 6 sized the join via the
`_batch_join` phase model and proved memory by driving `StreamFkJoiner`
directly. Task 7 wires the driver's real memory + disk budgeting and makes the
router auto-pick it above the crossover.

## 2. Verified memory + disk model

Connections open lazily; phase 1 is Arrow-IPC only (`begin_staging`/`stage_batch`
→ `SpillChildKeys`, no DuckDB). `run_ordered_join` runs the join with DuckDB live
while draining into the sorter (they ARE co-resident during the drain), then
closes the connection before `sorter.finish()`'s merge. Completed
`_OrderedJoinRows` are lazy (reader opens at first phase-3 iteration); each open
head ≤ `run_bytes_cap // (2·merge_fan_in)`.

| Phase | Peak residency | Bound source |
|---|---|---|
| 1 stage+mask (sink path) | O(batch) Arrow + payload spill to disk | inherited |
| 2 per edge (one at a time) | DuckDB join (`F_DUCKDB`) + active sorter fill (`F_SORT`, ×2.2 flush transient) co-resident during drain | existing `resolve_reorder_budgets` + RSS proof @1.35× |
| 3 resolve | N lazy heads @ `run_bytes_cap/(2·fan_in)` + O(batch) | 4.2 admission |
| build (joiners released) | one dedup conn @ undivided sink build cap | existing preflight |

**Bounded residency requires the sink + `LazySource` shape.** Without a sink,
`ResidentPayloadStore` retains every masked batch and the driver materializes
`list(rewritten())` (`_stream_driver.py:395`) — O(output), by design. So
auto-selection is **sink-path only** (§4.4); resident reorder stays reachable for
parity tests but is never auto-selected.

## 3. Scope

IN:
- `_stream_driver.py`: on the reorder path, size the joiner connection from
  `ReorderBudgets` (not `resolve_phase_memory_limits`); keep the outgoing-build
  cap = existing undivided sink build cap; add per-edge `require_disk` admission
  with cross-edge accumulation; add a DuckDB `max_temp_directory_size` runtime cap.
- `_reorder_budget.py`: add the phase-3 multi-edge head admission + input
  validation (no change to `F_*` fractions or `run_bytes_cap`).
- `_duckdb.py`: accept an optional `max_temp_directory_size` (runtime disk cap).
- new leaf `_route_policy.py` + `_runner.py`: sink-path route selection; extract
  `_table_order`/`_edge_indexes` into `_route_policy.py` to keep `_runner.py ≤ 676`.
- `_pipeline.py` + `_pipeline_route_exec.py`: thread the
  `out_of_core_reorder_threshold_rows` runtime kwarg.
- Tests + a calibration benchmark; docstring corrections (§6.3).

OUT: `_batch_join` internals; full-frame/chunked/sequential routes; the reorder
driver's phase structure and byte-parity logic; the validated `F_*` fractions and
the capacity preflight (untouched — build cap unchanged); resident-path
auto-selection; live per-host threshold calibration (deferred).

## 4. Design

### 4.1 Memory wiring (P0-1 / P0-2)

On the reorder (sink) path, size two connections directly and unambiguously
(never through `resolve_phase_memory_limits`):
- joiner: `memory_limit_for(budgets.duckdb_memory_limit_bytes, 1)` — one join
  connection live at a time, at `F_DUCKDB`·ceiling.
- outgoing build: `memory_limit_for(budget_bytes, 1)` — the existing undivided
  sink build cap the capacity preflight already assumes (so the preflight stays
  valid; build sizing is NOT changed).

`stream_table` gains an explicit `reorder_caps: ReorderCaps | None` param
(join+build memory-limit strings + `run_bytes_cap` + `merge_fan_in`). When
present (reorder path), it uses those and takes NO `resolve_phase_memory_limits`
branch; when `None` (its existing standalone/test callers), behavior is
unchanged. This removes the "don't call the legacy resolver / pass
`budget_bytes=None`" contradiction: one path, one cap source. Memory correctness
is inherited from the validated model and proven by the multi-edge RSS test
(§5 T9) to the same 1.35× envelope.

### 4.2 Multi-edge phase-3 head admission (P1-1/P1-2)

`run_bytes_cap` stays `F_SORT`·ceiling (no divide-by-N; avoids the `MIN_RUN_BYTES`
false-reject). The only unbounded case is N co-resident phase-3 heads at
`run_bytes_cap/(2·merge_fan_in)`. That fits while `N ≤ 2·merge_fan_in` (32 at
default). A table with more incoming FK edges is plausible (wide event / ERP /
healthcare schemas). So the router does NOT fail — it **falls back to
`_batch_join`** for such a table (which has no fan-in limit), via the selection
predicate (§4.4). `resolve_reorder_budgets` still exposes the head-fit check
(returns the per-head cap and the `2·fan_in` ceiling) so the policy can consult
it; it raises only on genuinely invalid inputs (§P3), never on plausible fan-in.
`co_resident_readers` = all incoming edges.

### 4.3 Disk admission + runtime cap (P0-3)

Call the existing `require_disk` per edge inside `stream_table`, at **phase-2
entry** (phase 1 has written all `SpillChildKeys` + `payload`, no join open yet),
with a correct upper bound and cross-edge accumulation:
- `estimated_output_bytes(k)` priced from edge k's **child/staged row count**
  (`joiner._staged_rows`, one ordered row per staged child row — the sorter's
  real output cardinality), × the masked join-row width from `_spill_estimate`'s
  masked-width logic (`max(source_width, strategy_output_width)`,
  `UNKNOWN_WIDTH_CEILING` fallback). NOT parent cardinality (rounds-2 error:
  small-parent/huge-child underestimates).
- cross-edge accumulation: prior edges' final sorted runs stay on disk until
  phase 3, so before edge k the effective remaining disk is
  `budgets.remaining_disk_bytes − Σ_{j<k} estimated_output_bytes(j)`. Thread this
  as a running decrement into each `require_disk` call (its `3×` already covers
  edge k's own duckdb-temp + runs + merge; the decrement adds the retained-prior
  term).
- quota denomination: `remaining_disk_bytes` is computed ONCE at budget
  resolution as `temp_disk_budget_bytes − current_root_usage` (via `_budget`'s
  `temp_disk_bytes`); the per-edge check compares future demand against that
  single remaining figure and its running decrement — no double-charge of
  already-written staging (rounds-2 error).
- runtime cap (hard, not just admission): pass
  `max_temp_directory_size = memory_limit_for-style byte string of the edge's
  duckdb-temp allowance` into `connect_duckdb` for the reorder join, so DuckDB
  aborts rather than overrunning if the estimate is beaten. The
  `BoundedExternalSorter` write path already fails closed on its own run cap.
- The table-boundary `check_temp_disk_budget` stays as the cross-table
  accumulation enforcer, unchanged.

Residual: this is an admission estimate + runtime cap, not a hard reservation;
concurrent external disk consumers remain a documented residual race.

### 4.4 Route selection (P1-1/P1-2/P1-3)

New leaf `_route_policy.py` (imports: relation metadata, `_reorder_budget`,
`_budget`; NOT `_runner`/`_stream_driver` → no cycle). Move `_table_order` and
`_edge_indexes` here from `_runner.py` (they are topology/routing helpers), which
frees enough LOC for the selection dispatch to keep `_runner.py ≤ 676` (the
sentry stays green; asserted in §5).

    decide_route(table, incoming_edges, parent_relations, *,
                 sink, budget_bytes, temp_disk_budget_bytes, root,
                 threshold_rows) -> RouteDecision   # (use_reorder, reorder_caps|None)

Decision key = relation cardinality (deduped, null-filtered, last-write-wins —
the count the join consumes): `ParentKeyRelation.key_count` (new cached property
= `pq.read_metadata(self.path).num_rows`; the relation is one parquet produced
after that dedup, so its footer `num_rows` is the distinct-key count).

    parent_key_count = max((key_count(parent_relations[e]) for e in incoming_edges), default=0)
    use_reorder = (
        sink is not None                       # bounded residency only on the sink path
        and incoming_edges                     # root tables never reorder
        and budget_bytes is not None
        and temp_disk_budget_bytes is not None
        and parent_key_count >= threshold_rows
        and len(incoming_edges) <= 2 * merge_fan_in   # high fan-in falls back (§4.2)
    )

Any false condition → `_stream_table` (current behavior, byte-for-byte). No
budget-less or high-fan-in caller regresses or fails. When `use_reorder`, derive
`reorder_caps` from `resolve_reorder_budgets`; if the budget is genuinely too
small for the sorter floor, `resolve_reorder_budgets` fails closed
(`out_of_core_reorder_budget_too_small`) and the router lets it propagate (sink
aborts, no output) — the one entered-but-unsizeable case, tested.

Override: `out_of_core_reorder_threshold_rows: int | None` runtime kwarg on
`run_pipeline`, threaded (like `out_of_core_threshold_rows`) through
`_pipeline` → `_pipeline_route_exec.run_out_of_core_route` → `run_fk_out_of_core`
→ `decide_route`. Default `REORDER_PARENT_KEY_THRESHOLD = 2_000_000`
(module constant). Validated non-negative; documented on `_pipeline_routing`
alongside the sibling routing kwargs; isolated-run (`_isolated_worker`)
threading covered.

### 4.5 Threshold calibration (P2-1)

Checked-in benchmark (`scripts/native-testing/reorder_crossover_bench.py` +
recorded results) measuring `_batch_join`-vs-reorder wall time across child
sizes, masked widths, sink shape, and fan-in, with a stated pass rule (2M sits
past the crossover with ≥ a named margin and low run-to-run variance). Live
per-host calibration deferred (Q3).

## 5. Acceptance tests

Selection:
- T1 sub-threshold → `_stream_table`; output unchanged.
- T2 at/above threshold + sink + budgets → `stream_table`; output == `_stream_table`
  exactly (§6.1).
- T3 no `budget_bytes`/disk budget → `_stream_table`, no raise.
- T4 root table (no incoming) → never reorder.
- T5 resident path (no sink), above threshold → `_stream_table` (auto-select is
  sink-only).
- T6 high fan-in (`len(incoming) > 2·fan_in`) → `_stream_table`, no raise/failure.
- T7 present-but-too-small budget on a reorder-selected table →
  `out_of_core_reorder_budget_too_small`, sink aborted, no output.
- T8 relation-cardinality key: many duplicate/null raw parent rows but few
  distinct keys → routes by the deduped count.
- T9 override kwarg `out_of_core_reorder_threshold_rows` changes the boundary;
  validated non-negative; threaded through isolated-run.
- T10 mixed multi-table job: each table routed independently; whole-job output
  == all-`_stream_table` (§6.1).

Memory (route-level, through `run_fk_out_of_core`):
- T11 `connect_duckdb` limit spy: joiner == `F_DUCKDB`·ceiling, build ==
  `memory_limit_for(budget,1)`; FAILS on the batch-model sizing, PASSES on the fix.
- T12 subprocess RSS proof (reuse `test_out_of_core_reorder_memory.py` harness):
  a sink+`LazySource` job with N incoming edges, peak RSS ≤ `1.35 × ceiling`;
  representative fan-in, real multi-run spill, route evidence asserted.
- T13 `resolve_reorder_budgets` input validation: `co_resident_readers ≤ 0`,
  `merge_fan_in < 2`, `remaining_disk_bytes < 0` each raise a coded error (no
  silent clamp). Head-fit helper returns pass ≤ 2·fan_in.

Disk:
- T14 `require_disk` admission: no join opens when required > remaining (assert
  `out_of_core_reorder_budget_too_small` before the first `connect_duckdb`);
  child≫parent and parent≫child both priced correctly (child-row output); the
  multi-edge case charges prior edges' retained runs (decrement).
- T15 DuckDB `max_temp_directory_size` runtime cap: a join whose real spill
  exceeds the admission estimate aborts cleanly (not an unbounded overrun).
- T16 table-boundary `check_temp_disk_budget` still fires for a reorder-routed
  job overrunning cross-table disk.

Parity (§6.1):
- T17 forced-reorder (threshold lowered via the real kwarg) == `_batch_join`
  EXACTLY, with a reorder-route witness asserted per case, across: 4 orphan
  policies (PRESERVE, REMAP, WARN, FAIL); sink + `LazySource`; `code_set_corpora`;
  warning order+content; unconfigured-column projection warn AND fail; keyed-mask;
  exact Arrow schema+metadata; composite + overlapping edges; empty/null/NaN
  columns; every payload strategy in `_compat.py`; every admitted parent-key
  strategy. Where `_stream_table` raises, assert `stream_table` raises the same
  type+code+message and leaves the sink in the same state.
- T18 module-size sentry stays green (`_runner.py ≤ 676`).

## 6. Contracts

### 6.1 Parity (precise)

Task 7's invariant is **reorder == `_batch_join`, exactly**: Arrow schema +
metadata, row/chunk order, values + null/NaN representation, warnings (order and
content), `quality_metrics`, and serialized sink bytes on the sink path.
`_batch_join` is the oracle-conformant baseline; its own pandas-oracle parity is
the normalized-value contract already documented for that route — Task 7 does not
re-open it and claims no byte-identity to the pandas oracle beyond what
`_batch_join` gives. Any reorder-vs-batch divergence is a P0 blocker. Route
choice changes timing and memory, never output.

### 6.2 Risk

R2 — changes which route runs above the threshold, on the sink path. Mitigations:
fail-safe selection (non-sink / budget-absent / sub-threshold / high-fan-in / root
all keep current behavior); T17 makes route choice output-invariant with a
per-case witness; memory inherited from the validated model + T11/T12; disk
fail-closed before spill (T14) plus a hard runtime cap (T15). No R3 side effects.
Held on `feat/native-phase3`; merges only with explicit Cam go.

### 6.3 Docstring corrections

`_stream_driver.py` and `_stream_join.py` docstrings currently assert
pandas-oracle byte parity for the reorder path; correct them to the
reorder-equals-`_batch_join` contract (§6.1). No logic change.

## 7. Open questions (gate/Cam, non-blocking)

- Q-A: RESOLVED — build cap unchanged (existing sink build cap), so no preflight
  change.
- Q-B: RESOLVED — count all incoming edges; high fan-in falls back to `_batch_join`.
- Q-C: fixed 2M threshold + checked-in benchmark acceptable for merge, live
  calibration deferred?

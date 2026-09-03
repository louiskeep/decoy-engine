# P4-A Task 7: reorder-route live seam (auto-select by parent-key size)

Status: plan (DRAFT v2 — remediated after Codex plan-gate round 1 NO-GO).
Author: Opus, 2026-09-03. Held target branch `feat/native-phase3`; merges with
the Phase-4 bundle. Route-selection behavior change (R2).

Cam decision (2026-09-03): fix the reorder job's safety gaps now, then wire the
route live so large jobs use it — auto-selected by parent-key size, small tables
stay on `_batch_join`.

## 0. What round 1 got wrong (so v2 does not repeat it)

The Codex plan-gate returned NO-GO with three P0s; all were verified true against
the code. The corrections below are load-bearing:

- **The reorder driver's live memory sizing was never actually built.** Task 6's
  `stream_table` sizes its join connection via `resolve_phase_memory_limits`
  (the `_batch_join` phase model), which — when `budget_bytes` is set — ignores
  the passed `memory_limit` and gives the join connection ≈the full budget
  (`_memory_estimate.py:176`). So DuckDB(≈budget) + sorter(`F_SORT`·budget) are
  co-resident during the drain and the `F_DUCKDB + F_SORT ≤ 0.70` bound is not
  enforced on the live path. Task 6's RSS proof ran `StreamFkJoiner` directly,
  not through this path, so the hole was invisible. Task 7 must build the
  reorder driver's real budgeting (P0-1). Scope therefore includes
  `_stream_driver.py`.
- **The disk guard has zero production callers.** `require_disk` is defined and
  exported but never called (`_reorder_budget.py`); `resolve_reorder_budgets`
  only *stores* `remaining_disk_bytes`. The table-boundary `check_temp_disk_
  budget` cannot substitute: `stream_table` `rmtree`s its whole temp subtree
  before returning (`_stream_driver.py:451`), so the transient spill peak is
  gone before the boundary check runs (P0-2).
- **The multi-edge memory argument was numerically wrong.** Completed
  `_OrderedJoinRows` are lazy (reader opens at first phase-3 iteration), and each
  open head is capped at `run_bytes_cap // (2 · merge_fan_in)`, not the full cap
  (`_external_sort.py:45`, `_stream_join.py:542`). N heads hold ≈`N·cap/(2·fan_in)`,
  so the real pressure point is N ≈ `2·merge_fan_in` (32 at default), not 6.
  Dividing the whole `run_bytes_cap` by N was both wrongly motivated and harmful
  (trips `MIN_RUN_BYTES` at ~10 edges) (P1-1).

## 1. Why this slice

Task 6 built the standalone reorder driver (`_stream_driver.py::stream_table`):
a three-phase, single-source-read FK join with ~flat wall time as parent-key
count grows, where `_batch_join` (`_runner.py::_stream_table`) scales
super-linearly (~4.5x slower at 10M parent keys). Nothing selects it —
`run_fk_out_of_core` calls only `_stream_table` (`_runner.py:202`). Task 7 makes
the router auto-pick the reorder driver above the crossover, and builds the
driver's real memory + disk budgeting so it is safe on a live workload.

## 2. Verified memory + disk model (the design these fixes target)

Connections open lazily; phase 1 is Arrow-IPC only (`begin_staging`/`stage_batch`
write `SpillChildKeys`, no DuckDB). Per-phase peak:

| Phase | DuckDB | Sorter | Other | Bound |
|---|---|---|---|---|
| 1 mask+stage+payload | none | none | O(batch) Arrow + payload append | O(batch) |
| 2 per edge (one at a time) | one join conn, drains then CLOSES before `finish()` | one active sorter fill (`run_bytes_cap`) | — | join and sort never co-resident within an edge; one conn across edges |
| 3 resolve | none | N lazy reader heads @ `run_bytes_cap/(2·fan_in)` each | payload+concat+output O(batch) | `N·run_bytes_cap/(2·fan_in)` + O(batch) |
| build (after joiners released) | one dedup conn | none | O(batch) | one conn |

So the correct caps are: join conn = `F_DUCKDB`·ceiling, sorter fill =
`F_SORT`·ceiling (`resolve_reorder_budgets`), build conn = a build cap sized when
no sorter/join is live. This is what round-1 missed by routing through
`resolve_phase_memory_limits`.

## 3. Scope

IN:
- `_stream_driver.py`: replace the `resolve_phase_memory_limits` sizing on the
  reorder path with reorder-budget-derived caps (join conn `F_DUCKDB`, build
  conn its own cap); add the production `require_disk` admission call before
  phase 2. (This widens the Task-6 "driver frozen" scope — required by P0-1/P0-2.)
- `_reorder_budget.py`: add the multi-edge phase-3 head admission check +
  input validation; keep `run_bytes_cap = F_SORT·ceiling` (no divide-by-N).
- `_runner.py` + new leaf `_route_policy.py`: route selection.
- Tests: memory (route-level RSS through `run_fk_out_of_core`), disk (ledger
  admission), selection behavior + fail-closed, exhaustive reorder≡batch_join
  parity.

OUT: `_batch_join` internals; full-frame/chunked/sequential routes; the reorder
driver's phase structure and byte-parity logic (frozen — only its resource
sizing changes); threshold auto-calibration (fixed default + checked-in
benchmark this slice, live calibration deferred).

## 4. Design

### 4.1 Memory sizing (P0-1)

On the reorder path, `stream_table` must NOT call `resolve_phase_memory_limits`.
Instead the router derives, once per reorder-routed table:

    budgets = resolve_reorder_budgets(
        process_ceiling_bytes=budget_bytes,
        remaining_disk_bytes=remaining,          # 4.3
        merge_fan_in=budgets_fan_in,             # a real constant, e.g. module _REORDER_MERGE_FAN_IN
    )

and passes `stream_table(run_bytes_cap=budgets.run_bytes_cap,
merge_fan_in=budgets.merge_fan_in, join_memory_limit=<F_DUCKDB str>,
build_memory_limit=<build-cap str>, budget_bytes=None, ...)`. `stream_table`
changes:
- Add explicit `join_memory_limit: str` and `build_memory_limit: str` params
  (reorder path passes them; a canonical bytes→DuckDB-string helper
  `_duckdb_limit_str(bytes)` converts `budgets.duckdb_memory_limit_bytes`,
  accounting for DuckDB's base-10 `"NNMB"` parse — reuse the existing conversion
  `_memory_estimate` already documents, do not invent a second).
- Open every joiner at `join_memory_limit`; run the outgoing build (`emit_to_
  sink` / `build_parent_key_relation_aligned`) at `build_memory_limit`.
- The reorder path passes `budget_bytes=None` into any legacy sizing so
  `resolve_phase_memory_limits` can never overwrite the `F_DUCKDB` cap. (Confirm
  no other consumer of `budget_bytes` inside `stream_table` needs the raw value;
  if it does, gate that consumer on route.)
- Build cap: sized when no sorter/join is live (phase 3 done, joiners released
  via `_release_joiners`), so it may use the freed `F_DUCKDB` share; size at
  `F_DUCKDB`·ceiling (same as join) for a single simple bound. Not larger —
  keeps a single documented envelope.

Acceptance: a route-level test spies on the actual `connect_duckdb` `memory_
limit` for sink and resident, N=1 and N>1, asserting `F_DUCKDB`·ceiling for the
join conn and the build cap for the build conn — proving the batch-model cap no
longer leaks in.

### 4.2 Multi-edge phase-3 head admission (P1-1, corrected)

Keep `run_bytes_cap = F_SORT·ceiling` (full fill buffer, one active sorter — no
early `MIN_RUN_BYTES` rejection). The only unbounded case is phase-3 co-resident
heads: `N · run_bytes_cap/(2·merge_fan_in)` must fit the phase-3 sort envelope
(sort share; phase 3 has no DuckDB, so `F_SORT`·ceiling is available and the
reserve is headroom). That holds while `N ≤ 2·merge_fan_in` (32 at default). For
a table whose incoming-edge count exceeds that, fail closed at admission with a
clear error (`out_of_core_reorder_too_many_edges`, naming N, `merge_fan_in`, and
the remedy: raise the budget or the fan-in), rather than silently overflowing.
`co_resident_sorters` = ALL incoming edges (Codex resolved Q-B: every successful
edge stays open regardless of orphan policy).

Add to `resolve_reorder_budgets` a `co_resident_readers: int = 1` param that
performs this admission check and returns the per-head cap for the assertion;
it does NOT shrink `run_bytes_cap`. Validate `co_resident_readers ≥ 1`,
`merge_fan_in ≥ 2`, `remaining_disk_bytes ≥ 0` before sizing (P3), raising a
coded error rather than silently clamping.

Alternative considered and rejected: a separate drain-reader cap decoupled from
`run_bytes_cap` (would need a `BoundedExternalSorter` API change). For single-org
scale a table with >32 incoming FK edges is unrealistic, so a documented
fail-closed bound is the right-sized choice; the decoupled cap is noted as future
work if a real workload ever approaches it.

### 4.3 Disk admission (P0-2)

Add a production `require_disk` call in `stream_table` at **phase-2 entry**
(after phase 1 has written all `SpillChildKeys` + `payload.arrow`, before the
first join opens). It models the true multi-edge peak:

    required ≈ staging_bytes(all N SpillChildKeys + raw_parent + payload)
              + max over k of ( Σ_{j<k} final_run_bytes(j)      # prior edges' runs kept for phase 3
                                + 2 · run_bytes(k) )            # edge k's k-way merge old+new
    available = min(remaining_run_quota, shutil.disk_usage(temp_dir).free)

`final_run_bytes(i)` / `run_bytes(i)` upper-bounded from edge i's relation
cardinality × masked join-output row width (reuse `_spill_estimate`'s masked-width
logic — `max(source_width, strategy_output_width)`, `UNKNOWN_WIDTH_CEILING` for
non-derivable strategies; do not invent a second estimator). Fail closed
(`out_of_core_reorder_insufficient_disk`) before any join if `required >
available`. Document the residual race (concurrent external disk consumers) —
this is an admission estimate, not a hard reservation. `remaining_run_quota` =
`temp_disk_budget_bytes − current_root_usage` (reuse `_budget`'s directory-walk
helper the boundary check already uses). The table-boundary `check_temp_disk_
budget` stays as the cross-table accumulation enforcer, unchanged (driver-agnostic).

### 4.4 Route selection (P1-2)

In `run_fk_out_of_core`'s topo loop, extract selection into a leaf
`_route_policy.py` (mandatory: `_runner.py` is exactly at its 676-LOC allowlist
ceiling, `test_module_size.py:499`, so any net growth fails the ratchet). The
policy module imports only relation metadata + `_reorder_budget` (no `_runner`
import → no cycle), and exposes:

    decide_route(table, incoming_edges, parent_relations, *,
                 budget_bytes, temp_disk_budget_bytes, root) -> RouteDecision
    # RouteDecision = (use_reorder: bool, budgets: ReorderBudgets | None)

Net `_runner.py` LOC: move the selection + budget-derivation + require-disk-arg
assembly into `_route_policy.py`, leaving the loop body calling
`decide_route(...)` then dispatching — net-neutral-to-negative on `_runner.py`,
keeping it ≤ 676 (the acceptance suite asserts the sentry stays green).

Decision key = **relation cardinality** (the deduped, null-filtered, last-write-
wins parent-key count the join actually consumes), not raw source rows:

    key_count(rel) = pq.read_metadata(rel.path).num_rows   # ParentKeyRelation has no __len__
    parent_key_count = max((key_count(parent_relations[e]) for e in incoming_edges), default=0)

Add `ParentKeyRelation.key_count` as a cached property reading the parquet
metadata (single source of truth; avoids re-reading). Predicate:

    use_reorder = (
        incoming_edges                       # root tables never reorder
        and budget_bytes is not None
        and temp_disk_budget_bytes is not None
        and parent_key_count >= REORDER_PARENT_KEY_THRESHOLD
    )

- Budget-absent OR sub-threshold OR root → `_stream_table` (current behavior,
  byte-for-byte; no budget-less caller regresses).
- Budget present but too small for the reorder floor → `resolve_reorder_budgets`
  fails closed (`out_of_core_reorder_budget_too_small`); the router lets it
  propagate (sink aborts, no output published) — this is the ONE case reorder is
  entered and cannot be sized, and it must not silently fall back (a large job on
  the resident path is the OOM this route exists to remove). Tested.

`REORDER_PARENT_KEY_THRESHOLD`: default **2_000_000**, a real overridable config
seam (a field on the OOC route config / plan setting, NOT test monkeypatch),
documented, justified by a checked-in benchmark (4.5). Tests pin behavior
(below/at boundary/override), not the literal.

### 4.5 Threshold calibration (P2-1)

Add a checked-in benchmark (`scripts/native-testing/` or `docs/`) recording the
`_batch_join`-vs-reorder crossover across representative child sizes, masked
widths, sink/resident shape, and fan-in — showing 2M sits safely past the
crossover with margin. Parent cardinality alone does not fix every crossover;
the benchmark documents the shape and the conservative-default rationale. Live
per-host calibration is deferred (fits Q3 next-phase tuning).

## 5. Acceptance tests (pin before build — PLAN-defines-acceptance rule)

Selection:
- T1 sub-threshold → `_stream_table` (route spy), output unchanged.
- T2 at/above threshold + budgets → `stream_table`; output equals `_stream_table`
  on the same fixture (exact — 6.1).
- T3 above threshold but `budget_bytes`/disk budget None → falls back to
  `_stream_table`, no raise.
- T4 root table, huge rows → never reorder.
- T5 present-but-too-small budget on a reorder-selected table →
  `out_of_core_reorder_budget_too_small`, sink aborted, no output published.
- T6 relation cardinality is the decision key: a table with many duplicate/null
  raw parent rows but few distinct keys routes by the deduped count (distinguishes
  cardinality from source rows).
- T7 mixed multi-table job: each table routed independently; whole-job output
  equals all-batch_join (6.1).

Memory (route-level, through `run_fk_out_of_core`):
- T8 `connect_duckdb` limit spy: join conn = `F_DUCKDB`·ceiling, build conn =
  build cap, for sink+resident × N=1,N>1. FAILS on the round-1 (batch-model)
  sizing, PASSES on the fix — proves it load-bearing.
- T9 subprocess RSS proof at the REAL ceiling (not 1.35×) for a table with
  N incoming edges: peak within ceiling.
- T10 `resolve_reorder_budgets(co_resident_readers=N)` admission: passes while
  `N ≤ 2·fan_in`, raises `out_of_core_reorder_too_many_edges` beyond, naming N +
  fan_in. Input-validation cases: `co_resident_readers ≤ 0`, `merge_fan_in < 2`,
  `remaining_disk_bytes < 0` each raise (not silent clamp).

Disk:
- T11 `require_disk` admission: no join begins when required > available (assert
  the ledger raises `out_of_core_reorder_insufficient_disk` BEFORE the first
  `connect_duckdb`); multi-edge case charges prior edges' retained final runs.
- T12 phase-1 cannot silently exceed its allowance (write-side check).
- T13 table-boundary `check_temp_disk_budget` still fires for a reorder-routed
  job that overruns accumulated cross-table disk.

Parity (6.1):
- T14 forced-reorder (threshold lowered via the real config seam) equals
  `_batch_join` EXACTLY across: 4 orphan policies (PRESERVE, REMAP, WARN, FAIL);
  resident and LazySource+sink paths; `code_set_corpora`; warning order+content;
  unconfigured-column projection warn AND fail; keyed-mask plumbing; exact Arrow
  schema+metadata; composite + overlapping edges; empty/null/NaN columns; every
  payload strategy admitted by `_compat.py`. Where the batch route raises, assert
  the reorder route raises identically (no "route raised, return" skip).

## 6. Contracts

### 6.1 Parity (P0-3, precise)

Task 7's invariant is **reorder ≡ `_batch_join`, exactly**, on: Arrow schema +
metadata, row/chunk order, values + null/NaN representation, warnings (order and
content), `quality_metrics`, and serialized sink bytes on the sink path.
`_batch_join` is the oracle-conformant baseline; its own pandas-oracle parity is
the *normalized-value* contract already documented for that route — Task 7 does
NOT re-open it and does NOT claim byte-identity to the pandas oracle beyond what
`_batch_join` already guarantees. Any reorder-vs-batch divergence is a P0 and
blocks the slice. Route choice may change timing and memory, never output.

### 6.2 Risk

R2 — changes which route runs above the threshold. Mitigations: fail-safe
selection (budget-absent/sub-threshold/root keep current behavior); T14 makes
route choice output-invariant by construction; memory bound verified per-phase
and enforced by T8/T9; disk fail-closed before spill (T11); pathological fan-in
fail-closed (T10). No R3 side effects. Held on `feat/native-phase3`; merges only
with explicit Cam go.

## 7. Open questions (gate/Cam, non-blocking)

- Q-A: build-conn cap = `F_DUCKDB`·ceiling (simple single envelope) vs a larger
  freed-budget cap for build throughput. v2 picks the simple bound; gate may
  argue for more.
- Q-B: RESOLVED (count all incoming edges).
- Q-C: fixed 2M threshold + checked-in benchmark acceptable for merge, with live
  calibration deferred?

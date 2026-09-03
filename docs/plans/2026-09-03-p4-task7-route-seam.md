# P4-A Task 7: reorder-route live seam (auto-select by parent-key size)

Status: plan (DRAFT — pending Codex plan-gate). Author: Opus, 2026-09-03.
Held target branch `feat/native-phase3`; merges with the Phase-4 bundle.

Cam decision (2026-09-03): build the two reorder-job safety fixes now, then wire
the route live so large jobs use it — auto-selected by parent-key size, small
tables stay on `_batch_join`. This slice is the wiring plus the two obligations
the Task 6 gate deferred. It is a route-selection **behavior change** (R2).

## 1. Why this slice

Task 6 built the standalone reorder driver (`_stream_driver.py::stream_table`):
a three-phase, single-source-read FK join whose wall time stays ~flat as
parent-key count grows, where the current `_batch_join` driver
(`_runner.py::_stream_table`) scales super-linearly (measured ~4.5x slower at
10M parent keys). Nothing selects it yet — `run_fk_out_of_core` calls only
`_stream_table` (`_runner.py:202`). Task 7 makes the router pick the reorder
driver for tables whose parent-key count is past the crossover, and closes the
two safety obligations the Task 6 Codex gate recorded before the route may run a
real workload:

1. **Disk-reservation guard.** The reorder driver spills in two places (phase-1
   payload store, phase-2 external sort). Its intra-table peak includes the
   k-way merge's real 2x (old runs + merged run co-resident) that a naive
   `staging + output` estimate misses. `_reorder_budget.require_disk` /
   `resolve_reorder_budgets(remaining_disk_bytes=...)` exist to size for this,
   but `stream_table` is not yet threaded to them.
2. **Multi-edge bounded-RSS.** `resolve_reorder_budgets` sizes the sorter as
   `run_bytes_cap = F_SORT * ceiling` for ONE sorter and takes no edge count.
   Phase 3 holds every incoming edge's `_OrderedJoinRows` merge reader open at
   once (Task 6 docstring: "the sorters' bounded merge readers stay open — and
   bounded by run_bytes_cap each — through phase 3"). With F_SORT=0.15 and the
   DuckDB share (0.55) freed in phase 3 (no join connection live) plus the
   >=0.30 reserve giving ~0.85 of ceiling, N co-resident readers at
   `run_bytes_cap` each exceed the ceiling once N >= ~6. A table with 6+
   incoming FK edges is an unbounded-memory hole today. This is a real guard,
   not a proof-only obligation.

## 2. Scope

IN:
- Route selection in `run_fk_out_of_core`'s topo loop (`_runner.py`).
- Multi-edge sort-budget fix in `_reorder_budget.resolve_reorder_budgets`.
- Disk-budget threading from the selection site into `stream_table`.
- Acceptance tests: selection behavior, fail-closed/fallback, multi-edge RSS
  bound (subprocess RSS proof), disk-guard, byte-parity across both routes.

OUT (unchanged this slice):
- The reorder driver's internals (Task 6, frozen — byte-parity is pinned).
- `_batch_join` internals.
- Any change to full-frame / chunked / sequential routes.
- Threshold auto-tuning: the crossover constant is a fixed, documented default
  this slice; live calibration is future work (Q3-adjacent), noted below.

## 3. Design

### 3.1 Route selection (auto by parent-key size)

The topo loop already has, per child `table_name`, its `incoming_edges` and the
built `parent_relations` for those edges (parents precede children in topo
order). A table's decision key is its **max incoming parent-key count**:

    parent_key_count = max(
        (len(parent_relations[edge]) for edge in incoming[table_name]),
        default=0,
    )

Selection predicate (fail-safe toward current behavior):

    use_reorder = (
        parent_key_count >= REORDER_PARENT_KEY_THRESHOLD
        and budget_bytes is not None
        and temp_disk_budget_bytes is not None
        and incoming[table_name]            # root tables (no incoming FK) never reorder
    )

- `REORDER_PARENT_KEY_THRESHOLD`: a named module constant in `_runner.py` (or a
  small `_route_policy.py` if `_runner.py` is at its size cap), set from the A/B
  crossover. Conservative default: the point where `_batch_join` is clearly
  losing but well inside where reorder is proven, not the exact intersection.
  Proposed value **2_000_000** parent keys (batch_join ~flat-to-slightly-worse
  below ~1M, ~4.5x worse by 10M; 2M sits safely past the crossover with margin).
  The value is documented and overridable; acceptance tests pin the *behavior*
  (below → batch_join, at/above → reorder), never the literal number.
- **Budget-absent → stay on `_batch_join`.** The reorder route requires both a
  memory ceiling and a disk budget (`resolve_reorder_budgets` fails closed
  without them). Route selection must therefore treat "no budget" as
  "not eligible for reorder" and fall back to the current driver, NOT raise.
  This preserves every budget-less caller's behavior exactly (no regression),
  and confines fail-closed to the case where reorder was actually entered.
- Below threshold, at/without budgets, or root table → `_stream_table`
  verbatim, byte-for-byte current behavior.

When `use_reorder`, size the reorder budgets once at the call site:

    remaining_disk = _remaining_disk_bytes(root, temp_disk_budget_bytes)
    budgets = resolve_reorder_budgets(
        process_ceiling_bytes=budget_bytes,
        remaining_disk_bytes=remaining_disk,
        merge_fan_in=_DEFAULT_MERGE_FAN_IN,
        co_resident_sorters=len(incoming[table_name]),   # 3.2
    )
    stream_table(
        ..., run_bytes_cap=budgets.run_bytes_cap,
        merge_fan_in=_DEFAULT_MERGE_FAN_IN,
        memory_limit=budgets.duckdb_memory_limit,   # str form, as stream_table expects
        budget_bytes=budget_bytes, ...
    )

`stream_table`'s signature already accepts `run_bytes_cap`, `merge_fan_in`,
`memory_limit`, `budget_bytes`, `temp_dir`, `relation_dir`, `staging_path`,
`sink`, `outputs`, `warnings`, `code_set_corpora`, etc. — the call mirrors the
existing `_stream_table` call with the reorder caps added. Temp paths stay under
the same `root/{joins,relations,staged}/<table>` subtree so the table-boundary
`check_temp_disk_budget(root, ...)` already covers spill accumulation.

### 3.2 Multi-edge bounded-RSS fix (root cause, in the budget module)

Add `co_resident_sorters: int = 1` to `resolve_reorder_budgets`. The sort share
is divided across the sorters that are co-resident at phase-3 peak so N merge
readers together stay within the sort share:

    per_sorter_sort_bytes = round(F_SORT * process_ceiling_bytes / max(1, co_resident_sorters))
    run_bytes_cap = per_sorter_sort_bytes
    # MIN_RUN_BYTES check now applies to the PER-SORTER cap:
    if run_bytes_cap < MIN_RUN_BYTES: raise out_of_core_reorder_budget_too_small

Rationale: phase 2 fills sorters one edge at a time (single live join), so fill
residency is one sorter at F_SORT/N — safely under F_SORT. Phase 3 opens all N
merge readers at once; N * (F_SORT/N) = F_SORT total, back inside the single
proven share, with the freed DuckDB fraction + reserve as headroom. The bound
now holds for ANY N by construction, at the cost of a smaller per-sorter buffer
(more spill runs, bounded slowdown) on high-fan-in tables — the correct
memory-first trade. `co_resident_sorters=1` reproduces today's exact sizing for
the common single-edge table, so single-edge behavior and its RSS proof are
unchanged. The import-time `F_DUCKDB + F_SORT` reserve assertion is untouched.
The `require_disk` ledger is unaffected (disk 2x peak is per-merge, not per-N).

### 3.3 Disk guard

`resolve_reorder_budgets` already takes `remaining_disk_bytes` and fails closed
without it (3.1 guarantees it is present whenever reorder is selected).
`_remaining_disk_bytes(root, temp_disk_budget_bytes)` = the run's temp-disk
ceiling minus current `root` usage (reuse `_budget`'s directory-walk helper the
boundary check already uses; do not re-implement). This gives the reorder
sizing the *live remaining* budget, not the whole-run figure, so a table late in
the topo order sizes against what disk is actually left. The existing
table-boundary `check_temp_disk_budget(root, max_bytes=temp_disk_budget_bytes)`
stays as the accumulation enforcer and fires identically for both drivers (it
walks `root`, driver-agnostic). No new disk mechanism is introduced.

## 4. Acceptance tests (behavior + failure modes; pin before build per PLAN rule)

Selection:
- T1 parent_key_count below threshold → `_stream_table` chosen (assert via a
  route-evidence hook / spy), output byte-identical to today.
- T2 parent_key_count at/above threshold, budgets present → `stream_table`
  chosen; output byte-identical to `_stream_table` on the same fixture AND to
  the pandas oracle (both routes must agree — this is the core parity gate).
- T3 at/above threshold but `budget_bytes is None` (or disk budget None) →
  falls back to `_stream_table`, NO raise (budget-less regression guard).
- T4 root table (no incoming edges) with a huge row count → never reorder.
- T5 mixed multi-table job where some tables cross and others don't → each
  table routed independently, whole-job output byte-identical to all-batch_join
  and to the oracle.

Multi-edge RSS + budget:
- T6 `resolve_reorder_budgets(co_resident_sorters=N)` returns run_bytes_cap ==
  single-sorter/N (parametrized N=1,2,4,8); N=1 equals the pre-change value
  exactly.
- T7 `co_resident_sorters` large enough to drop per-sorter cap under
  MIN_RUN_BYTES → `out_of_core_reorder_budget_too_small` (fail closed, clear
  message).
- T8 subprocess RSS proof: a table with N>=6 incoming FK edges run through
  `stream_table` with a tight ceiling keeps peak RSS within the ceiling (mirror
  the existing single-edge sorter RSS subprocess proof; this is the test that
  would FAIL on today's un-divided sizing — it must fail on `co_resident_sorters=1`
  sizing and pass on the fix, proving the guard is load-bearing).

Disk:
- T9 reorder selected with a `remaining_disk_bytes` too small for the k-way
  merge 2x → `require_disk` fails closed before spilling (not a mid-run crash).
- T10 table-boundary `check_temp_disk_budget` still fires for a reorder-routed
  job that overruns accumulated disk (driver-agnostic enforcer unchanged).

Parity (the non-negotiable):
- T11 across all FK shapes × orphan policies (FAIL/WARN/DROP) × sink and
  resident paths, a job forced onto the reorder route (threshold lowered in the
  test) is byte-identical to the same job on `_batch_join` and to the pandas
  oracle. Reuse Task 6's parity corpus; add the router in front of it.

## 5. Risk class

R2 — changes which execution route runs for real workloads above the threshold.
Mitigations: (a) fail-safe selection (budget-absent and sub-threshold both keep
current behavior); (b) byte-parity gate T2/T5/T11 makes route choice
output-invariant by construction; (c) memory bound now holds for any N; (d)
disk guard reuses the existing enforcer. No R3 side effects. Held on
`feat/native-phase3`; merges only with explicit Cam go.

## 6. Byte-parity obligation

The whole point of route selection is that it is output-invariant: the reorder
route and `_batch_join` must produce byte-identical output to each other and to
the pinned pandas oracle for every masking config. Any divergence is a P0 and
blocks the slice. Route choice may change timing and memory, never bytes.

## 7. Open questions (for the gate / Cam, not blockers to building)

- Q-A: threshold value. Proposed 2_000_000 parent keys as a documented,
  overridable default; live per-box calibration is deferred (fits Q3's
  next-phase tuning work). Gate: is a fixed conservative default acceptable for
  the merge, with calibration as a follow-up?
- Q-B: should `co_resident_sorters` count ALL incoming edges, or only those on
  FAIL/WARN paths that actually stay open through phase 3? Conservative choice:
  all incoming edges (upper bound). Refinement is a later optimization, not a
  correctness need.

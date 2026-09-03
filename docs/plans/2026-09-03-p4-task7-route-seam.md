# P4-A Task 7: reorder-route live seam (auto-select by parent-key size)

Status: plan (DRAFT v4 — remediated after Codex plan-gate rounds 1-3 NO-GO).
Author: Opus, 2026-09-03. Held target branch `feat/native-phase3`; merges with
the Phase-4 bundle. Route-selection behavior change (R2).

Cam decision (2026-09-03): fix the reorder job's safety gaps now, then wire the
route live so large jobs use it — auto-selected by parent-key size, small tables
stay on `_batch_join`.

Cam decision (2026-09-03, round 3): **the reorder route's disk-safety bar
MATCHES the existing `_batch_join` OOC route** — the route-entry `_spill_estimate`
advisory plus the table-boundary `check_temp_disk_budget`, no new enforced
aggregate sorter quota. Neither route has a hard within-table disk cap today, and
Task 7 does not add one to only one of them. Hardening disk safety, if wanted
later, is a separate slice for the WHOLE OOC route. This decision removes the
enforced-quota / `max_temp_directory_size` / `_emit`+`_relation`+`_stream_join`
disk-wiring work that round-3 P0/P1 demanded; the reorder route INHERITS the
existing disk safety unchanged.

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
  an existing per-edge disk ledger primitive (`staging + 3×output`) that ENFORCES
  a per-edge rejection. It has zero production callers, and **Task 7 does NOT wire
  it** — that would impose the enforced-quota bar Cam declined (§0 round-3
  decision, §4.3). It stays an unused primitive.
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
| 2-3 join + resolve | phase 2: DuckDB join (`F_DUCKDB`) + active sorter fill (`F_SORT`, ×2.2 flush transient) co-resident during drain; phase 3: N lazy heads @ `run_bytes_cap/(2·fan_in)` + O(batch) | existing `resolve_reorder_budgets`; **memory contract = the RSS envelope, 1.35× ceiling, scoped to phases 2-3** (§4.1) |
| build (joiners released) | one dedup conn @ undivided sink build cap | existing capacity preflight + the build's OWN measured envelope (`test_out_of_core_relation_dedup_memory.py`, ~1.6× of the DuckDB limit) |

The memory contract is NOT a single whole-route 1.35× number (round-3 P1-2): the
1.35× ceiling envelope covers the reorder join+resolve phases (2-3); the outgoing
relation build is bounded separately by its existing dedup-memory envelope, which
the capacity preflight already gates. Task 7 proves phases 2-3 to 1.35× at the
admitted maximum fan-in (§5 T12) and leaves the build's contract as-is.

**Bounded residency requires the sink + `LazySource` shape.** Without a sink,
`ResidentPayloadStore` retains every masked batch and the driver materializes
`list(rewritten())` (`_stream_driver.py:395`) — O(output), by design. So
auto-selection is **sink-path only** (§4.4); resident reorder stays reachable for
parity tests but is never auto-selected.

## 3. Scope

IN:
- `_stream_driver.py`: on the reorder path, size the joiner connection from
  `ReorderBudgets` (not `resolve_phase_memory_limits`); keep the outgoing-build
  cap = existing undivided sink build cap. (No new disk mechanism — §4.3.)
- `_reorder_budget.py`: add the phase-3 multi-edge head-fit helper + input
  validation (no change to `F_*` fractions or `run_bytes_cap`).
- `_relation.py`: metadata-only — add a `ParentKeyRelation.key_count` cached
  property (`pq.read_metadata(self.path).num_rows`). File is 592/600 LOC, so a
  small cached property fits; if it would breach the cap, put the footer-read
  helper in `_route_policy.py` instead and drop the property.
- new leaf `_route_policy.py` + `_runner.py`: sink-path route selection; extract
  `_table_order`/`_edge_indexes` into `_route_policy.py` to keep `_runner.py ≤ 676`.
  `_route_policy.py` imports: relation metadata (`pyarrow.parquet` /
  `ParentKeyRelation`), `_reorder_budget`, and `_memory_estimate`
  (`memory_limit_for`, for the join/build cap strings) — cycle-free (none import
  `_runner`/`_stream_driver`); `_budget` only if a concrete symbol is needed.
- `_pipeline.py` + `_pipeline_route_exec.py`: thread the
  `out_of_core_reorder_threshold_rows` runtime kwarg (with the module-size
  compensations named in §4.4 — three files are at/near their sentry ceilings).
- Tests + a calibration benchmark; docstring corrections (§6.3).

OUT: `_batch_join` internals; full-frame/chunked/sequential routes; the reorder
driver's phase structure and byte-parity logic; the validated `F_*` fractions and
the capacity preflight (untouched — build cap unchanged); resident-path
auto-selection; live per-host threshold calibration (deferred); **any new disk
mechanism** — the reorder route inherits the existing OOC disk safety unchanged
(§4.3), so `_duckdb.py` / `_emit.py` / `_relation.py` get NO disk-cap wiring, and
`_stream_join.py` gets only a docstring correction (§6.3).

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
`budget_bytes=None`" contradiction: one path, one cap source. Phase 2-3 memory
correctness is inherited from the validated model and proven by the multi-edge
RSS test (§5 T12) to the 1.35× envelope, exercised at the admitted maximum fan-in
with all phase-3 heads concurrently loaded; the outgoing build keeps its own
existing envelope (§2).

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

### 4.3 Disk safety — inherited, no new mechanism (Cam round-3 decision)

Task 7 adds NO new disk mechanism. The reorder route's disk-safety posture is
made IDENTICAL to the existing `_batch_join` OOC route, which Cam accepted as the
bar (round-3 decision, §0):
- Route-entry advisory: `_spill_estimate.enforce_ooc_disk_preflight` (warn-only,
  conservative over-estimate) already runs for the OOC route regardless of which
  child-join driver is chosen — it estimates the whole job's spill footprint and
  logs a WARNING; it does not reject. Unchanged.
- Runtime enforcer: the table-boundary `check_temp_disk_budget(root, ...)` fires
  at every table boundary, walks `root` (driver-agnostic — reorder spills under
  the same `root`), and aborts cleanly (rolling back the transactional sink) if
  accumulated disk exceeds the run quota. Unchanged.

This is the exact safety the shipped OOC route relies on. The known residual —
a within-table transient spill peak can exceed the quota between boundary checks,
and `stream_table` removes its temp subtree before the next boundary check — is
IDENTICAL in kind to `_batch_join`'s (its DuckDB temp is likewise only bounded
between boundary checks), so Task 7 does not regress the route's disk posture;
it matches it. Both routes assume adequate disk headroom for a single table's
transient spill. Reorder may spill somewhat more per table (staged keys + N
sorted runs + merge amplification) than `_batch_join`'s DuckDB temp. The
route-entry advisory runs unchanged, but note honestly: it is calibrated to the
shipped batch posture and assumes payload columns stream to the sink rather than
becoming transient spill (`_spill_estimate`), so it MAY UNDERPREDICT reorder's
additional transient run files. Under Cam's decision this needs no new estimator;
it is disclosed as a known advisory limitation, and the boundary
`check_temp_disk_budget` remains the actual enforcer for both routes. T14 pins
the identical warn-only control flow, not the estimate's numerical adequacy.

`require_disk` / `max_temp_directory_size` / an enforced aggregate sorter quota
are explicitly OUT (they would hold reorder to a higher bar than the shipped
route — deferred to a future whole-OOC-route disk-hardening slice if wanted).
This also means the round-3 P1-1 concern (a child-priced hard cap wrongly
aborting a feasible parent≫child job) cannot arise: there is no new hard cap,
and the boundary check aborts only on genuine quota exhaustion, identically for
both routes.

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
→ `decide_route`. Semantics: `None` = use the default
`REORDER_PARENT_KEY_THRESHOLD = 2_000_000` (module constant); an explicit `0`
means "reorder every eligible sink table" (valid, for forcing/tests); reject
`bool` (a common `True`/`1` confusion) and negative values with a clear error.
Documented on `_pipeline_routing` alongside the sibling routing kwargs;
isolated-run (`_isolated_worker` serializes/forwards JSON-compatible kwargs)
threading covered.

Module-size cost (round-3 P2): `_pipeline.py` and `_stream_join.py` are AT their
sentry ceilings and `_pipeline_route_exec.py` has ~1 line of headroom
(`test_module_size.py`). Threading a kwarg adds a param + a forward line per file,
which the allowlist-shrink-only ratchet forbids. Compensate concretely:
`_stream_join.py`'s parity-docstring correction (§6.3) removes more lines than it
adds (net negative); in `_pipeline.py` and `_pipeline_route_exec.py`, fold the new
kwarg into the existing routing-kwarg passthrough grouping and trim an adjacent
over-long comment block by the same count, so each stays ≤ its ceiling. The build
MUST keep the full module-size sentry green (§5 T17) — if a clean trim is not
found, the fallback is a tiny leaf extraction of the routing-kwarg bundle, not a
ratchet bump. (§5 T17 runs the full sentry.)

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
- T9 override kwarg `out_of_core_reorder_threshold_rows` changes the boundary,
  via a direct call AND an isolated-run call. Enumerate the semantics/validation:
  `None` → default; `0` → reorder every eligible sink table; a positive int →
  that boundary; a negative int → coded error; `True`/`False` (`bool`) → coded
  error (not silently treated as 1/0); a non-integer → coded error. Assert the
  same coded error from both the direct and isolated paths.
- T10 mixed multi-table job: each table routed independently; whole-job output
  == all-`_stream_table` (§6.1).

Memory (route-level, through `run_fk_out_of_core`):
- T11 `connect_duckdb` limit spy: joiner == `F_DUCKDB`·ceiling, build ==
  `memory_limit_for(budget,1)`; FAILS on the batch-model sizing, PASSES on the
  fix. Additionally monkeypatch `resolve_phase_memory_limits` to RAISE and assert
  a reorder-routed job completes — proving the reorder path never consults it.
- T12 subprocess RSS proof (reuse `test_out_of_core_reorder_memory.py` harness),
  scoped to reorder phases 2-3: a sink+`LazySource` job at the ADMITTED MAXIMUM
  fan-in (`N = 2·merge_fan_in`), a threshold-scale parent, real multi-run spill,
  and all N phase-3 heads concurrently loaded near their per-head cap; peak RSS ≤
  `1.35 × ceiling` (`_ENVELOPE_FACTOR`) without relaxing the factor; route
  evidence asserted. The outgoing relation build's memory is covered by its
  existing envelope test — not re-proven here.
- T13 `resolve_reorder_budgets` input validation: `co_resident_readers ≤ 0`,
  `merge_fan_in < 2`, `remaining_disk_bytes < 0` each raise a coded error (no
  silent clamp). Head-fit helper returns pass while `N ≤ 2·fan_in`.

Disk (inherited posture — §4.3; no new mechanism):
- T14 disk-posture parity: a reorder-routed job's disk safety behaves exactly as
  `_batch_join`'s — the route-entry `_spill_estimate` advisory WARNS (not
  rejects) for a tight job, and the job still proceeds. No reorder-specific hard
  cap aborts a feasible parent≫child job.
- T15 table-boundary `check_temp_disk_budget` fires for a reorder-routed job that
  overruns accumulated cross-table disk, aborting cleanly with sink rollback —
  same enforcer, same behavior as the `_batch_join` route.

Parity (§6.1):
- T16 forced-reorder (threshold lowered via the real kwarg) == `_batch_join`
  EXACTLY, with a reorder-route witness asserted per case, across: 4 orphan
  policies (PRESERVE, REMAP, WARN, FAIL); sink + `LazySource`; `code_set_corpora`;
  warning order+content; unconfigured-column projection warn AND fail; keyed-mask;
  exact Arrow schema+metadata; composite + overlapping edges; empty/null/NaN
  columns; every payload strategy in `_compat.py`; every admitted parent-key
  strategy. Where `_stream_table` raises, assert `stream_table` raises the same
  type+code+message and leaves the sink in the same state.
- T17 the FULL module-size sentry stays green (`test_module_size.py`), not only
  `_runner.py ≤ 676`: `_pipeline.py`, `_stream_join.py`, `_pipeline_route_exec.py`
  each stay ≤ their current ceilings after the kwarg threading + extraction (§4.4).

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
all keep current behavior); T16 makes route choice output-invariant with a
per-case witness; phase-2-3 memory inherited from the validated model + T11/T12,
build memory from its existing envelope; disk posture matched to the shipped
`_batch_join` route (route-entry advisory + boundary enforcer, T14/T15), a
deliberate right-size call (§0). No R3 side effects. Held on
`feat/native-phase3`; merges only with explicit Cam go.

### 6.3 Docstring corrections

`_stream_driver.py` and `_stream_join.py` docstrings currently assert
pandas-oracle byte parity for the reorder path; correct them to the
reorder-equals-`_batch_join` contract (§6.1). No logic change. `_stream_join.py`
receives ONLY this docstring correction (no disk-cap wiring — §4.3, and no kwarg
threads through it); the correction is net line-negative, so the file stays under
its sentry ceiling.

### 6.4 Follow-on touch points (round-3 P3)

- Moving `_table_order` / `_edge_indexes` out of `_runner.py` requires updating
  their importers: `tests/unit/execution/_stream_driver_harness.py` and any stale
  `_compat.py` comment that identifies `_runner.py` as their owner.
- The new kwarg is documented on `_pipeline_routing` (add it to the doc scope
  alongside the sibling routing kwargs).

## 7. Calibration benchmark pass rule (round-3 P2)

`scripts/native-testing/reorder_crossover_bench.py`: for each shape (child sizes,
masked widths, sink, fan-in), run each route `R` repetitions (R ≥ 5), report
median and p90 wall time. Pass rule for the 2M default: at `parent_key_count ≥ 2M`
the reorder route's median wall time is ≤ `_batch_join`'s median by a margin ≥
20%, AND reorder's p90 ≤ `_batch_join`'s median (tail does not erase the win);
below ~1M the two are within run-to-run variance (no regression claim). Record
the results file in-repo alongside: warmup reps discarded, the exact shape matrix
(child sizes, masked widths, sink flag, fan-in values), the quantile method
(median + p90, linear interpolation), the environment (CPU, RAM, disk type), and
the pinned dependency versions (pyarrow, duckdb). Live per-host calibration
deferred (Q3).

## 8. Open questions (gate/Cam, non-blocking)

- Q-A: RESOLVED — build cap unchanged (existing sink build cap), so no preflight
  change.
- Q-B: RESOLVED — count all incoming edges; high fan-in falls back to `_batch_join`.
- Q-C: RESOLVED — disk-safety bar matches the existing OOC route (Cam round-3);
  no new disk mechanism.
- Q-D: fixed 2M threshold + checked-in benchmark acceptable for merge, live
  calibration deferred?

Status: plan (Phase D follow-on, reorder-lane parent-size admission guard. rev 1 DRAFT. HOLD build until Codex plan-gate GO. Autonomous merge on the full green gate per Cam's standing order; the Codex FINAL prompt must forbid the reviewer from merging. Cam 2026-10-15 approved: surgical guard now + document + V2 backlog for the partitioned-join redesign.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; measure-first / cite the source pattern). Base: engine `origin/main` @ `4422e030`.

# Reorder-lane parent-size admission guard

## 0. Why + scope

The out-of-core (OOC) FK **reorder** sub-lane forces the parent relation as the DuckDB hash
BUILD side, whose resident cost is O(distinct parent keys). DuckDB's `memory_limit` is a SOFT
cap, so a large parent overshoots any engine budget: the 100M proof ran at ~30 GB (uncapped)
and was killed at 14 GB under an 8 GB budget, while the BATCH sub-lane did the same job at
~3.9 GB. An earlier O(parent) admission floor was removed (plan
`docs/plans/2026-07-22-ooc-b-external-reorder-implementation.md` Task 8) and the surviving
capacity check was demoted to ADVISORY, so today NOTHING stops a large-parent job from entering
the unbounded reorder lane.

This slice adds a **budget-derived parent-size guard**: when the largest incoming edge's
parent-key count exceeds what the reorder lane can keep within the memory budget, `decide_route`
**demotes the table to the bounded BATCH lane** (slower, ~O(batch) RSS), instead of running the
reorder lane unbounded.

Key fact that makes this a demotion, NOT a reject (FRAME-confirmed): the reorder lane is an
**optimization, not a correctness requirement**. BOTH OOC sub-lanes emit rows ordered by
`__decoy_row_nr` and are byte-order-identical to the pandas oracle (batch: `ORDER BY
c.__decoy_row_nr` at `_batch_join.py:258`,`:313`; reorder: a `BoundedExternalSorter` keyed on
`__decoy_row_nr` restoring that same order, driver contract `_stream_driver.py:50-52`). The OOC
parity harnesses compare POSITIONALLY (`test_out_of_core_fk_parity.py:131-144` folds + list-eq;
`scripts/fk-scale/_parity.py:10,:35-53` relies on preserved order), so the batch 100M run's
"parity ok" already proves batch preserves input order. Reorder buys SPEED (one join pass vs
re-probing the spilled parent per child batch), never order. Batch is the universal, always-valid
bounded default (`_route_policy.decide_route` already returns it as the fail-safe for non-sink /
no-budget / sub-threshold / high-fan-in / wide-key). So a large-parent job demotes to batch with
byte-identical output and nothing is rejected.

Scope: `out_of_core/_route_policy.py` (`decide_route`, one fallback clause) + `out_of_core/
_reorder_budget.py` (the budget -> safe-key-count math). No change to masked output, row order,
parity, the reorder lane itself, or any reject-before-read code. `_pipeline_routing.py` is NOT
touched (it owns full-frame/SEQ/OOC-route selection, not the intra-OOC reorder-vs-batch choice).

## 1. What exists to build on

- **The seam:** `out_of_core/_route_policy.decide_route(...)` (`:94-158`), called per table in the
  runner loop (`out_of_core/_runner.py:236-243`). It already returns
  `RouteDecision(use_reorder=False, reorder_caps=None)` (-> batch) as the fail-safe fallback for
  every "reorder can't take this" case: non-sink / no budget (`:121-128`), sub-threshold
  (`REORDER_PARENT_KEY_THRESHOLD = 2_000_000`, `:49`), high fan-in (`:121-128`), per-edge wide-key
  (`:154-157`). The guard is one more fallback clause of the identical shape, placed immediately
  AFTER the existing `parent_key_count >= threshold` selection.
- **Signals already in hand inside `decide_route`:**
  - `parent_key_count = max(_parent_key_count(parent_relations[edge]) for edge in incoming_edges)`
    (`:129-131`), read from the deduped relation's parquet footer `num_rows` (`:184-190`) -- the
    SAME count the 2M reorder-selection threshold uses. Reuse it; no new read.
  - `resolve_reorder_budgets(...)` (`:134-136`) yields `duckdb_memory_limit_bytes = round(F_DUCKDB
    * process_ceiling_bytes)` = 0.55 x budget (`_reorder_budget.py:48,:192`). That is the ceiling
    the parent build side must live within, and the right comparison basis.
- **The removed floor (context):** Task 8 removed `enforce_ooc_memory_preflight`'s O(parent)
  reject from the reorder path on the (now-falsified) theory that the spillable parent build is
  bounded by `duckdb_memory_limit`. The surviving capacity check (`_capacity_eval.py`) is advisory
  (`FIT` + `warned=True`), and the only hard refusal left is fan-in impossibility. So the guard
  is reinstating the SAFETY the removal lost, but as a route-demotion (batch exists) rather than
  the old reject-before-read.
- **Per-key byte anchor (calibration starting point):** `_memory_estimate._BUILD_FLOOR_BYTES_PER_ROW
  = 120.0` (`_memory_estimate.py:292`), fit against measured OOM tiers for the RESIDENT build
  floor. It is a starting anchor, not the final constant (see §3).

## 2. The change

- **New budget math in `_reorder_budget.py`** (pure function alongside `resolve_reorder_budgets` /
  `ReorderBudgets`, e.g. `safe_parent_key_ceiling(budgets) -> int`): `safe_parent_keys =
  duckdb_memory_limit_bytes // _REORDER_BUILD_BYTES_PER_KEY`. Define a DEDICATED module constant
  `_REORDER_BUILD_BYTES_PER_KEY` (do NOT reuse `_BUILD_FLOOR_BYTES_PER_ROW`, so recalibrating the
  reorder lane never silently moves the resident-build estimate used elsewhere). Its value is
  calibrated per §3.
- **One fallback clause in `decide_route`**, immediately AFTER the `parent_key_count >= threshold`
  reorder-selection test and alongside the existing wide-key fallback: if `parent_key_count >
  safe_parent_key_ceiling(budgets)`, return `RouteDecision(use_reorder=False, reorder_caps=None)`
  (demote to batch). Otherwise proceed to reorder as today.
- No change anywhere else. Masked output, row order, parity, the reorder execution path, and all
  reject-before-read codes are untouched.

## 3. Calibrating `_REORDER_BUILD_BYTES_PER_KEY` (measure-first; do NOT guess)

The constant must divert the unbounded cases and NOT over-reject jobs the lane handles. The
build MUST derive and JUSTIFY it from evidence, not pick a round number:
- Anchor evidence: the 100M GCP A/B. At ~35M distinct parent keys (the grandchild's parent = the
  child table) the reorder peak was ~30 GB (uncapped, 25.7 GB budget) and >14 GB (killed, 8 GB
  budget); batch did ~3.9 GB. The resident build anchor is 120 B/row.
- The reorder lane uses the parent as the JOIN build side (a potentially different, larger slope
  than the resident-build 120 B/row) AND DuckDB's `memory_limit` is soft and overshoots
  (documented `_reorder_budget.py:24-30`). So err CONSERVATIVE (a larger bytes/key -> a smaller
  safe ceiling -> demote sooner): better to demote a borderline job to the (correct, bounded)
  batch lane than to OOM.
- Cross-check against the existing A/B tooling where present (`scripts/route_reorder_vs_batchjoin_ab.py`,
  `scripts/native-testing/reorder_crossover_bench.py`) and the perf sentinels, WITHOUT new GCP
  spend (Cam dropped further cloud runs). Devbox-scale reasoning + the recorded 100M numbers are
  the basis; state the derivation in the constant's docstring (established-methodology cite).
- Required outcome the constant must satisfy (becomes T2/T3): at an 8 GB budget the 100M-class
  (~35M-key) parent DEMOTES (safe ceiling below 35M), and a moderate parent in the proven-safe
  band (just above the 2M selection threshold) still SELECTS reorder.

## 4. Risks (reproduce, do not smooth)

- **Regressing moderate parents:** the guard sits AFTER the `>= threshold` selection and only
  demotes when `parent_key_count > safe_parent_keys`; the proven-safe band [2M, safe] must keep
  selecting reorder. Existing boundary tests stay green (`test_route_policy_wide_key_fallback.py`
  per-head boundary, `test_narrow_max_key_bytes_still_selects_reorder`).
- **Over/under-calibration:** §3; conservative by construction; validated by T2/T3.
- **Byte-parity / order contract:** unaffected -- the demotion target (batch) is byte-identical by
  construction (§0), pinned by `test_out_of_core_fk_parity.py`.
- **Log hygiene:** a route demotion is silent by design (route choice "changes timing and memory,
  never output", `_runner.py:170-176`). Any telemetry/warning names only the table, the counts,
  and the budget -- never key values or payload (mirror `_route_policy.py`'s coded-error style).
- **Budget absence:** moot -- `decide_route` already returns batch at `:121-128` before budget math.

## 5. Acceptance matrix (mirror `tests/unit/execution/test_route_policy_wide_key_fallback.py`)

- **T1 large parent demotes:** a parent above the budget-derived ceiling ->
  `decide_route(...) == RouteDecision(use_reorder=False, reorder_caps=None)` (batch). Mutant:
  dropping the guard clause -> reorder selected -> killed.
- **T2 boundary triple:** just under the ceiling -> `use_reorder is True`; at the ceiling and just
  over -> `False` (model on `test_width_admission_boundary_matches_per_head_cap_arithmetic`).
- **T3 moderate parent still reorders:** a parent in [2M, safe] -> `use_reorder is True`,
  `reorder_caps is not None` (guards against over-rejection); AND the 100M-class (~35M-key) parent
  at an 8 GB budget demotes (the calibration-outcome assertion from §3).
- **T4 safe-key-ceiling math:** unit-test `safe_parent_key_ceiling(budgets)` directly against
  known budgets (e.g. 8 GB -> duckdb_limit 4.4 GB -> ceiling = 4.4 GB // _REORDER_BUILD_BYTES_PER_KEY),
  so the constant + formula are pinned independent of `decide_route`.
- **T5 end-to-end byte-parity:** a forced-large-parent job completes via batch and matches the
  oracle POSITIONALLY (extend `test_out_of_core_fk_parity.py`); proves the demotion preserves
  output + order.
- **T6 perf sentinel (devbox-feasible, NO GCP):** a parent that WOULD select reorder but is above
  the ceiling, demoted to batch, keeps VmHWM within the batch envelope at a devbox-feasible scale
  (extend/sibling `tests/perf/test_out_of_core_reorder_memory.py`); a scaled proxy, not a 100M run.

## 6. Verification + gates

- ci-mirror venv for routing/pure-Python; abi-3 companion venv for any OOC parity (T5). `~/bin/
  pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`.
- T1-T6 green; routing + OOC-parity + reorder-memory suites green; no existing boundary/parity test
  weakened. Coverage + mutation on the new clause + the ceiling math (drop-guard, over-reject,
  boundary mutants killed).
- Module sizes: `_route_policy.py` 242 and `_reorder_budget.py` 256 both have headroom under 600;
  add the clause + the pure function; census only if a file crosses 600 (not expected). Do NOT
  touch `_pipeline_routing.py` / `_pipeline_route_exec.py` / `_capacity_eval.py`.
- Established-methodology note on `_REORDER_BUILD_BYTES_PER_KEY` (the derivation + the soft-limit
  overshoot rationale), citing the reorder-memory FRAME + the 100M A/B record.
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build freely; autonomous merge on the full
  green gate, flag Slack before merge-to-main, PR, PR CI green, merge, confirm main CI. **The
  Codex FINAL prompt MUST say: do NOT merge/push/PR; Opus merges after CI.**

## 7. Done when

- [ ] T1-T6 green; large-parent reorder jobs demote to batch (byte-identical + order-preserving);
  moderate parents still reorder; the calibrated constant is justified from evidence.
- [ ] Changed-lines coverage + mutation-proven; module sizes within policy.
- [ ] diff + evidence to Slack; PR CI green; merge; main CI green.
- [ ] DOCUMENT: ROADMAP + RECENTLY-SHIPPED updated (guard shipped); the reorder-memory finding
  recorded (the wiki note `30_Resources/Decoy-Execution-Routing-and-Memory-Model.md` exists);
  **add the V2 backlog entry** (below); engine graphify refresh (note if none cached).

## 8. V2 backlog (deferred; Cam 2026-10-15)

A genuinely memory-bounded reorder lane FOR HUGE PARENTS needs a partitioned / blocked join so
neither side builds an O(distinct-keys) hash table, PLUS a large-parent RSS proof to replace the
small-parent one (`tests/perf/test_out_of_core_reorder_memory.py` `_PARENT_ROWS=500`). That is an
architecture task (the deferred "Task 4" large-parent bound), NOT this patch. Record as a V2
roadmap entry: "bounded reorder for large parents (partitioned join)". Until then, large-parent
order-sensitive jobs run correctly on the batch lane (slower), which this guard routes them to.

cam

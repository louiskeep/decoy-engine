Status: plan (Phase D follow-on, reorder-lane parent-size admission guard. rev 2 (folds Codex plan-gate rev-1 NO-GO: 2 HIGH + 3 MED + 1 LOW -- scope as an empirical CARDINALITY BACKSTOP over the measured narrow-key domain, not a universal build-fit guarantee (keys are variable-width); calibrate the constant from the feasible interval the recorded anchors define via a reproducible worksheet, not peak/count; fix the T2 `>` boundary; T5 uses the route-seam-parity harness with sink+budget+batch-vs-reorder witness; T6 is a real subprocess with a concrete RSS bar; compare against the ACTUAL enforced DuckDB cap). Demotion design + seam + module placement AFFIRMED by the gate. HOLD build until Codex plan-gate GO. Autonomous merge on the full green gate; the Codex FINAL prompt must forbid the reviewer from merging. Cam 2026-10-15: surgical guard now + document + V2 backlog.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; measure-first / cite the source pattern). Base: engine `origin/main` @ `4422e030`.

# Reorder-lane parent-size admission guard

## 0. Why + scope

The out-of-core (OOC) FK **reorder** sub-lane forces the parent relation as the DuckDB hash
BUILD side, whose resident cost grows with distinct parent keys. DuckDB's `memory_limit` is a SOFT
cap, so a large parent overshoots any engine budget: the 100M proof ran at ~30 GB (uncapped) and
was killed at 14 GB under an 8 GB budget, while the BATCH sub-lane did the same job at ~3.9 GB. An
earlier O(parent) admission floor was removed (plan `docs/plans/2026-07-22-ooc-b-external-reorder-
implementation.md` Task 8) and the surviving capacity check is ADVISORY, so today nothing stops a
large-parent job from entering the unbounded reorder lane.

This slice adds a **budget-derived parent-size guard**: when the largest incoming edge's
parent-key count exceeds a budget-derived ceiling, `decide_route` **demotes the table to the
bounded BATCH lane** (slower, ~O(batch) RSS) instead of running the reorder lane unbounded.

Demotion, NOT reject (gate-affirmed): the reorder lane is an OPTIMIZATION, not a correctness
requirement. BOTH OOC sub-lanes emit rows ordered by `__decoy_row_nr` and are byte-order-identical
to the oracle (batch `ORDER BY c.__decoy_row_nr` `_batch_join.py:258`,`:313`; reorder restores that
same order via a `BoundedExternalSorter`, driver contract `_stream_driver.py:50-52`), and the OOC
parity harnesses compare POSITIONALLY, so the batch 100M "parity ok" already proves batch preserves
input order. Reorder buys SPEED, never order. Batch is the universal, always-valid bounded default
(`decide_route` already returns it for non-sink / no-budget / sub-threshold / high-fan-in /
wide-key). So a large-parent job demotes to batch with byte-identical output; nothing is rejected.

**What this guard IS (scoped, honest):** an EMPIRICAL CARDINALITY BACKSTOP calibrated over the
MEASURED key domain (the narrow, hash/length-prefixed FK keys the recorded runs used). It is NOT a
universal "admitted parent builds always fit the budget" proof: the join build holds variable-width
encoded keys (`_fk_keys.fk_join_key_tuple` is length-prefixed, `_fk_keys.py:210`) plus masked
parent components, and the existing per-edge wide-key fallback (`_route_policy.py:154-157`) measures
the SLIM SORTER row per-row, not the aggregate raw-key build. So a cardinality ceiling alone cannot
bound an arbitrarily WIDE-key parent. This patch's guarantee is therefore: over the measured
narrow-key domain, a parent above the ceiling demotes before it can overshoot; wider-key shapes are
a recorded remaining gap, handled conservatively (the existing wide-key fallback still fires, and a
width-aware ceiling is a documented follow-on, see §8). The deferred partitioned-join redesign (§8)
is the real general-case bound and is NOT pulled into this patch.

Scope: `out_of_core/_route_policy.py` (`decide_route`, one fallback clause) + `out_of_core/
_reorder_budget.py` (the budget -> safe-key-count math). No change to masked output, row order,
parity, the reorder execution path, or any reject-before-read code. `_pipeline_routing.py` is NOT
touched.

## 1. What exists to build on

- **The seam:** `_route_policy.decide_route(...)` (`:94-158`), per table in `_runner.py:236-243`.
  It already returns `RouteDecision(use_reorder=False, reorder_caps=None)` (-> batch) as the
  fail-safe for non-sink / no budget (`:121-128`), sub-threshold (`REORDER_PARENT_KEY_THRESHOLD =
  2_000_000`, `:49`), high fan-in, and per-edge wide-key (`:154-157`). The guard is one more clause
  of the identical shape, placed immediately AFTER the `>= threshold` selection and AFTER
  `resolve_reorder_budgets(...)`, before returning reorder caps. (Gate-confirmed: `_runner`
  dispatches reorder ONLY after `decide_route`; direct joiner/driver calls in benchmarks/tests
  bypass it -- documented known scope, §6, not a production bypass.)
- **Signals in hand:** `parent_key_count = max(_parent_key_count(parent_relations[edge]) ...)`
  (`:129-131`, deduped null-filtered parquet footer `num_rows`, `:184-190`) -- the SAME count the
  2M threshold uses; and `resolve_reorder_budgets(...)` (`:134-136`). For the budget basis use the
  ACTUAL enforced DuckDB cap, not the nominal 0.55x: `resolve_reorder_budgets` yields a nominal
  `duckdb_memory_limit_bytes = round(0.55 x process_ceiling)` (`_reorder_budget.py:48,:192`), but
  `memory_limit_for` rounds it through whole MiB and emits a decimal `"MB"` string, so the enforced
  cap is smaller (Codex LOW-6). Compute the comparison basis via `actual_duckdb_cap_bytes(...)`
  (`_memory_estimate.py:147`) so the ceiling reflects what DuckDB is actually told.
- **Removed floor (context):** Task 8 removed the O(parent) reject on the theory the spillable
  parent build is bounded by `duckdb_memory_limit` (falsified by the 100M run). The guard reinstates
  that safety as a route-demotion (batch exists), not a reject.
- **Recorded calibration evidence (no new GCP needed, Codex HIGH-2):**
  - 100M run: ~35M distinct parent keys (grandchild's parent = child table), reorder peak ~30 GB
    uncapped (25.7 GB budget) / killed >14 GB at 8 GB budget; batch ~3.9 GB. The killed number is a
    LOWER BOUND, not a completed peak -- do NOT derive a slope from peak/count.
  - D4a record (`docs/records/2026-10-15-d4a-reorder-revalidation.md:38`): a reorder run COMPLETED
    with 2.0M / 2.2M parents at a **1 GiB** budget, within the established 1.35x RSS envelope -- the
    strongest moderate-scale "must stay on reorder" anchor.

## 2. The change

- **New budget math in `_reorder_budget.py`** (pure function by `resolve_reorder_budgets` /
  `ReorderBudgets`, e.g. `safe_parent_key_ceiling(budgets) -> int`): `safe_parent_keys =
  actual_duckdb_cap_bytes(...) // _REORDER_BUILD_BYTES_PER_KEY`. Dedicated module constant
  `_REORDER_BUILD_BYTES_PER_KEY` (do NOT reuse `_BUILD_FLOOR_BYTES_PER_ROW`), value per §3, with a
  docstring carrying the calibration worksheet.
- **One fallback clause in `decide_route`**, immediately AFTER the `parent_key_count >= threshold`
  selection and budget resolution: if `parent_key_count > safe_parent_key_ceiling(budgets)`, return
  `RouteDecision(use_reorder=False, reorder_caps=None)` (demote to batch). Strictly `>`: at the
  ceiling, stay on reorder.
- No other change. Output, order, parity, the reorder path, reject-before-read codes all untouched.

## 3. Calibrating `_REORDER_BUILD_BYTES_PER_KEY` (measure-first; reproducible worksheet)

Pick a dedicated constant `C` (bytes per parent key) so the guard diverts the unbounded case and
does NOT regress the recorded/pinned moderate cases. The ceiling is `actual_duckdb_cap_bytes(...) //
C` (the ENFORCED cap, §1/LOW-6), so all anchor arithmetic uses the ACTUAL cap, not the nominal
0.55x (GB = decimal 1e9, GiB = 2^30; Codex rev-2 reproduced these):

| Anchor | budget | actual DuckDB cap | constraint |
|---|---|---|---|
| must DEMOTE: 35M keys | 8 GB | 4,196,000,000 B | `C > 119.89` |
| must REORDER (recorded D4a): 2.2M keys | 1 GiB | 563,000,000 B | `C <= 255.9` |
| must REORDER (EXISTING perf test, both children): 420k keys | 128 MiB | 70,000,000 B | `C <= 166.67` |

The third row is the EXISTING `tests/perf/test_d4a_reorder_reachability.py:43` two-sorter witness
(420k parent keys reorder at 128 MiB); a larger `C` would demote its grandchild and BREAK that
test. All three together give integer **`C` in [120, 166] B/key**. Pick CONSERVATIVELY within the
band (larger C -> smaller ceiling -> demote sooner) but with margin below 166 to keep the 420k
witness safe -- recommend `C = 150` (ceiling at 8 GB actual cap ~= 28.0M keys: 35M demotes, the
420k / 2.2M cases stay on reorder with headroom). The build records the chosen value + margin.
`_BUILD_FLOOR_BYTES_PER_ROW = 120` is the resident-build anchor the band sits just above (the join
build side is heavier than the resident floor), consistent with conservative. Do NOT use `30 GB /
35M ~= 857 B/key` -- that gross peak-over-count mixes fixed costs, sorting, masking and is a kill
lower bound; it would demote every recorded moderate case.

The constant's docstring MUST carry a reproducible calibration worksheet: workload (the D4 FK chain
shape + key encoding), engine/DuckDB versions, budgets (with GB/GiB stated), which measurements are
COMPLETED vs KILLED, the fixed-overhead assumption, and the safety margin. Basis = the recorded
runs above plus bounded LOCAL measurements; NO new GCP runs (Cam). Cite the reorder-memory FRAME +
the D4a record (established-methodology).

## 4. Risks (reproduce, do not smooth)

- **Over-rejecting moderate parents:** guard sits AFTER `>= threshold` and demotes only when
  `count > ceiling`; the recorded 2.2M@1GiB case and the proven-safe band stay on reorder (T3).
  Keep existing boundary tests green (`test_route_policy_wide_key_fallback.py`,
  `test_narrow_max_key_bytes_still_selects_reorder`).
- **Wide-key shapes (scoped gap, Codex HIGH-1):** a cardinality ceiling does not bound an
  arbitrarily wide-key parent; this patch is a narrow-key-domain backstop. The existing per-edge
  wide-key fallback still fires; a width-aware ceiling is the §8 follow-on. State the scope plainly;
  do not claim universal build-fit.
- **Calibration honesty (Codex HIGH-2):** the worksheet + the pinned recorded anchors are the
  guard against a made-up slope; conservative direction; validated by T2/T3/T4.
- **Byte-parity / order:** unaffected -- batch is byte-identical by construction, pinned by the
  route-seam parity control (T5).
- **Log hygiene:** demotion is silent by design (`_runner.py:170-176`); any telemetry names only the
  table, counts, and budget -- never key values or payload.
- **Budget absence:** moot -- `decide_route` returns batch at `:121-128` before budget math.

## 5. Acceptance matrix (mirror `test_route_policy_wide_key_fallback.py`; `_SINK = object()`)

- **T1 large parent demotes:** a parent above the ceiling -> `RouteDecision(use_reorder=False,
  reorder_caps=None)`. Mutant: dropping the clause -> reorder selected -> killed.
- **T2 boundary (match the `>` predicate, Codex MED-3):** with all three ABOVE the 2M selection
  threshold: `ceiling-1` -> reorder; `ceiling` -> reorder; `ceiling+1` -> batch. PLUS: a budget
  whose ceiling falls BELOW the 2M selection threshold (the sub-threshold path already returns batch
  -- the guard must not misbehave), and a `threshold`-override of zero.
- **T3 moderate stays on reorder + large demotes (pin all three calibration anchors):** (a) the
  recorded D4a case (2.2M keys, 1 GiB) -> `use_reorder is True`; (b) the 100M-class (~35M keys, 8 GB)
  -> batch; (c) the EXISTING `tests/perf/test_d4a_reorder_reachability.py` (420k keys, 128 MiB, BOTH
  children reorder) stays green unchanged -- it must remain in the suite and pass, proving the
  constant does not demote the two-sorter witness. These pin all three ends of the calibration band.
- **T3b existing fan-in boundary test isolated from the guard (Codex rev-3):** the EXISTING
  `tests/unit/execution/test_route_policy.py:227` `test_t6_admitted_fan_in_boundary_still_eligible`
  uses 5M keys/parent at 1 GiB as scaffolding to exercise the 32-edge-eligible / 33-edge-fallback
  FAN-IN boundary; the C=150 ceiling at 1 GiB (~3.75M) would demote 5M, breaking it. 5M@1GiB
  genuinely SHOULD demote (5M x 150 B = 750 MB > the 563 MB cap; the fixture made no memory-safety
  claim), so ADJUST this fixture's per-parent key count to a value above the 2M selection threshold
  AND below the ceiling with margin (e.g. ~2.5-3M keys) -- OR raise its budget -- while preserving
  its EXACT 32-edge reorder assertion and the 33-edge fallback control. This isolates the fan-in
  boundary from the cardinality guard; it is a scaffolding adjustment, NOT an assertion change.
- **T4 ceiling math unit:** `safe_parent_key_ceiling(budgets)` directly against known budgets via
  `actual_duckdb_cap_bytes(budgets.duckdb_memory_limit_bytes, 1)` (the ENFORCED cap, matching the
  joiner's `memory_limit_for(..., 1)`; NOT nominal 0.55x) -- e.g. 8 GB -> actual cap 4,196,000,000 B
  -> `ceiling = 4_196_000_000 // C`. Pins the constant + formula + the MiB/MB conversion independent
  of `decide_route`.
- **T5 demotion parity (route-seam harness, Codex MED-4):** use the
  `test_out_of_core_route_seam_parity.py:155` pattern -- a sink + resolved budgets + an otherwise
  reorder-eligible fixture whose parent is above the ceiling. WITNESS batch construction and the
  ABSENCE of reorder construction. Compare the demoted (batch) output against an ACTUAL reorder
  control on the same fixture, positionally with exact schema/metadata; and against the oracle under
  its documented normalizations. Do NOT describe either as serialized-Parquet byte identity. (The
  plain `test_out_of_core_fk_parity.py` helper supplies no sink/budget and would bypass reorder
  admission -- not sufficient here.)
- **T6 perf sentinel (real subprocess, devbox-feasible, NO GCP, Codex MED-5):** a FRESH SUBPROCESS
  running the PRODUCTION runner (`run_fk_out_of_core`, through `decide_route`) with lazy inputs + a
  transactional sink, a real parent cardinality ABOVE the calibrated ceiling, the fixture generated
  OUTSIDE the measured worker, and EXPLICIT budget / batch size / timeout / disk requirement / RSS
  bar. Assert the demotion witness AND output completeness + order. The RSS bar is a CONCRETE number
  for THIS fixture (note: recorded batch RSS ~1009 MiB at a nominal 512 MiB ceiling for 2M parents,
  so the bar is the fixture's measured batch envelope, not ~3.9 GB and not universal). It proves
  this fixture's batch envelope, not the 35M envelope or universal boundedness.

## 6. Verification + gates

- ci-mirror venv for routing/pure-Python; abi-3 companion venv for OOC parity (T5/T6). `~/bin/
  pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`.
- T1-T6 green; routing + OOC-parity + reorder-memory suites green. The ONLY existing test adjusted
  is the fan-in fixture (T3b: scaffolding key-count moved, assertions preserved); no other
  boundary/parity/perf test weakened, and `test_d4a_reorder_reachability` stays green unchanged.
  Coverage + mutation on the new clause + ceiling math (drop-guard, over-reject, boundary mutants
  killed).
- Module sizes: `_route_policy.py` 242, `_reorder_budget.py` 256 -- both keep headroom under 600; no
  census, no `_pipeline_routing.py` / `_pipeline_route_exec.py` / `_capacity_eval.py` change.
- Scope note in the code + record: `decide_route` is the only production reorder-selection path;
  direct internal joiner/driver calls (benchmarks/tests) intentionally bypass it.
- Established-methodology note + the calibration worksheet on `_REORDER_BUILD_BYTES_PER_KEY`.
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build freely; autonomous merge on the full green
  gate, flag Slack before merge-to-main, PR, PR CI green, merge, confirm main CI. **The Codex FINAL
  prompt MUST say: do NOT merge/push/PR; Opus merges after CI.**

## 7. Done when

- [ ] T1-T6 green; large-parent (measured-domain) reorder jobs demote to batch (byte-identical +
  order-preserving); the recorded 2.2M@1GiB case still reorders; the calibrated constant is
  justified by the worksheet.
- [ ] Changed-lines coverage + mutation-proven; module sizes within policy.
- [ ] diff + evidence to Slack; PR CI green; merge; main CI green.
- [ ] DOCUMENT: ROADMAP + RECENTLY-SHIPPED updated; the wiki note
  `30_Resources/Decoy-Execution-Routing-and-Memory-Model.md` exists; **add the V2 backlog entries
  (§8)**; engine graphify refresh (note if none cached).

## 8. V2 backlog (deferred; Cam 2026-10-15)

1. **Bounded reorder for large parents (partitioned/blocked join):** so neither side builds an
   O(distinct-keys) hash table, PLUS a large-parent RSS proof to replace the small-parent one
   (`test_out_of_core_reorder_memory.py` `_PARENT_ROWS=500`). Architecture task ("Task 4"), not this
   patch. Until then large-parent order-sensitive jobs run correctly on batch (slower).
2. **Width-aware reorder admission:** replace the cardinality backstop with a build-size estimate
   that accounts for encoded join-key + masked-component WIDTH, so wide-key parents are bounded too
   (this patch's recorded gap, §0/§4).

Record both as V2 roadmap entries.

cam

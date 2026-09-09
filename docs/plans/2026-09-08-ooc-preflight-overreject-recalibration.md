# OOC capacity preflight: advisory build-floor, hard fan-in end-to-end, completion-cap recalibration

Status: plan v6 (Opus, 2026-09-08). Risk R2 (changes a public verdict contract +
cross-repo CLI behavior + capacity control flow). The measure-first step falsified
the row-independence premise of v1-v4; v5 established the correct row-linear,
advisory, no-classifier direction (Codex validated it); v6 folds in v5's gate
findings: calibrate in DuckDB-completion-cap units (not peak RSS), make the fan-in
refusal hard end-to-end (it is not today), and declare the public FIT semantic
change. Cross-repo: decoy-engine + the decoy CLI. Not started; build proceeds under
the two build-phase gates (dennis, Codex-final); merge on Cam's go.

## Frame

The preflight prices each parent table's out-of-core relation-build with a row-linear
floor (`predict_ooc_build_floor_bytes = 24 MiB + 190 B/row`) and REFUSES when the
floor exceeds the DuckDB cap. Value: fail fast with "you need ~N GB" instead of a
mid-run OOM. Defect: over-rejection.

The mandatory measurement (`build_floor_probe`, driving the one `_build_relation`
every build entrypoint including streaming `emit_to_sink` funnels through) showed the
build is row-LINEAR on every path, not the flat envelope v1-v4 assumed. Completion
caps (the DuckDB `memory_limit` a row count needs to finish, not peak RSS):

| rows | completes at | fails at | peak RSS at a passing tier |
|---|---|---|---|
| 5M  | <= 256 MiB | -       | 578 MB @256 |
| 10M | <= 256 MiB | -       | 607 MB @256 |
| 20M | ~768 MiB   | 512 MiB | 1217 MB @768 |

Plus GCP: 33.3M/table completed only at a ~3.3 GB per-instance limit (OOMed near
1 GB). So the split-dedup removed the pre-Phase-4 `arg_max` O(distinct-key) RESIDENT
blowup (the retired 33.3M cloud anchor was that old operator), but a real
row-dependent DuckDB working set remains on all paths (the staged GROUP BY + join-back
over distinct keys). The over-rejection is a MISCALIBRATED SLOPE: `190 B/row`
over-predicts the measured completion-cap need by ~2-4x. No shape classifier is
needed (Codex confirmed): there is no separate bounded builder.

## The fix

1. **Recalibrate the slope in DuckDB-completion-cap units** (Codex BLOCKER). `floor_bytes`
   is compared to `actual_duckdb_cap_bytes` and inverted through the reserve model, so
   it must represent the `memory_limit` REQUIRED FOR COMPLETION, NOT peak RSS. Fit
   `24 MiB + slope * rows` over the measured stable COMPLETION tiers using
   `slope = max((stable_pass_cap_bytes - base) / rows)` across row counts and key
   widths; failed tiers are lower-bound sanity checks. The devbox gives ~40-50 B/row;
   the GCP 33.3M point implies up to ~98 B/row (cross-environment fragmentation), so
   the slope covers the cloud completion need with margin (target ~110-120 B/row,
   FINALIZED from the build-step completion-cap measurement, never pre-committed). The claim is
   "conservative over the measured domain" (typical key widths), NOT an unconditional
   "never under-predicts any requirement". SEPARATELY verify the final whole-GiB host
   recommendation covers observed VmHWM; never feed VmHWM into `floor_bytes`.
   `_BUILD_FLOOR_BASE_BYTES` bumped 24 MiB -> 28 MiB (P2-1 remediation) so
   `floor(100k)` clears the reproduced 40e6 fail edge while `floor(40 rows)`
   stays under the 32e6 tiny-fixture routing knob.
2. **Make the build-floor prediction advisory.** The build-floor branch
   (`_capacity_eval.py:269-293`) returns FIT + `warned=True` + a relation-build-only
   recommended size, not INSUFFICIENT. The job proceeds. Backstops are caller-side and
   CONDITIONAL: the isolated worker's RLIMIT only when `mem_cap_bytes` is set
   (`apply_mem_cap`); the direct in-process path has none, and its only IMMEDIATE
   advisory is the pre-execution logger warning (the residency `QualityWarning` is
   attached AFTER a successful return, so it cannot warn before an OOM -- do not call
   it a backstop).
3. **Keep fan-in HARD, and make it hard END-TO-END** (Codex BLOCKER). Demoting the
   build-floor makes fan-in the only hard refusal, exposing three gaps where it is
   currently NOT hard end-to-end:
   - `evaluate_capacity` returns UNKNOWN for `unresolved_parent_tables` BEFORE the
     fan-in loop. Fan-in needs only `incoming_edge_counts` + budget, so RUN THE FAN-IN
     GUARDS FIRST when a usable budget exists; only then return UNKNOWN for unpriceable
     rows. No usable budget at all -> UNKNOWN (nothing budget-relative to compute).
   - `run_out_of_core_route`'s `except ExecutionError:` (budget resolution) swallows
     `out_of_core_fanin_exceeds_budget` as "detection failed". NARROW the catch to the
     RAM-detection-failed code; always re-raise fan-in.
   - `estimate_job_capacity` re-raises a fan-in from budget resolution, but the CLI
     preflight catches only `capacity_source_unprofilable`. Make the CLI render a
     propagated fan-in as an INSUFFICIENT capacity failure (EXIT_CAPACITY), or
     normalize it to `CapacityEstimate.INSUFFICIENT` before it reaches the CLI.
4. **Declare the public contract change** (Codex MEDIUM). Redefining `FIT` from "clears
   the budget" to "no hard impossibility detected" (with `warned` marking an adverse
   prediction) DOES change a public field's meaning. Pre-GA permits it, but declare it:
   engine + CLI changelogs, `CapacityVerdict`/`estimate_job_capacity` docstrings,
   `capacity.py`, CLI preflight help, and `exit_codes.py`. Retain `decoy run`
   recognition of a legacy `out_of_core_insufficient_memory` build-floor code as
   documented older-engine compatibility.
5. **Companion decoy CLI change.** `cli/preflight.py:459` maps every FIT to a green PASS
   ignoring `warned`. Render `FIT && warned` as a WARNING (status="warn", recommended
   size shown) via `add_warn`, honoring `--fail-on-warning`; default execution proceeds.
   Re-anchor the parity test (`test_run_preflight_capacity_parity.py`): its
   `test_insufficient_agrees` 300k build-floor case becomes mutual-advisory, so switch
   the mutual-refusal assertion to a FAN-IN case (many incoming edges over a tiny
   budget) -- which the fan-in-end-to-end fix (item 3) makes actually refuse on both
   sides -- and add a case asserting the 300k build-floor case WARNS on both.

## Non-goals

- No shape classifier, no `CapacityInputs` row/width expansion, no job-level
  topological live-set, no leaf-table pricing, no whole-output residency model, no
  ceiling-resource redesign, no DuckDB-cap re-sizing. The resident path's whole-output
  accumulation stays UNPRICED as today; the advisory is explicitly relation-build-only
  and excludes resident inputs, accumulated outputs, and ingestion peak.
- No change to the FK drivers, the split-dedup, the route policy, the reserve model,
  or budget-resolution ARITHMETIC (only the fan-in error PROPAGATION is narrowed).

## Measurement (finalizes the slope; completion-cap, not RSS)

Repeat `build_floor_probe` per point with variance recorded. For each row count in
{1M,5M,10M,20M}, BRACKET the stable completing `"NNMB"` tier (account for DuckDB's
non-monotonic tier behavior: a larger limit can fail where a smaller passed, so
confirm a STABLE pass). Fit the slope as `max((stable_pass_cap_bytes - base) / rows)`;
failed tiers are lower-bound checks. Include at least one distinct-single-string and
one composite/wider key point (key width changes the per-row need; without them the
claim is limited to the tested int64-key width). Record the DuckDB setting and
`actual_duckdb_cap_bytes` SEPARATELY. Separately record peak VmHWM per point and
verify the host recommendation covers it. Thread-transition and >100M characterization
stay DEFERRED to the separate benchmark. Log all points in the sprint-testing ledger.

## Acceptance tests (define behavior before build; no later contributor weakens)

1. **Floor is a conservative upper envelope over the measured completion domain.**
   For each measured tier, `predict_ooc_build_floor_bytes(rows) >` the largest FAILING
   tier's cap for that row count (it never hands a job a cap already known to OOM).
   The floor is NOT required to stay at or below the passing cap: a single slope that
   covers the cross-environment worst case (the cloud 33.3M point, ~98 B/row-equiv)
   necessarily over-predicts this devbox's more efficient tiers by ~2-4x, and that
   over-prediction is intended conservatism, not a defect (the advisory demotion means
   it over-recommends memory but never blocks). Stated in DuckDB-cap bytes, NOT peak
   RSS. (Replaces v5's mathematically-impossible peak-RSS criterion, and v6's earlier
   two-sided bracket, which a cloud-covering slope cannot satisfy on the devbox.)
2. **Slope meaningfully gentler than 190** at 20M (the loosening that fixes
   over-recommendation), matching the recalibrated constant.
3. **Host recommendation covers observed VmHWM.** For each measured point,
   `declared_minimum_ceiling_bytes(floor)` implies a host >= the measured peak VmHWM
   (the RSS check lives here, separate from `floor_bytes`).
4. **Build-floor no longer blocks.** A former build-floor INSUFFICIENT case (e.g. 20M
   on 4 GiB) returns FIT + `warned=True` + recommended size; `enforce` does NOT raise.
5. **Fan-in hard END-TO-END.** (a) `(64 MiB, 67 live)` admits / `(68 live)` raises;
   pure-joiner-leaf raises. (b) unresolved rows + fan-in-exceeds + usable budget ->
   INSUFFICIENT (NOT UNKNOWN). (c) auto-detected budget whose fan-in exceeds it ->
   the runtime does NOT swallow it as detection-failed. (d) no usable budget at all ->
   UNKNOWN. (e) CLI: a fan-in job -> EXIT_CAPACITY with INSUFFICIENT rendered, not a
   raw traceback.
6. **`enforce` raises only fan-in codes** (assert the raised code set).
7. **Advisory is relation-build-only.** `estimate_job_capacity` docstring/result + CLI
   help state the recommendation excludes resident inputs, accumulated outputs, and
   ingestion; a formerly-INSUFFICIENT case is FIT-with-advisory; FIT enum redefined and
   the change declared in changelogs/exit_codes docs.
8. **CLI renders advisory as status="warn"** in BOTH human and JSON output, under
   default execution and under `--fail-on-warning` (the latter governs the exit code);
   the parity test's mutual-refusal case is a FAN-IN impossibility; the 300k build-floor
   case is mutual-advisory.
9. **Recommendation units distinct** (`~1.311` cap->host conversion, not 1.25;
   completion-cap floor vs VmHWM check kept separate).
10. **Fail-open / NOT_APPLICABLE unchanged** (`budget None` -> UNKNOWN; CSV / non-OOC ->
    NOT_APPLICABLE / UNKNOWN), except that fan-in now precedes the unresolved-rows UNKNOWN
    when a usable budget exists.
11. **evaluate/enforce parity** on the new boundary points.
12. **Resident whole-output residency unchanged** (unpriced; residency warning still
    fires for the caller-managed shape) -- no regression.

## Steps

1. Finalize the slope from repeated completion-cap measurement (+ key-width points);
   record in the ledger; confirm acceptance test 1 holds with the chosen slope.
2. Recalibrate `_BUILD_FLOOR_BYTES_PER_ROW` + rewrite its derivation comment
   (completion-cap units, row-linear all paths, retired arg_max anchor as historical).
3. Demote the build-floor branch to advisory FIT-with-warning; reorder so fan-in guards
   run before the unresolved-rows UNKNOWN when a usable budget exists; redefine the FIT
   enum docstring; correct the `~1.311` factor.
4. Narrow `run_out_of_core_route`'s budget-resolution catch to the RAM-detection-failed
   code (re-raise fan-in); make `estimate_job_capacity`/CLI render a propagated fan-in
   as INSUFFICIENT/EXIT_CAPACITY.
5. Companion decoy CLI: render `FIT && warned` as status="warn" honoring
   `--fail-on-warning`; update CLI help + changelog + exit_codes doc; re-anchor the
   parity test on a fan-in impossibility + add the build-floor-warns-both-sides case;
   retain legacy `out_of_core_insufficient_memory` recognition.
6. Update every engine test asserting build-floor INSUFFICIENT to advisory; add the
   fan-in-end-to-end cases (5b-5e) and enforce-raises-only-fan-in.
7. Verify: ruff + format + mypy on every changed file (both repos); full `execution`
   unit suite + OOC parity suites (engine) + CLI preflight/parity suites; mutation grade
   on the recalibrated floor, the advisory branch split, and the fan-in guards +
   propagation.
8. dennis gate, then Codex final gate (both repos' diffs). Merge only on Cam's go, after
   green CI. Then run the separate >100M streaming benchmark.

## Risks

- **A gentler slope under-predicts the completion need.** Central risk; mitigated by
  test 1 (floor stays a conservative upper envelope above every measured failing tier,
  covering the cloud 33.3M point)
  and the "conservative over the measured domain / typical key widths" claim (not
  unconditional). In advisory mode a wrong-low recommendation is bounded by the RLIMIT
  where set; the direct path is caller-managed.
- **Fan-in end-to-end control-flow change.** A real production change, bounded to error
  propagation (narrow a catch, reorder two returns, render one CLI error); mitigated by
  the explicit boundary tests 5b-5e.
- **Advisory lets a job start that then overflows.** Accepted by scope (Cam): clean stop
  at the RLIMIT where set; caller-managed otherwise; the advisory states the size up
  front. The direct-run pre-execution logger warning is the only immediate heads-up
  there.
- **CLI drift turning a refusal into a silent PASS.** Mitigated by the companion change +
  the parity re-anchor (test 8), landed with the engine change.

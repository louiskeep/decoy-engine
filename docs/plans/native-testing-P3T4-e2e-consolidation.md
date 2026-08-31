Status: record

# P3-T4 end-to-end C1 gate + parity consolidation (right-sized, final batch)

- **Plan:** `docs/plans/2026-08-30-phase3-c1-test-plan.md`, batch P3-T4 (section 4) and the
  method in section 3 (binding by reference from `docs/plans/2026-08-29-native-efficiency-test-
  plan.md`).
- **Scope:** the frozen e2e references for the Phase 3 C1 surface
  (`tests/parity/native/test_phase3_c1_gate.py`, `tests/parity/native/test_c1_faker_parity.py`);
  their branch-coverage contribution to the five graded Phase 3 units
  (`_phase3_eligibility.py`, `_pool_quality.py`, `_route_diagnostics.py`,
  `generation/pool/_identity.py`, `_dispatch.py`'s changed lines); the no-self-grading
  discipline at the e2e level; the Phase 0 determinism sentries.
- **Branch:** `feat/native-phase3`, worktree `.claude/worktrees/native-phase3-build`.
- **This batch is verification + consolidation, not a re-grade.** P3-T0 through P3-T3 already
  drove per-unit branch coverage and mutation to the plan's bar. Nothing here re-runs a
  full-file `--readjudicate-killed` mutation pilot.
- **Tool versions:** Python 3.13.13, pytest 9.1.1, coverage.py 7.16.0. HEAD `caec455a`
  (`feat/native-phase3`), base `186141ba` (`origin/main`). Companion (`decoy_engine_native`)
  present in `.venv` for this run.

## 1. Frozen e2e references: confirmed, run, and green

Both files are the frozen end-to-end tests for the C1 surface, matching the plan's own
description exactly:

- `tests/parity/native/test_phase3_c1_gate.py` is the consolidated four-criterion gate (module
  docstring, lines 1-8): (1) exact parity vs the pinned oracle -- values, row order, null
  placement, warnings, row errors, logical schema; (2) seed stability + partition invariance
  (the DE-02 seam); (3) bounded state + C1 fidelity (`measure_pool_quality`/
  `enforce_pool_quality`, `RouteDiagnostics` bounded view); (4) intended-route proof via an
  invocation-scoped, independently-observed exact-count route ledger (`_ledger_for_table`,
  built from `NativeRouteEvidence.node_routes` plus a 1:1 input/output chunk-count check, not a
  Cartesian reconstruction from routing intent).
- `tests/parity/native/test_c1_faker_parity.py` is the exact-parity harness for the deterministic
  faker route specifically: values, row order, null placement, and route-tag/column-count
  evidence, swept across batch sizes, row order, an all-null faker column, and non-zero Arrow
  chunk offsets.

Run command and result:

```
PYTHONPATH=src .venv/bin/python -m pytest tests/parity/native/test_phase3_c1_gate.py \
  tests/parity/native/test_c1_faker_parity.py -q
```

```
21 passed in 136.99s (0:02:16)
```

All 21 tests (12 from the gate, 9 from the parity file) pass unchanged, companion-present, well
within the ~3-minute estimate.

## 2. Branch-coverage contribution to the CHANGED Phase 3 units

Full command (companion-present; the two gate/parity files plus every faker-only and
companion-required unit test that exercises the five graded units, including
`tests/native/test_native_dispatch.py` for `_dispatch.py`'s changed lines and
`tests/unit/generation/pool/test_identity.py` for `_identity.py` directly):

```
.venv/bin/python -m coverage run --branch -m pytest -q \
  tests/native/test_phase3_eligibility.py tests/native/test_pool_quality.py \
  tests/native/test_c1_diagnostics.py tests/native/test_c1_bounded_state.py \
  tests/native/test_dispatch_faker.py tests/native/test_native_dispatch.py \
  tests/unit/generation/pool/test_identity.py \
  tests/parity/native/test_c1_faker_parity.py tests/parity/native/test_phase3_c1_gate.py

.venv/bin/python -m coverage report -m --include=\
"*/execution/native/_phase3_eligibility.py,*/execution/native/_pool_quality.py,\
*/execution/native/_route_diagnostics.py,*/generation/pool/_identity.py,\
*/execution/native/_dispatch.py"
```

Result: 218 passed in 243.73s (0:04:03).

| Unit | Stmts | Branch | Cover | Missing |
|---|---:|---:|---:|---|
| `_dispatch.py` (whole file; changed-lines scope, see below) | 219 | 78 | 98% | 288, 538, 613->615 |
| `_phase3_eligibility.py` | 86 | 42 | 98% | 107 |
| `_pool_quality.py` | 90 | 26 | 100% | (none) |
| `_route_diagnostics.py` | 51 | 8 | 100% | (none) |
| `generation/pool/_identity.py` | 13 | 0 | 100% | (none) |

### Every apparent gap traced: no genuinely uncovered changed line

- **`_dispatch.py` line 288** (`reasons.append(f"no_native_kernel_or_pool:{column}:{node.strategy}")`
  inside `_static_route_decision`'s `elif no_kernel and no_pool_path:` branch) -- a Phase 3 diff
  line, but already adjudicated **unreachable-by-contract** in P3-T3
  (`docs/plans/native-testing-P3T3-dispatch-diagnostics.md` section 2.4, mutants
  `x__static_route_decision__mutmut_41/43/50`): `_requirements.py`'s `_fallback_policy` guarantees
  `fallback_policy == "native"` implies the node's strategy is in `NATIVE_KERNEL_STRATEGIES` or
  `NATIVE_POOL_STRATEGIES`, so `no_kernel and no_pool_path` cannot be true for any node that
  reaches this `elif` at all. A defense-in-depth guard against the two allowlists drifting apart,
  not a reachable branch under the current contract; no test can hit it without first breaking
  `_requirements.py` (a different, already-graded unit).
- **`_dispatch.py` line 538** (`return iter(())` in `_mask_native`'s empty-input short-circuit)
  and **613->615** (the `if route_evidence_sink is not None:` branch inside
  `run_native_or_oracle_chunked`'s `empty_input` handling) -- both confirmed against
  `git diff origin/main..HEAD -- src/decoy_engine/execution/native/_dispatch.py` to be
  **unchanged Phase 2 lines**, explicitly out of this batch's (and P3-T3's) scope per the plan's
  instruction not to re-grade unchanged Phase 2 dispatch code. P3-T3's record (section 2.5) lists
  line 538 by name in its "out of scope: unchanged Phase 2" bucket; the 613-615 empty-input block
  is the same pre-existing branch.
- **`_phase3_eligibility.py` line 107** (`continue` inside `if not isinstance(col, dict):`) --
  already adjudicated **unreachable-by-contract** in P3-T1
  (`docs/plans/native-testing-P3T1-eligibility-identity.md`, "Unreachable-by-contract (1)"):
  `phase3_c1_eligibility` calls `native_route_eligibility(config, table=table, profile=profile)`
  FIRST over the identical `table_cfg.get("columns")` list, and that function's own column loop
  has no such guard -- a non-dict entry raises `AttributeError` there before this function's loop
  is ever reached, confirmed by direct reproduction in the P3-T1 record.

**Conclusion: every changed line in the five graded units is covered by the Phase 3 test set.**
The four apparent gaps are all previously-adjudicated unreachable-by-contract branches or
unchanged Phase 2 lines outside this plan's scope, not new findings. No focused test was added
for this batch (the unit batches already drove these to the bar; this is a consolidation check,
not a re-grade, and it confirms rather than contradicts that).

## 3. No self-grading at the e2e level: cited

The logical grader at every e2e comparison point is the pandas oracle or the frozen recipe,
never a native-produced golden.

- **`test_phase3_c1_gate.py` / `test_c1_faker_parity.py`:** both call `run_pipeline(config, ...,
  substrate="pandas", execution_mode="full_frame", auto_chunk=False, ...)` (`_run_oracle_both` /
  `_run_oracle`, `test_phase3_c1_gate.py` lines 204-218; `test_c1_faker_parity.py` line ~143) to
  produce the oracle result, then compare the native output against it via
  `assert_logical_parity(candidate, oracle)` (`tests/parity/native/_fixtures.py` lines 246-279).
  That function's own docstring states the contract directly: "Assert `candidate` is LOGICALLY
  identical to the pinned `oracle`... Compares EXACTLY: output-table set, per-column values,
  null positions, row order, diagnostics (warnings AND row errors), and the logical schema." The
  candidate is always the native route's output and the oracle is always the live pandas run;
  nothing in either file constructs a golden from the native output itself.
- **`_pool_quality.py`'s DuckDB collision measurement** (graded in `test_pool_quality.py`, not
  re-run this batch but cited for completeness since P3-T4's acceptance bar covers it): the
  arithmetic is graded against `_reference_collision_measurement` (`test_pool_quality.py` lines
  112-150), whose own docstring states it "Never reads `PoolQualityMeasurement`, a production
  threshold constant... " and computes purely from raw `(source, masked)` pairs and raw pool
  values -- confirmed by `test_collision_measurement_matches_raw_pure_python_reference` (line
  ~801) and `test_pool_duplicate_rate_matches_raw_distinct_count` (line ~843), the latter's own
  docstring noting the comparison is against "the reference's own counts (never the production
  measurement's)".

No self-grading path exists at either the e2e or the pool-quality-measurement level.

## 4. Determinism sentries: pass unchanged

Named claim only (Phase 0 sentries confirmed to still hold after the whole Phase 3 stack), not
a program-wide claim.

```
PYTHONPATH=src .venv/bin/python -m pytest tests/native/test_determinism_goldens.py \
  tests/native/test_draw_site_inventory_coverage.py -q
```

```
32 passed, 5 warnings in 0.49s
```

The 5 warnings are a pre-existing numpy `timedelta` deprecation warning in
`transforms/windowed_date.py`, unrelated to Phase 3 and not newly introduced.

Both files pass with no golden fingerprint moved:

- `test_determinism_goldens.py` -- reproduces shipped provider output byte-for-byte against
  pinned goldens (windowed-date and related providers).
- `test_draw_site_inventory_coverage.py` -- the `draw_site` inventory sentry, asserting the
  registered set of nondeterministic draw sites in the codebase matches its pinned inventory.

This is a bounded, named claim about these two specific files, not an "anywhere in the program"
assertion.

## 5. Correctness-only scoping (explicit)

This batch, and the whole P3-T0 through P3-T4 program, certifies **primitive and end-to-end
logical correctness** of the Phase 3 C1 native surface. Two things it explicitly does NOT
certify, per the master plan's section 0 and this batch's own instructions:

- **JC-3 perf/RSS**: Cam accepted the JC-3 perf-threshold decision separately; this batch does
  not re-check it, run a bench, or make any wall-time/memory claim.
- **Operational breach-blocks-publication**: `enforce_pool_quality` is proven correct as a
  standalone primitive (P3-T2; this batch's e2e coverage confirms criterion 3 of the frozen gate
  calls it and it does not raise on the frozen recipe), but nothing in the current dispatch path
  wires a pool-quality breach to BLOCK publication of masked output. That operational
  certification -- the coordinator wiring plus an injected-breach test proving a breach actually
  prevents publication -- is deferred to Task 3.5 and its prod-sim leg, out of scope here as
  stated in the master plan.

## 6. Acceptance against the plan's P3-T4 gate

- Frozen e2e references confirmed and run green: met (section 1).
- Branch-coverage contribution measured per unit, every changed line traced to covered,
  previously-adjudicated-unreachable, or out-of-scope-unchanged-Phase-2: met (section 2). No new
  test needed.
- No self-grading at the e2e level, cited with exact assertion sites: met (section 3).
- Named Phase 0 determinism sentries re-run, pass unchanged, bounded claim: met (section 4).
- Correctness-only scoping stated explicitly (JC-3 deferred to Cam's acceptance, operational
  breach-blocks-publication deferred to Task 3.5): met (section 5).

## 7. Reproducible commands

```
# Frozen e2e gate + parity (~2m20s)
PYTHONPATH=src .venv/bin/python -m pytest tests/parity/native/test_phase3_c1_gate.py \
  tests/parity/native/test_c1_faker_parity.py -q

# Coverage contribution over the full Phase 3 native test set (~4m)
.venv/bin/python -m coverage run --branch -m pytest -q \
  tests/native/test_phase3_eligibility.py tests/native/test_pool_quality.py \
  tests/native/test_c1_diagnostics.py tests/native/test_c1_bounded_state.py \
  tests/native/test_dispatch_faker.py tests/native/test_native_dispatch.py \
  tests/unit/generation/pool/test_identity.py \
  tests/parity/native/test_c1_faker_parity.py tests/parity/native/test_phase3_c1_gate.py

.venv/bin/python -m coverage report -m --include=\
"*/execution/native/_phase3_eligibility.py,*/execution/native/_pool_quality.py,\
*/execution/native/_route_diagnostics.py,*/generation/pool/_identity.py,\
*/execution/native/_dispatch.py"

# Named Phase 0 determinism sentries (<1s)
PYTHONPATH=src .venv/bin/python -m pytest tests/native/test_determinism_goldens.py \
  tests/native/test_draw_site_inventory_coverage.py -q
```

mypy is env-blocked by the same pre-existing numpy 2.5.0 stub syntax error the P3-T0 through
P3-T3 records already carried forward (`Type statement is only supported in Python 3.12 and
greater`); not introduced or fixed here. No production or test file was changed this batch, so
there is no `ruff check`/`ruff format` diff to run.

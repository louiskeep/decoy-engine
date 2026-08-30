Status: record

# P3-T0 harness precondition: Phase 3 C1 native mutation + coverage harness

- **Plan:** `docs/plans/2026-08-30-phase3-c1-test-plan.md`, batch P3-T0 (section
  4) and the substrate wrinkles in section 3.
- **Scope:** prove the harness works for the Phase 3 Python units and settle
  the two substrate wrinkles. This batch grades nothing; P3-T1 onward measure
  and fill gaps on the real surface.
- **Branch:** `feat/native-phase3`, worktree
  `.claude/worktrees/native-phase3-build`.
- **Touched:** this report only. No production code changed (the harness
  script `scripts/native-testing/python_mutation_pilot.py` already exists from
  the Phase 2 T0 batch and needed no change here).

Phase 3 is Python-only against `origin/main` (plan section 1: no Rust delta,
the C1 hash columns reuse the unchanged Phase 2 kernel). So this record has no
Lane A/Lane B Rust sections; it reuses the Phase 2 T0 Python runner
(`scripts/native-testing/python_mutation_pilot.py`, itself built on
`scripts/tq_mutate.py`) unchanged, per the plan's own instruction not to stand
up a new harness.

## 1. Environment precondition: this worktree's venv needed `mutmut`

This worktree's `.venv` resolved `decoy_engine` to its own `src/` correctly
(no editable-install cross-contamination from the main checkout), but lacked
`mutmut` entirely -- a fresh venv here had never had the mutation extra
installed. Fix:

```
uv pip install --python .venv/bin/python -e '.[dev,mutation]'
```

`mutmut` lives in the separate `mutation` extra (not `dev`), kept apart on
purpose so the certified DP profile's dependency fingerprint never pulls in
mutmut/pytest-cov/libcst/textual (see the extra's own comment in
`pyproject.toml`). Installing `.[dev]` alone (matching this batch's instructed
first try) left `mutmut` missing; `.[dev,mutation]` is the actual dev-install
recipe for this harness.

Verified (all via `.venv/bin/python`, from the repo root):

```
$ .venv/bin/python -c "import decoy_engine, pathlib; print(pathlib.Path(decoy_engine.__file__).resolve())"
/home/cam/vscode/decoy-engine/.claude/worktrees/native-phase3-build/src/decoy_engine/__init__.py

$ .venv/bin/python -c "import importlib.util; print(bool(importlib.util.find_spec('decoy_engine_native')))"
True

$ .venv/bin/python -m mutmut --version
python -m mutmut, version 3.7.0

$ .venv/bin/python -c "import duckdb; print(duckdb.__version__)"
1.5.4

$ .venv/bin/python -m coverage --version
Coverage.py, version 7.16.0 with C extension
```

`git status --short` was empty before and after the install (the extras
install only touches the venv, not the checked-in tree).

**Tool versions differ honestly from the Phase 2 T0 record** (mutmut 3.6.0
there vs. 3.7.0 here): the pin is `mutmut>=3.0` (a floor, not exact), so a
fresh resolve months later picked up a newer release. Recorded as-installed
rather than forced back to match, per the batch's honesty instruction.

| Tool | Phase 2 T0 | This batch (P3-T0) |
|------|-----------:|--------------------:|
| Python | 3.10.20 (shared venv) | 3.13.13 (this worktree's own venv) |
| mutmut | 3.6.0 | 3.7.0 |
| coverage | 7.15.2 | 7.16.0 |
| duckdb | (not used in Phase 2 T0) | 1.5.4 |

The compiled companion `decoy_engine_native` was already present in this
worktree's venv (built for an earlier batch); `find_spec` confirms it resolves
truthy, satisfying the companion-required lane's precondition.

## 2. Two-lane companion split

### 2a. Faker-only lane: `_phase3_eligibility.py`, no compiled companion needed

`_phase3_eligibility.py` (229 LOC) is the cleanest faker-only unit: its
covering test file, `tests/native/test_phase3_eligibility.py` (21 tests, runs
in 0.24s), imports no Rust-backed kernel and carries no
`@_NEEDS_COMPANION` marker.

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_phase3_eligibility.py \
  --tests tests/native/test_phase3_eligibility.py \
  --timeout 30
```

Result: 184 mutants, 154 killed, 30 survived, 0 true-timeout, 0 unresolved.
Full run (mutmut generation + readjudication) completed in 1m21s wall clock.

```
===== CORRECTED MUTATION TALLY =====
module:            ['src/decoy_engine/execution/native/_phase3_eligibility.py']
total mutants:     184
mutmut raw:        {'killed': 154, 'survived': 30}
killed:            154
survived:          30
true-timeout:      0
LOGIC score:       83.70%  (154/184)
```

**One clearly KILLED mutant:**
`decoy_engine.execution.native._phase3_eligibility.x_phase3_c1_eligibility__mutmut_1`,
which replaces `table_cfg = _find_table(config, table)` with `table_cfg =
None`. Every test in the covering file constructs a real table config and
asserts on the returned `Phase3Eligibility`, so forcing the table lookup to
always miss is caught immediately.

**One clearly SURVIVING mutant:**
`decoy_engine.execution.native._phase3_eligibility.x_phase3_c1_eligibility__mutmut_9`,
which replaces `base = native_route_eligibility(config, table=table,
profile=profile)` with `base = native_route_eligibility(config, table=table,
profile=None)` -- the caller-supplied `profile` argument is dropped on the
floor. No test in `test_phase3_eligibility.py` passes a `profile` whose
presence changes the result versus `profile=None`, so this survivor is a real,
demonstrated gap (not equivalent), left for P3-T1 to close since grading is
out of scope here. Both mutants confirm the harness distinguishes killed from
survived on this faker-only unit, which is P3-T0's actual job.

### 2b. Companion-required lane: skip vs. fail under a simulated companion-absent build

The companion-required Phase 3 tests (`tests/native/test_dispatch_faker.py`,
`tests/parity/native/test_c1_faker_parity.py`,
`tests/parity/native/test_phase3_c1_gate.py`) gate on
`_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not
None`, computed once at collection time. Simulating the companion's absence
means making `find_spec` return `None` without a raising `meta_path` finder
(which would propagate an exception out of `find_spec` instead of a clean
`None` and defeat the purpose): `sys.modules["decoy_engine_native"] = None`
does exactly this --

```
$ .venv/bin/python -c "
import sys, importlib.util
sys.modules['decoy_engine_native'] = None
print(importlib.util.find_spec('decoy_engine_native'))
"
None
```

Two runs, same three files, to compare companion-present against
simulated-absent:

**Companion present (baseline):**

```
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/native/test_dispatch_faker.py \
  tests/parity/native/test_c1_faker_parity.py \
  tests/parity/native/test_phase3_c1_gate.py
```
```
51 passed in 137.47s
```

**Companion simulated-absent** (`sys.modules["decoy_engine_native"] = None`
set before pytest collects, via a small driver script that sets it then calls
`pytest.main(...)` in-process):

```
51 passed, 0 skipped -> companion present
29 passed, 22 skipped, 0 failed -> companion simulated-absent
```

Zero failures either way. The 22 tests that skip under simulated-absence are
exactly the companion-gated ones (`@_NEEDS_COMPANION`); the 29 that still run
and pass are the companion-independent tests in `test_dispatch_faker.py` (pool
identity/config-shape logic that never touches the Rust kernel). This is the
proof the plan asks for: a skipped companion-required test reports as SKIP,
never as a false PASS or a covering-line credit, so a companion-absent CI run
cannot be miscounted as having graded the companion-required lane.

## 3. Substrate wrinkle 1: DuckDB filesystem spill, empirical false-timeout check

`_pool_quality.py`'s `measure_pool_quality` opens a real `connect_duckdb`
connection, writes a Parquet spool, and runs the frozen `GROUP BY source`
aggregation for every test in `tests/native/test_pool_quality.py` (35 tests,
0.57s plain `pytest` run) -- every mutant's covering-test run touches this
path, not just tests that target the DuckDB code directly, so the concern is
real: does the per-mutant DuckDB connection open/spill/close overhead
misfire mutmut's in-process timeout budget the way the Phase 2 record found
for pandas/Arrow?

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_pool_quality.py \
  --tests tests/native/test_pool_quality.py \
  --timeout 30
```

```
===== CORRECTED MUTATION TALLY =====
module:            ['src/decoy_engine/execution/native/_pool_quality.py']
total mutants:     223
mutmut raw:        {'survived': 59, 'killed': 164}
killed:            164
survived:          59
true-timeout:      0
LOGIC score:       73.54%  (164/223)
```

**Empirical verdict: no false-timeout at the 30s floor.** `mutmut run`'s own
progress counters showed zero timeouts (`⏰ 0`) across all 223 mutants, and
`tq_mutate.py`'s readjudication of the 59 survived mutants confirmed the same
tally standalone (`survived -> survived` for all 59, ~2.2-2.4s each). Unlike
the pandas/Arrow substrate the Phase 2 record found misfiring, DuckDB's
connection-open + spool-write + query + close cycle for this module's small
test fixtures (tens of rows) is fast enough (well under a second per test)
that it never approaches the 30s per-mutant budget.

**Known-semantic threshold mutant, specifically checked:** mutmut generated a
comparison-operator flip at both threshold checks in `enforce_pool_quality`
(`>` to `>=`):

- `x_enforce_pool_quality__mutmut_95`: `if measurement.collision_rate >=
  collision_threshold:` (was `>`)
- `x_enforce_pool_quality__mutmut_121`: `if measurement.pool_duplicate_rate >=
  duplicate_threshold:` (was `>`)

Both were KILLED (`mutmut run`'s own raw exit code 1, not a timeout, not sent
to readjudication), meaning an existing test in `test_pool_quality.py` already
exercises a pool sitting exactly at a frozen threshold and asserts it is
admitted, not rejected -- the boundary the plan's "strict `>` not `>=`" gap
item names is caught on the first empirical check, at least for these two
mutants (P3-T2 still owns the full boundary-mutant sweep under the
zero-unadjudicated-survivor bar; this batch only had to prove killed-vs-
survived distinguishes, not grade the module).

**Chosen timeout floor: 30s**, unchanged from the Phase 2 default. No raised
floor was needed: the empirical run above completed with zero timeouts at
this value, so P3-T2 can start from 30s rather than a DuckDB-specific
override; if a future full-module run at wider test selection does misfire,
that is a P3-T2 finding, not a P3-T0 one.

### Forcing and correcting the false timeout

`_pool_quality.py` is fast enough that, like `_kernels_scalar.py` in the
Phase 2 record, it does not spontaneously trigger mutmut's in-process
misfire. `--force-false-timeout` reproduces the pathology on demand:

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_pool_quality.py \
  --tests tests/native/test_pool_quality.py \
  --timeout 30 --force-false-timeout --clean-mutants-dir
```

```
===== CORRECTED MUTATION TALLY =====
module:            ['src/decoy_engine/execution/native/_pool_quality.py']
total mutants:     223
mutmut raw:        {'timeout': 223}
killed:            164
survived:          59
true-timeout:      0
LOGIC score:       73.54%  (164/223)
```

`mutmut run`'s own raw pass reports all 223 mutants as `timeout`, a complete
misfire. `tq_mutate.py`'s readjudication re-runs every one of them standalone
in a fresh subprocess and recovers the EXACT same tally as the honest
default-timeout run above (164 killed, 59 survived, 0 true-timeout) -- proof
the forced timeouts were false, not real: a genuinely slow or hanging mutant
would not resolve to the identical numbers on a fresh standalone rerun.

## 4. Substrate wrinkle 2: `_route_diagnostics` process model

The plan's concern (section 3, item 2) is precise: the collector's
length-prefix isolation is a TEST-CONSTRUCTION precondition (construct
`RouteDiagnostics` before the invocation whose warnings it isolates), and a
serial-mutation requirement is real ONLY IF the actual runner shares a
process/cache across mutants. Two separate questions, both settled here.

### 4a. Test-construction precondition: honored in every existing test

Read `tests/native/test_c1_diagnostics.py` and
`tests/native/test_c1_bounded_state.py` directly (not inferred):

- `test_isolation_excludes_prior_invocation_warnings` (lines 60-73): a prior
  invocation's warning is put at line 63, `RouteDiagnostics(cache)` constructs
  at line 66 (baseline snapshot AFTER the prior warning), the invocation
  this test isolates puts at line 67 (AFTER construction).
- `test_diagnostics_constructed_before_any_prior_warning_sees_everything_after`
  (lines 76-85): constructs at line 81, puts at lines 82-83, both after.
- `test_repeated_identical_warning_dedupes_to_one_first_emission_order`
  (lines 93-107): constructs at line 95, puts at lines 97/98/103, all after.
- `test_same_input_same_order_deterministic` (lines 140-149): constructs at
  line 143, puts at lines 144-146, all after.
- `test_attribution_fans_out_across_columns_sharing_one_provider`
  (lines 157-177): constructs at line 163, puts at line 164, after.
- `test_multi_table_attribution_and_cross_invocation_isolation`
  (lines 180-212): `diag_patients` constructs at line 186, puts at 187-188
  (after); `diag_observations` constructs at line 206 -- AFTER
  `diag_patients`'s reads at line 193, per the file's own comment ("mirroring
  the real coordinator, which constructs one RouteDiagnostics per table right
  before dispatching it and reads it right after that table's chunk stream
  drains, before the next table's invocation starts") -- puts at line 207
  (after).
- `test_c1_bounded_state.py`'s `_run` helper (lines 79-97): constructs
  `RouteDiagnostics(cache)` at line 84 with an explicit comment ("Constructed
  BEFORE dispatch: the collector's isolation baseline must predate this
  invocation's own pool build"), then calls `run_native_or_oracle_chunked`
  (the invocation it isolates) at line 87.
- `test_collector_view_stays_bounded_under_same_identity_rebuild_churn`
  (lines 230-252): constructs at line 244, the 50-iteration rebuild-churn
  loop starts at line 245 (after).

Every existing test constructs its `RouteDiagnostics` before the puts/
invocation it isolates. No test interleaves two invocations sharing one cache
inside a single test in violation of the precondition.

### 4b. Does the runner share a process/cache across mutants? No.

Read `mutmut`'s own mutation loop
(`.venv/lib/python3.13/site-packages/mutmut/__main__.py`, around line 1472):
for every mutant, the parent process calls `pid = os.fork()`; the child sets
`MUTANT_UNDER_TEST`, runs that one mutant's covering tests via
`runner.run_tests(...)`, and calls `os._exit(result)`. The parent never runs
tests itself -- it only forks, waits, and reads exit codes
(`read_one_child_exit_status`). So each mutant's test run happens in its own
freshly forked OS process, forked from the same still-pristine parent every
time (never from a previous mutant's already-exited child), and any
module-level singleton state (a `PoolCache`, the process-global
`get_default_pool_cache()`) starts from whatever the PARENT held at fork
time -- which never ran a mutant's tests -- so no warning state carries over
from one mutant's child into another's.

`tq_mutate.py`'s standalone readjudication is even more strongly isolated:
`readjudicate()` calls `_run_selection()`, which calls `subprocess.run(cmd,
...)` -- a brand-new Python interpreter subprocess per mutant, not a fork,
confirmed at `scripts/tq_mutate.py` lines 282-308.

**Determination: no serial-mutation requirement is created by this runner.**
Both halves of the pilot (`mutmut run`'s per-mutant fork, `tq_mutate.py`'s
per-mutant fresh subprocess) already give every mutant its own process, so a
`PoolCache`/`RouteDiagnostics` pair never lives across two mutants. Within one
mutant's single forked child, every test in the covering file's session runs
in that one process/interpreter, in test-file order -- but section 4a already
confirmed every existing test constructs its own local `PoolCache` (never the
process-global `get_default_pool_cache()`) and honors the construct-before-
invocation precondition, so ordinary in-session test ordering does not
violate it either. Per the plan's own instruction, this batch does NOT assert
a serial-worker requirement the runner's process model does not create: none
is pinned here, and P3-T3 (which grades `_route_diagnostics.py` for real)
should run it with the default parallel mutmut runner, not a forced
`-j 1`/serial mode.

## 5. Resource discipline observed

Every run was scoped to one module via `only_mutate` plus a focused
`--tests` selection (never a full-suite run). `free -g`/`free -m` checked
before, during (via a background monitor polling every 8-15s), and after each
heavy run; available memory never dropped below ~9.7 GB out of 12 GB across
the `_phase3_eligibility.py` run (184 mutants, ~1m21s), the `_pool_quality.py`
default-timeout run (223 mutants), and the `_pool_quality.py`
`--force-false-timeout` run (223 mutants, all readjudicated standalone).
`isolated_execution_enabled` was never touched; nothing in either module's
covering tests spawns the engine's `_isolated_worker` subprocess path.
`mutants/` was deleted between runs (`rm -rf mutants` / `--clean-mutants-dir`)
to bound disk use, not left to accumulate.

## 6. Acceptance against the plan's P3-T0 gate

> Gate P3-T0 on: both lanes demonstrably distinguish killed from surviving;
> the DuckDB timeout (if any) is reproduced and classified; the
> `_route_diagnostics` process model is established (serial requirement
> asserted only if real); all with recorded commands.

- Faker-only lane killed vs. survived: `_phase3_eligibility.py` pilot (154
  killed, 30 survived, named examples above). Met.
- Companion-required lane skip-not-fail: simulated-absent run, 29 passed / 22
  skipped / 0 failed vs. companion-present 51 passed / 0 skipped. Met.
- DuckDB false-timeout: empirically checked, NOT reproduced at the default
  30s floor (0 true-timeouts across 223 mutants, including both boundary
  `>`-to-`>=` threshold mutants, both killed); the pathology was then forced
  and corrected on demand via `--force-false-timeout`, matching the honest
  tally exactly. Met.
- `_route_diagnostics` process model: established as fully process-isolated
  per mutant (mutmut's `os.fork()` loop, `tq_mutate.py`'s `subprocess.run`);
  no serial-mutation requirement asserted, because none is created. Met.
- No production code changed; `pyproject.toml` restored clean after every
  run (verified `git status --short pyproject.toml` empty throughout).

## 7. Carried forward for P3-T1 onward

- **`x_phase3_c1_eligibility__mutmut_9`'s dropped-`profile`-argument
  survivor** is a real gap (`native_route_eligibility(..., profile=profile)`
  mutated to `profile=None` survives every existing test): P3-T1 should add a
  test where a non-None `profile` changes the admitted verdict versus
  `profile=None`, rather than re-discover this finding.
- **The other 29 survivors on `_phase3_eligibility.py`** and **58 (of 59) on
  `_pool_quality.py`** are NOT adjudicated here (out of scope for a harness
  batch); P3-T1/P3-T2 grade them against the plan's candidate-gap lists
  (section 4) rather than this record.
- **DuckDB timeout floor: keep 30s as the P3-T2 starting point.** No evidence
  here requires raising it; if P3-T2's wider test selection (the full
  `test_pool_quality.py` suite plus any new property tests) does misfire,
  that is a P3-T2 finding to raise the floor for, with its own empirical
  proof, not an assumption carried from this batch.
- **No serial-mutation flag for `_route_diagnostics.py`/`_cache.py` in
  P3-T3.** The process model established in section 4b applies to that
  module's mutation run too (same runner); do not add `-j 1` or any
  serial-worker override without a new empirical reason specific to that
  batch.
- No blocker found for P3-T1 onward: the venv precondition, the two-lane
  split, and both substrate wrinkles are settled with recorded commands
  above.

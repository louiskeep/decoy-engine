Status: record

# P3-T2 pool-quality measurement + enforcement: zero-unadjudicated-survivor gate

- **Plan:** `docs/plans/2026-08-30-phase3-c1-test-plan.md`, batch P3-T2 (section 4) and the
  method in section 3 (binding by reference from `docs/plans/2026-08-29-native-efficiency-test-
  plan.md`).
- **Scope:** `src/decoy_engine/execution/native/_pool_quality.py` (397 LOC before this batch):
  `measure_pool_quality` (DuckDB spill-backed collision measurement), `enforce_pool_quality`
  (per-column threshold check), `PoolQualityError`. Existing tests:
  `tests/native/test_pool_quality.py` (35 tests from Task 3.2's build).
- **Branch:** `feat/native-phase3`, worktree `.claude/worktrees/native-phase3-build`.
- **Harness reused:** `scripts/native-testing/python_mutation_pilot.py` (P3-T0), unchanged.
  Faker-only lane: no compiled-companion dependency.
- **Bar:** zero unadjudicated semantic survivors on the measurement arithmetic, the tuple-aware
  threshold selection, and every enforcement branch (crypto/RI-tier).

## 1. SQL-drift adjudication (done first, per the plan's instruction)

The frozen baseline (`PHASE3-C1-BASELINE.md`, "Bounded aggregation") filters the collision
population on `WHERE source IS NOT NULL` only. Production `_COLLISION_SQL` had drifted to
`WHERE source IS NOT NULL AND masked IS NOT NULL`.

**Verdict: DIVERGENT. Restored the frozen `source IS NOT NULL`-only SQL.**

The extra `masked IS NOT NULL` filter is fail-open in direction: dropping a non-null-source/
null-masked row shrinks the `distinct_sources` denominator, which can turn a real collision
population into a smaller-or-empty pass. The plan asks to PROVE the filter is behaviorally inert
before leaving it, or restore the frozen SQL. It is NOT provably inert:

- The deterministic sampler (`generation/pool/_sampler.py::PoolSampler._deterministic`) only
  emits a null output at a position where the SOURCE is null (`if is_null_arr[i]: continue`);
  every non-null-source position is assigned `pool_values[idx]`, a real pool value. So for THAT
  one code path, a non-null source never produces a null masked output today.
- But that observation does not make the filter inert for `measure_pool_quality` as a GENERIC
  primitive. The function accepts arbitrary `source`/`masked` Arrow arrays from any caller; its
  own docstring anticipates "a null `source` (or null `masked`) entry" as a real input shape, not
  a theoretical one. No code-level invariant anywhere in the pool-build path
  (`generation/pool/_builder.py::PoolBuilder.build`, `_value_pool.py::ValuePool`) guarantees a
  built pool's values array can never contain a null entry -- a custom or future provider is not
  ruled out by any assertion. Proving inertness would require proving that invariant, which does
  not exist.
- Given the leak-gate stakes and the standing rule that a later contributor does not weaken a
  frozen acceptance definition, the conservative choice is the frozen one: restore
  `WHERE source IS NOT NULL` only, so a non-null-source/null-masked row is counted (not silently
  dropped) and correctly reported as a collision against nothing (see `TestSqlDriftRestoredFrozenFilter`
  in the test file, which pins this and was verified to FAIL against the drifted SQL by
  temporarily reverting the fix and re-running).

**Verified empirically, not assumed:** `ANY_VALUE(masked)` in this repo's pinned DuckDB (1.5.4)
returns the source's single non-null masked value when at least one non-null value exists in the
group, and NULL only when every row for that source has a null masked value (checked directly
against a live connection, both mixed-null and all-null-per-source cases). So a source with a
null-masked row is never silently lost under the restored SQL; it either resolves to its real
output or, if every occurrence is null-masked, is counted as a collision (correctly conservative,
never a silent pass).

**Escalation to Cam:** not needed. The restore is the plan's own default action when inertness
cannot be proven, and no re-freeze of the baseline population is being proposed here -- if Cam
later wants to formally re-scope the population definition, that is a separate decision, not
implied by this fix.

## 2. Tuple-aware threshold guard (BLOCKER, fixed)

**Was the code fail-open?** Yes. `enforce_pool_quality` selected `COLLISION_RATE_THRESHOLD[column]`
/ `POOL_DUPLICATE_RATE_THRESHOLD[column]` by column name alone. The frozen thresholds
(`PHASE3-C1-BASELINE.md`) were measured at ONE calibration point per column: pool_size 10,000 for
every column, and distinct_sources 1,000 (FIRST) / 1,200 (LAST) / 360 (MAIDEN) -- fixed by the
frozen recipe's config and its synthetic data generator, not derived from row count. Nothing
checked that a measurement's `pool_size`/`distinct_sources` actually matched that calibration
point before applying the threshold. A pool with a different pool_size or distinct-source
population has a different collision floor (a larger pool_size lowers it; more distinct sources
raise it), so applying the frozen threshold to it silently compares a real rate against a
threshold that was never validated for that population -- a non-adjudicable leak-gate survivor.

**Fix:** added `FROZEN_POOL_SIZE` and `FROZEN_DISTINCT_SOURCES` (the same per-column calibration
point the thresholds were measured at) and two new fail-closed checks in `enforce_pool_quality`,
placed after the existing measurement-integrity checks (non_deterministic_sources, finite-range)
and before the two threshold comparisons:

- `measurement.pool_size != FROZEN_POOL_SIZE[column]` always raises (`metric="pool_size"`) --
  the pool is unconditionally real regardless of the source column's null-ness, so this check is
  NOT exempted by an empty population.
- `measurement.distinct_sources != 0 and measurement.distinct_sources != FROZEN_DISTINCT_SOURCES[column]`
  raises (`metric="distinct_sources"`). `distinct_sources == 0` is exempt: that is the separately
  frozen empty-population pass (rate 0, always -- Task 3.0 Step 4), not a tier this guard governs.

Both accept (matching tier) and reject (mismatched tier, both directions on pool_size, and the
distinct_sources mismatch) are tested in `TestTupleAwareThresholdGuard`, including a regression
pin (`test_unsupported_tier_gate_test_recipe_stays_at_the_calibrated_tuple`) that the frozen C1
gate's own synthetic recipe (`tests/parity/native/test_phase3_c1_gate.py`) still lands on the
calibrated tuple at both its parity (10,000-row) and moderate (250,000-row) tiers -- verified by
actually running both parametrizations of `test_criterion3_pool_quality_passes_frozen_thresholds`
against the fixed code (both pass; see section 6).

## 3. Every unit's five-field result

Final mutation run: `python scripts/native-testing/python_mutation_pilot.py --module
src/decoy_engine/execution/native/_pool_quality.py --tests tests/native/test_pool_quality.py
--timeout 30 --readjudicate-killed`.

```
===== CORRECTED MUTATION TALLY =====
module:            ['src/decoy_engine/execution/native/_pool_quality.py']
total mutants:     262
mutmut raw:        {'killed': 259, 'survived': 3}
killed:            259
survived:          3
true-timeout:      0
LOGIC score:       98.85%  (259/262)
```

| Field | Count |
|---|---:|
| (a) Branch coverage | 100% (90 stmts, 26 branches, `coverage run --branch`) |
| (b) Killed | 259 |
| (c) Equivalent (reason below) | 3 |
| (d) Unreachable-by-contract | 0 |
| (e) Tool-excluded | 0 |

**Zero unadjudicated semantic survivors.** All 3 survivors are adjudicated equivalent below, each
with an empirical proof, not an assumption.

### Equivalent mutants (3)

- **`x_measure_pool_quality__mutmut_14` / `_16`** -- the Parquet spool's column name in
  `pa.table({"source": source, "masked": masked})` is uppercased (`"SOURCE"` / `"MASKED"`) in the
  written table, while `_COLLISION_SQL` still reads `source` / `masked` in lowercase. Verified
  directly against this repo's pinned DuckDB (1.5.4): a Parquet file written with a column
  literally named `SOURCE` still resolves correctly against a lowercase `source` reference in
  `read_parquet(...)` (SQL identifier matching is case-insensitive). Reproduced with a standalone
  script; both mutants are behaviorally identical to the original for every input.
- **`x_measure_pool_quality__mutmut_45`** -- `collision_rate = float("nan")` mutated to
  `float("NAN")`. Verified directly: Python's `float()` parser is case-insensitive for the `nan`/
  `inf` literals, and `struct.pack("d", float("nan")) == struct.pack("d", float("NAN"))` (bit-
  identical). No test can ever distinguish these.

### Killed: representative examples (259 total, see the pilot's own report for the full list)

- Both `>`-to-`>=` boundary mutants on the two threshold comparisons (`collision_rate` and
  `pool_duplicate_rate`) are killed by `TestThresholdBoundary` (measurement exactly at threshold
  must pass) and the new `test_rate_exactly_one_is_a_valid_finite_rate_not_an_integrity_failure`
  /`test_finite_out_of_range_rate_is_caught_by_the_integrity_check` pair, which additionally kills
  a mutant flipping the finite-range check's `or` to `and` (a NaN-only test cannot distinguish
  those two operators, since NaN satisfies both `not isfinite` and `not in-range` simultaneously;
  only a FINITE out-of-range value like 1.5 tells them apart).
- Every `column=None` / `observed=None` / `threshold=None` kwarg-drop mutant across all nine raise
  sites in `enforce_pool_quality`, plus the `PoolQualityError.__init__` mutant that forces
  `self.column` to always be `None` regardless of the constructor argument, are killed by adding
  `.column`/`.observed`/`.threshold` assertions to the corresponding test for each raise site (no
  existing test asserted `.column` anywhere before this batch, which is why the `__init__` mutant
  had survived).
- A mutant changing the non_deterministic_sources error's `threshold=0` to `threshold=1` (loosening
  the "any nonzero count fails" contract to "any count above 1 fails") is killed by asserting
  `.threshold == 0` explicitly.
- The `_breach_message` mutant that changes `message += ...` to `message = ...` (discarding the
  "pool_quality breach on column ..." prefix whenever a warning is present) is killed by asserting
  the prefix survives alongside the warning codes, not just the codes alone.
- The `_breach_message` separator mutant (`", ".join` -> a filler-wrapped separator) needed a
  DIFFERENT test: a single warning code cannot exercise a join of two items, so
  `test_multiple_warning_codes_are_comma_joined_in_the_breach_message` uses two distinct codes and
  asserts the exact joined substring.
- Every message-text-only mutant that wraps ONE string-literal fragment in filler text (mutmut's
  `"XX...XX"` convention) or upper-cases it survived an initial loose `phrase in message` check,
  because the mutated fragment's own INTERIOR substring is untouched by a prefix/suffix wrap --
  only the fragment BOUNDARY changes. Each of these (~30 mutants across the obligation, column,
  measurement-mismatch, non-determinism, finite-range, pool_size-guard, and distinct_sources-guard
  messages) was killed by rewriting the assertion to straddle the actual fragment join (e.g.
  `"1 source value(s) mapped to more than one masked output"` instead of the looser `"mapped to
  more than one masked output"`), which a wrap or case change at either edge of the fragment
  necessarily breaks.

## 4. Differential collision measurement (no self-grading)

`TestDifferentialCollisionMeasurement.test_collision_measurement_matches_raw_pure_python_reference`
is a Hypothesis property test (40 examples) generating random `(source, masked)` pairs over a
5-symbol source alphabet and a 4-symbol masked alphabet (both including `None`), and comparing
`measure_pool_quality`'s real DuckDB-backed result against `_reference_collision_measurement`, a
pure-Python function operating ONLY on the raw pairs list -- it never reads `PoolQualityMeasurement`,
a production threshold constant, or `ValuePool.distinct_count`. The reference mirrors the frozen
`source IS NOT NULL`-only population and the empirically-verified `ANY_VALUE` semantics (a
source's `out_val` is its one distinct non-null masked value if exactly one exists, NULL if every
occurrence is null-masked, and arbitrary -- never compared -- if more than one distinct non-null
value exists, since that case forces `collision_rate` to NaN on both sides regardless of which
arbitrary value DuckDB picked). Covers duplicate sources, nondeterministic mappings, nulls in
both source and masked, and empty inputs in one sweep.

`TestPoolDuplicateRateRawCounting.test_pool_duplicate_rate_matches_raw_distinct_count` is a
second property test (30 examples) building a `ValuePool` whose `distinct_count` is independently
computed as `len(set(raw_values))` from the SAME raw values array the pool holds (never read back
from the pool itself), and asserting `measurement.pool_duplicate_rate` matches the formula applied
to that independently-derived count. A separate test covers `pool.size == 0` (rate 0, not a
division error).

Every property test was confirmed able to fail: the SQL-drift regression tests
(`TestSqlDriftRestoredFrozenFilter`) were run against a temporarily-reverted (drifted) SQL and
failed exactly as predicted (see section 6); the concurrency test likewise failed against a
temporarily-reverted fixed-filename spool path.

## 5. Exceptional-cleanup fault injection + fixed-filename resolution

`measure_pool_quality`'s own `finally` unlinks the spool BEFORE the function returns, so "enforce
raises" never exercises this cleanup -- the real seams are `pq.write_table`, `conn.execute`/
`fetchone`, and `conn.close`, all INSIDE `measure_pool_quality`. `TestExceptionalCleanupSeams`
injects a fault at each, using a `_CloseTrackingConnection` wrapper around a REAL DuckDB connection
(so the parts of the call not under fault still behave normally):

- **`pq.write_table` raises:** spool never written; connection still closed; the raised exception
  propagates.
- **`conn.execute` raises** (after a real write): the spool WAS written for real, proving unlink
  had something to clean up (not a no-op on an absent file); it is gone afterward, and the
  connection is closed.
- **`conn.close` raises** (after a real, successful write+execute+fetchone): the unlink runs in the
  inner `try`, BEFORE `conn.close()` in the inner `finally`, so the spool is already gone by the
  time close() itself raises; that raise propagates on its own.

**Fixed-filename resolution:** the spool path was `pairs_{column}.parquet`, a name shared by every
call for a given column -- two overlapping measurements of the same column could read/unlink each
other's in-flight file. Fixed with a per-call random suffix (`pairs_{column}_{uuid4().hex}.parquet`),
the minimal change (unique path, not an exclusivity contract, since nothing in this standalone
module's contract prevents concurrent calls). Tested two ways:

- `test_sequential_measurements_of_same_column_use_distinct_spool_paths`: two back-to-back calls
  use different paths.
- `test_overlapping_measurements_of_same_column_do_not_clobber_each_other`: two REAL threads, one
  paused mid-write (via a hook on `pq.write_table`) until the other fully completes a measurement
  of the same column; the paused one then resumes and must still read its OWN data. Verified to
  FAIL against the pre-fix fixed filename by temporarily reverting the `uuid4()` suffix and
  re-running just this test: it raised a real DuckDB `IOException` ("No files found that match the
  pattern ... pairs_FIRST.parquet") because thread B's `unlink` removed thread A's in-flight file
  before A could read it. Confirmed the fix and the test both matter, not just one.

## 6. Reproducible commands

```
# Full test suite for this module
.venv/bin/python -m pytest -q tests/native/test_pool_quality.py

# Branch coverage
.venv/bin/python -m coverage run --branch -m pytest -q tests/native/test_pool_quality.py
.venv/bin/python -m coverage report --include=*/_pool_quality.py -m

# Mutation pilot (zero-unadjudicated-survivor bar; foreground, ~3-4 min)
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_pool_quality.py \
  --tests tests/native/test_pool_quality.py \
  --timeout 30 --readjudicate-killed

# Frozen C1 gate regression check (both tiers; confirms the tuple guard does not
# reject the gate's own frozen recipe)
.venv/bin/python -m pytest -q \
  "tests/parity/native/test_phase3_c1_gate.py::test_criterion3_pool_quality_passes_frozen_thresholds"

# ruff
.venv/bin/python -m ruff check src/decoy_engine/execution/native/_pool_quality.py tests/native/test_pool_quality.py
.venv/bin/python -m ruff format src/decoy_engine/execution/native/_pool_quality.py tests/native/test_pool_quality.py
```

mypy on `_pool_quality.py` aborts on a numpy 2.5.0 stub syntax error (`Type statement is only
supported in Python 3.12 and greater`), a known carry-forward in this worktree, not introduced or
fixed here.

## 7. Acceptance against the plan's P3-T2 gate

- Zero unadjudicated semantic survivors: met (259 killed, 3 adjudicated equivalent with empirical
  proof, 0 unreachable, 0 tool-excluded).
- SQL drift adjudicated in writing, restored to the frozen filter, not escalated (no re-freeze
  proposed): met.
- Tuple-aware threshold fail-closed, both accept and reject sides tested, code fixed: met.
- Strict `>` not `>=` on both thresholds, plus the `or`/`and` finite-range distinction: met.
- Exceptional cleanup at the real seams (`pq.write_table`, `conn.execute`/`fetchone`,
  `conn.close`), fixed-filename collision resolved and tested both ways: met.
- `connect_duckdb` used directly, its `memory_limit`/`temp_dir` arguments proven passed through
  from `measure_pool_quality` (`TestConnectDuckdbDirectUsage`), and the threads/`0o700` clamp
  itself already covered by `TestBoundedAggregation` (pre-existing, unaffected by this batch): met.
- Differential grading against a raw-input pure-Python reference for the collision arithmetic and
  the pool-duplicate-rate arithmetic, never against `PoolQualityMeasurement`/production constants/
  `ValuePool.distinct_count`: met.
- Every property test demonstrably able to fail: the SQL-drift and concurrency tests were both run
  against the pre-fix code and failed as predicted; documented above.

Status: record

# T6 end-to-end gate hardening + 100M-row certification

- **Plan:** `docs/plans/2026-08-29-native-efficiency-test-plan.md`, batch T6 (section 4) and the
  method in section 3. T6 is deliberately right-sized: NOT a mutation-adjudication batch like
  T1-T5 (there is no five-field denominator here), but three scoped pieces plus a determinism
  re-check.
- **Branch:** `docs/native-efficiency-test-plan`, worktree `.claude/worktrees/native-test-plan`.
- **Reused, not re-derived:** `tests/parity/native/test_phase2_gate.py` (the frozen W2
  correctness/route/perf gate and its fixtures: `_COLUMNS`, `_build_source`, `_build_config`,
  `_run_native`, `_run_oracle`, `_assert_gate_parity`), `scripts/native-baseline/bench_driver.py`
  + `bench_worker_native.py` + `build_w2_parquet.py` (the honest out-of-band-generation +
  lazy-`ParquetFile.iter_batches` harness proven in Task 2.7), and the frozen targets in
  `docs/plans/PHASE2-BASELINE.md`.

## 1. Small exhaustive correctness/property test

`test_phase2_gate.py`'s `test_native_route_exact_parity_vs_oracle` already carries the
batch-size/order space (3 sizes x 2 orders) over an all-{passthrough,redact,truncate,hash}
admitted 11-column table; this batch does not re-explode that space. The genuinely missing
end-to-end angle: does the WHOLE dispatch-vs-oracle pipeline (not a single kernel, T3's scope)
still match exactly when nulls are scattered at ARBITRARY row positions across every nullable
admitted column at once, at chunk boundaries that vary? The gate already pins the two shapes at
the ends of that space (a whole-column all-null, and no nulls at all); the middle was untested.

Added: `tests/parity/native/test_e2e_certification.py::test_native_route_matches_oracle_with_scattered_nulls`,
a Hypothesis property (`max_examples=15`, `deadline=None`) over `n_rows` (1-23), a null-placement
seed (each row independently nulled with probability 0.3 across `h_email`, `h_token`, `rd_ssn`,
`rd_notes`, `tr_phone`, `tr_card`), and `batch_size` (1, 5, 11). Each generated example builds the
gate's own row-shaped source with that null mask applied, runs it through both
`run_native_or_oracle_chunked` and the pinned pandas oracle, and asserts exact logical parity via
the gate's own `_assert_gate_parity`.

Non-vacuous: `derive_array`'s null-in/null-out contract (`decoy-engine-native/src/batch.rs`) means
a scattered-null row is a real, distinct code path from the two example shapes already pinned; a
regression that mishandled a null NOT at a whole-column boundary (e.g. an off-by-one at a chunk
edge landing on a null row) would have no other test to catch it before this one.

```
pytest tests/parity/native/test_e2e_certification.py::test_native_route_matches_oracle_with_scattered_nulls -q
```

Result: 1 passed (15 Hypothesis examples).

## 2. ONE medium end-to-end mixed-route case

Rather than add a second, near-duplicate mixed-route test, this batch closed the evidence gap in
the ONE representative case the gate already carries:
`test_mixed_admitted_and_non_admitted_columns_stays_fully_on_oracle`
(`tests/parity/native/test_phase2_gate.py`). The table has 3 hash + 3 passthrough + 2 redact + 3
truncate columns (incl. the legacy from_end variant; every one individually admitted, each with
its own compiled kernel) plus one
`text_redact` column, a strategy with NO compiled native kernel
(`_requirements.NATIVE_KERNEL_STRATEGIES` does not include it). Before this batch the test asserted
only `native_admitted is False` and every node's route tag was `"oracle"` -- proving the ROUTE
decision, but not the two other legs of route-proof named in the plan ("zero native kernels ran",
which job success or a route tag alone does not establish -- the exact Decision-10 trap the test
file's OWN control test names a few lines above).

Added two assertions to the existing test, closing that gap:

```python
assert evidence.kernel_calls == {}
assert evidence.compiled_kernel_executed is False
```

Full route-proof now asserted together: every node tagged `"oracle"`, `kernel_calls == {}` (zero
native kernels ran, for ANY strategy, not just hash), `compiled_kernel_executed is False`, and the
native-route output still matches the oracle exactly (`_assert_gate_parity`) -- proving the
admitted/non-admitted boundary is a WHOLE-TABLE decision: the otherwise-admitted
passthrough/redact/truncate/hash columns reroute too, purely because one sibling column has no
kernel.

(A float-typed hash column was tried first as the representative case, per the plan's own example
list -- but the pandas oracle itself rejects float canonicalization for `hash`
[`GenerationError: float_canonicalization_unsupported`], so that config never reaches a state
where BOTH routes succeed and can be compared. `text_redact` was kept as the representative case
instead: it is a real strategy with no native kernel, and both routes succeed on it.)

```
pytest tests/parity/native/test_phase2_gate.py::test_mixed_admitted_and_non_admitted_columns_stays_fully_on_oracle -q
```

Result: 1 passed.

## 3. The 100M-row certification

Ran the NATIVE route over the frozen W2 workload (`scripts/native-baseline/bench_worker_native.py`,
the exact 10-column shape in `docs/plans/PHASE2-BASELINE.md`: 3 keyed-hash, 3 passthrough, 2
redact, 2 truncate; fixed seed `20260828`; fixed 32-byte mask key) at EXACTLY 100,000,000 rows.

### Harness (unchanged in spirit, one infrastructure fix disclosed below)

- `scripts/native-baseline/build_w2_parquet.py` builds the 100M-row Parquet file in a SEPARATE,
  non-timed, non-RSS-sampled process, 50,000 rows per row group, dropping each batch after it
  writes -- generation itself never holds more than one batch in memory.
- `scripts/native-baseline/bench_worker_native.py` runs in a FRESH OS process, reads the file
  LAZILY via `ParquetFile.iter_batches(batch_size=50_000)`, and drops each masked output batch
  immediately after counting its rows -- never accumulating the 100M-row output.
- `scripts/native-baseline/bench_driver.py` spawns that fresh process under external `VmHWM`
  sampling (`/proc/<pid>/status`, polled every 20ms, never read from inside the measured process)
  and reports wall time from the worker's own `BENCH_JSON` payload.

Command:

```
python scripts/native-baseline/bench_driver.py \
  --worker bench_worker_native.py --prebuild build_w2_parquet.py \
  --batch-rows 50000 --tiers 100000000 --reps 1 --warmup 0 \
  --source-dir <scratch dir> \
  --out scripts/native-baseline/results_native_100m.json
```

One rep, no discarded warmup: the 1x/4x/16x runs (`results_native_1x_4x.json`,
`results_native_16x.json`) already established sub-second-scale variance (IQR 0.05-4.4s across
5/5/3 reps) and a linear wall/flat-RSS fit up to 16M rows; a calibration run at 5,000,000 rows
during this batch (78.43s wall, 332.1MB peak RSS -- both on the same linear/flat trend) reconfirmed
it immediately before the 100M run. Re-reading the file cold (no warmup) is also the more honest
figure for a genuinely fresh 100M-row Parquet file, since a real deployment reads it once.

### Infrastructure fix (disclosed, test-harness only): worktree self-location

`bench_driver.py` hardcoded `ENGINE = Path("/home/cam/vscode/decoy-engine")` and derived `VENV_PY`
from it, then spawned the worker with `cwd=str(ENGINE)` and no explicit `PYTHONPATH`. The shared
venv's `decoy_engine` is an editable install pointing at whichever worktree last built it (a
different worktree, `native-phase2-task2.2`, at the time this batch started) -- so every worker
subprocess silently imported THAT worktree's source, not this branch's. Since this branch changes
only tests/scripts (no `src/decoy_engine` change), the measured numbers are unaffected either way,
but running the cert against a different worktree's code than the one under test defeats the
point of a certification.

Fix: `bench_driver.py` now self-locates `ENGINE` from its own file path (`HERE.parent.parent`, i.e.
the worktree containing the script) instead of a hardcoded absolute path, and explicitly threads
`PYTHONPATH=<that worktree>/src` into both the prebuild and worker subprocess environments. `VENV_PY`
stays pointed at the one shared venv (as the repo's own convention documents). Behavior-preserving:
a run from the base checkout resolves to the identical path as before; a run from any worktree now
measures that worktree's own code, which is the whole point of running a benchmark script that
lives in git.

### Results

Raw reading: `scripts/native-baseline/results_native_100m.json`.

| Criterion | Target | Measured | Verdict |
|---|---|---|---|
| (a) Sampled parity | Byte-identical native vs. oracle on a sampled slice | `test_100m_certified_workload_sampled_slice_matches_oracle` passed: 6,000 rows (head + midpoint + tail of the 100M range) byte-identical | **PASS** |
| (b) Route-proof | Every admitted node routed native, compiled kernel executed | Full run: `native_admitted=true`, `reroute_reason=null`, `compiled_kernel_executed=true`, `hash_ms=1,218,963.8` across all 3 hash columns (`hash_cols=3`), `redact_ms=144,941.0`, `truncate_ms=184,446.5`, `passthrough_ms=206.0` -- every strategy actually ran a kernel, not just hash. Per-node route tags for the identical 10-column W2 shape are asserted directly in the sampled-slice test (`evidence.node_routes` all `"native_kernel"`, `kernel_calls["hash"] > 0`) | **PASS** |
| (c) Wall time | Well under the oracle's ~4,228s 100M extrapolation (42.2s/1M-row fit + 8.6s fixed, from `PHASE2-BASELINE.md`) | **1,563.39s** median (1 rep) -- 0.370x the oracle extrapolation (37%), and under the 0.60x ratio (2,536.8s) the 16x gate held native to | **PASS** |
| (d) Peak RSS + flatness | <=6.5 GiB (6,656 MB) AND <=1.5x the 1x value (1x = 315.9 MB -> bound 473.85 MB) | **398.4 MB** peak RSS (external `VmHWM`) -- 6.0% of the 6.5 GiB cap, and 1.261x the 1x reading (well inside the 1.5x flatness bound; DOES NOT scale with row count) | **PASS** |

All four T6 100M-certification criteria PASS. No frozen target was weakened or waived.

Full wall/RSS trend across every tier measured this program (1x/4x/16x from Task 2.7, 100M from this
batch), all under the SAME harness:

| Tier | Rows | Wall | Peak RSS | RSS ratio vs. 1x |
|---|---:|---:|---:|---:|
| 1x | 1,000,000 | 16.19s (median, 5 reps) | 315.9 MB | 1.00x |
| 4x | 4,000,000 | 63.92s (median, 5 reps) | 322.8 MB | 1.02x |
| 16x | 16,000,000 | 260.14s (median, 3 reps) | 349.9 MB | 1.11x |
| **100x** | **100,000,000** | **1,563.39s (1 rep)** | **398.4 MB** | **1.26x** |

Wall time is closely linear in rows across all four tiers (100M/1M row ratio 100x, wall ratio
1563.39/16.19 = 96.6x -- a hair sublinear, consistent with the same fixed per-process overhead the
1x/4x/16x fit already showed). Peak RSS grows only 26% from 1x to 100x while the workload grows
100x, confirming the native streaming route holds RSS flat against row count -- the exact
memory-win property Phase 2 exists to prove, now demonstrated at the product's stated conservative
cap (100M rows), not merely up to 16M.

## 4. Determinism sentries

Re-ran the two NAMED Phase 0 golden sentries this batch is scoped to (not an unbounded
"anywhere in the program" claim):

```
pytest tests/native/test_determinism_goldens.py -q
# 20 passed
pytest tests/native/test_draw_site_inventory_coverage.py -q
# 12 passed
```

32 passed total, 0 failed. No fingerprint moved: every golden hash/inventory entry these sentries
pin matched its recorded value exactly, both before and after this batch's test/harness additions
(none of which touch `src/decoy_engine` production code -- see the gate section below for the one
production-adjacent change, which is to a benchmark script, not a draw site).

## Gates

- `ruff check` on all touched files: clean.
- `ruff format --check` on all touched files: clean.
- `mypy src/decoy_engine testflight`: clean (428 source files).
- `pytest tests/native/ tests/parity/native/ -q`: 492 passed, 1 skipped (pre-existing: the shared
  KAT fixture file is not generated in this environment), 59 xfailed (pre-existing).

## Files touched

- `tests/parity/native/test_e2e_certification.py` (new): the scattered-null property test and the
  100M-workload sampled-slice parity test.
- `tests/parity/native/test_phase2_gate.py`: two added assertions on the existing mixed-route test
  (no new test; closes the route-proof evidence gap in place).
- `scripts/native-baseline/bench_driver.py`: worktree self-location fix (disclosed above);
  benchmark harness, not production code.
- `scripts/native-baseline/results_native_100m.json` (new): the 100M cert's raw wall/RSS reading.
- This file.

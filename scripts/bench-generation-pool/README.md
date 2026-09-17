# Generation-throughput probe (GP1): harness built, the real sweep is owed

Status: the harness (`bench_worker_gen.py` + `bench_compare_gen.py`) is **built**
and covered by its own fast test suite
(`tests/physical/test_bench_gen_pool_harness.py`). The real 8-tier x 10-type
sweep this harness exists to run has **not been run yet**. That sweep is a
deliberate offline invocation (multi-minute, dozens of subprocess launches),
never a CI step.

## Why this exists

GP2 shipped pool-based Faker generation for ten scalar column types
(`first_name`, `last_name`, `name`, `prefix`, `suffix`, `city`, `state`,
`country`, `job`, `company`) with a placeholder auto-pooling threshold:
`N_THRESHOLD = 50_000` in `src/decoy_engine/generation/_faker_pool.py`. Pool
build pays a fixed ~10k-Faker-call cost regardless of row count, so below some
row count pooling is a net loss, not a win. `50_000` was set past an
*estimated* crossover (a parallelism spike measured ~25-30k), never a
measured one. This harness measures the real number so that estimate can be
replaced with data.

## What is here now

- `bench_worker_gen.py` -- one process, one arm, one (faker_type, n_rows)
  cell. Builds a single-column generate pipeline (`type: faker`), validates
  it (`PipelineConfig.model_validate`), compiles it (`compile_plan`), then
  times only `generate_tables(plan)`. Arm selection is `GEN_POOL_BENCH_FLAG`
  (`off` = `pooled: false`, always per-row; `on` = `pooled` omitted, pools
  whenever `pool_eligible` says yes). Prints one `BENCH_JSON` line: row
  count, faker type, arm, wall time, rows/s, output row count, and the
  OBSERVED `pooled_activated` (not just the arm's intent).
- `bench_compare_gen.py` -- the tiers x types sweep/compare driver. Runs the
  worker as a fresh subprocess per arm per rep, alternating arm order by rep
  parity, and reports the paired-ratio statistics (median, p95, seeded
  bootstrap CI) and peak-RSS per (type, n_rows) cell. Only the exact default
  sweep shape (every default tier, every allowlisted type, >=20 reps, >=3
  warmups, >=10,000 bootstrap resamples) is eligible to print a crossover
  recommendation; anything smaller prints `SMOKE COMPLETE` and withholds it.
- `tests/physical/test_bench_gen_pool_harness.py` -- the fast test suite:
  real worker smoke (both arms), the threshold override taking effect inside
  the child process, the production `pool_eligible` predicate staying
  untouched, the vacuity guard genuinely catching a non-activation, the
  driver's pure statistics, every fail-closed shape, and the smoke label.

## The load-bearing threshold override

Production only auto-pools at `n >= N_THRESHOLD` (currently 50,000). If the
sweep relied on that auto-pooling, every tier below 50,000 would run
per-row in BOTH arms by construction, making a measured "crossover" >= 50,000
no matter what the data says -- the probe would prove nothing.

The worker fixes this with a **worker-local, same-process** override: when
`GEN_POOL_BENCH_THRESHOLD` is set, the worker assigns
`decoy_engine.generation._faker_pool.N_THRESHOLD = int(env)` on the module
object before generation runs. `pool_eligible`'s `n >= N_THRESHOLD` check is
an in-module global lookup, so this reassignment takes effect for that one
process only -- no `src/` change, and no effect on any other process,
including the harness's own tests of the real (untouched) production
predicate. The sweep driver sets this to `1`, so the pooled arm pools at
every measured tier. The off arm's `pooled: false` never consults
`N_THRESHOLD` at all, so the override cannot change its behavior.

Every `BENCH_JSON` record, and the driver's own output, carry
`bench_threshold_override` so a pooled measurement can never be mistaken for
production's real auto-pooling behavior.

## Running the real offline sweep

```
python scripts/bench-generation-pool/bench_compare_gen.py --out gp1_sweep_results.json
```

on a quiet bench node. The default flags already match the full-sweep shape
(tiers 1,000 through 1,000,000, all ten allowlisted types, 20 reps, 3
warmups, 10,000 bootstrap resamples) -- this is a real cost: 8 tiers x 10
types x 23 reps x 2 arms = 3,680 subprocess launches, several of them at up
to 1,000,000 rows. Expect it to run for a while. A quick correctness check
first:

```
python scripts/bench-generation-pool/bench_compare_gen.py \
    --tiers 1000 --types city --reps 2 --warmup 1 --bootstrap 10
```

prints `SMOKE COMPLETE` and never a recommendation -- it is a wiring check,
not a measurement.

## Reading the output

Per (type, n_rows) cell: `ratio_median` / `ratio_p95` (pooled wall time over
per-row wall time; below 1.0 means pooling won that rep), the bootstrap
`ci_low`/`ci_high` on that ratio, both arms' median rows/s, and the peak-RSS
ratio/delta (`os.wait4`'s `ru_maxrss`, the authoritative source).

The recommendation (full-sweep runs only) is the smallest row count per type
whose ratio CI upper bound falls under 1.0 **and stays under 1.0 for the
next measured tier** -- a lone win that regresses again at the next tier is
treated as noise, not a crossover. If only the LAST measured tier wins, with
no further tier available to confirm it, that is reported as "crossover at
or beyond sweep boundary" rather than a false confirmed number. The overall
recommendation is the MAX of every type's confirmed crossover (both the raw
value and a rounded, padded number are reported); it is withheld, with a
stated reason, if any type never confirms a crossover within the sweep, or
if pooling's peak-RSS cost exceeds `--max-rss-ratio` (default 1.25x) or
`--max-rss-delta-kb` (default 51,200, i.e. 50 MB) at or above the
recommended tier.

## Owed follow-up: applying the result

This harness measures; it does not act on the measurement. Once a real sweep
runs and produces a recommendation, updating
`src/decoy_engine/generation/_faker_pool.py`'s `N_THRESHOLD` constant to the
recommended value is a separate, data-backed follow-up -- out of scope here
by design (this unit ships the harness, not a `src/` change).

## Out of scope

- The real offline sweep itself, and the resulting `N_THRESHOLD` change.
- Cloud-scale sweep wiring (a platform-side harness exists for that shape;
  wiring this probe into it is a later follow-up if the local sweep needs a
  bigger host).
- Any faker type outside the ten pool-eligible types
  (`_faker_pool.POOL_ELIGIBLE_FAKER_TYPES`) -- a non-allowlisted `--types`
  value is rejected up front, before any subprocess runs.
- Any change to `src/`.

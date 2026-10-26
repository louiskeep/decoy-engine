# Synthea scale-characterization harness (RW-A / A-2)

One instrumented harness that masks a realistic, Synthea-shaped healthcare dataset
through the normal `run_pipeline` path with the native companion ON, and emits an
honest size-to-time record. It measures **speed, memory and throughput only** --
V1 makes no distribution-fidelity or differential-privacy claim, so this harness
asserts none. The reader-facing table it backs is
[`docs/guides/sizing-and-hardware.md`](../../docs/guides/sizing-and-hardware.md);
the measurement record is in `docs/records/`.

## What it builds

A three-table FK chain with realistic PHI columns and strategies:

| Table | ~cols | FK | PHI strategies a hospital would use |
|---|---|---|---|
| `patients` | 15 | (root) | FPE on SSN/licenses, month bucket-perturb on birth/death dates, deterministic categorical remap on names/address/city and marital/race/gender/state, ZIP truncated to the HIPAA 3-digit prefix, keyed hash on the patient id |
| `encounters` | 15 | `patient` -> `patients.id` | keyed hash on the encounter/org/provider/payer/code ids, month bucket-perturb on start/stop, FPE on the cost fields, categorical on encounter class, text-redaction on the free-text descriptions |
| `observations` | 10 | `encounter` -> `encounters.id` | keyed hash on code/provider + the FKs, month bucket-perturb on the date, FPE on the value, categorical on category/units/type, text-redaction on the free-text description |

Row counts are TOTAL across the chain, split `patients : encounters : observations
= 1 : 5 : 14`. The `1m` / `10m` / `100m` tiers are presets. Only 1M and 10M are
measured; the 100M tier is extrapolated, not run (see below).

## Reproduce the 1M and 10M results (one command each)

```bash
# env: the engine + the native companion must be importable.
export PYTHONPATH="$PWD/src:$PWD"

# 1M tier
python scripts/synthea-scale/build_synthea_dataset.py --tier 1m  --out-dir /tmp/synthea1m
python scripts/synthea-scale/bench_worker_synthea.py  --data-dir /tmp/synthea1m \
    --rss-ceiling-mb 8192 --repeats 3 --out results/synthea-1m.json

# 10M tier
python scripts/synthea-scale/build_synthea_dataset.py --tier 10m --out-dir /tmp/synthea10m
python scripts/synthea-scale/bench_worker_synthea.py  --data-dir /tmp/synthea10m \
    --rss-ceiling-mb 8192 --out results/synthea-10m.json
```

Each worker run prints one `BENCH_JSON {...}` line and, with `--out`, writes the
same record as indented JSON.

## The 100M tier (extrapolated, not run)

There is **no paid 100M GCP run** (Cam, 2026-10-26) and no `synthea` battery is
wired into the GCP harness. The 100M row in the sizing guide and the measurement
record is an extrapolation from the measured 1M/10M curve (method shown in the
record); authoritative large-scale numbers come from the prodsim work.

The builder does take explicit per-table counts, so a real 100M run is the same
code path with a larger input if one is ever scoped as a separate work item:

```bash
python scripts/synthea-scale/build_synthea_dataset.py --out-dir /data/synthea100m \
    --patients 5000000 --encounters 25000000 --observations 70000000
python scripts/synthea-scale/bench_worker_synthea.py --data-dir /data/synthea100m \
    --rss-ceiling-mb 8192 --out /data/results/synthea-100m.json
```

## How it measures (honestly)

- **Two processes.** A supervisor (reused from `scripts/fk-scale/_supervisor`, not
  forked) spawns the worker per repeat, measures peak RSS from OUTSIDE via `wait4`'s
  `ru_maxrss` cross-checked against polled `VmHWM`, and always emits a record -- even
  when the worker is OOM-killed, signalled or hung. `completed` is true only when the
  run returned, committed row counts equal the inputs, and FK parity passed; `rc=0`
  alone is never success.
- **Native disposition.** The record reports each column's `executed_backend`
  (compiled Rust kernel vs Python oracle), discovered from the engine's own stamps --
  a successful companion import is not taken as proof of native execution.
- **Order-independent FK parity.** `_synthea_parity` verifies that every sampled
  masked child FK still resolves to a masked parent key (referential integrity) and
  that the keys were genuinely masked, without assuming the route preserved row
  order, so it is correct on both the sequential and batch routes.

## Key fields in `BENCH_JSON`

`completed`, `mask_s` (+ `mask_s_p50`/`p95`), `throughput_rows_per_s`, `rows`,
`total_rows`, `table_count`, `column_count`, `peak_rss_mb` (+ `peak_rss_poll_mb`),
`rss_within_ceiling`, `spill.temp_disk_peak_mb`, `route`, `disposition`,
`native_companion`, `parity`, `machine`, `run_date_utc`.

# Record: Synthea scale characterization (RW-A / A-2)

Date: 2026-10-25. Engine `origin/main` @ `195fab1`, version 0.7.0, with the Rust
native companion built from `decoy-engine-native` (`maturin develop --release`).
Harness: [`scripts/synthea-scale/`](../../scripts/synthea-scale/README.md). Reader
table: [`docs/guides/sizing-and-hardware.md`](../guides/sizing-and-hardware.md).

This records what was measured, the exact config, the machine, and the
assumptions a reader must know. It characterizes **speed, memory and throughput
only**; V1 makes no fidelity or DP claim and this harness asserts none.

## What was measured

| Tier | Total rows | patients / encounters / observations | Where |
|---|---:|---|---|
| 1M | 1,000,000 | 50,000 / 250,000 / 700,000 | this box (below) |
| 10M | 10,000,000 | 500,000 / 2,500,000 / 7,000,000 | this box (below) |
| 100M | 100,000,000 | 5,000,000 / 25,000,000 / 70,000,000 | GCP reference run (orchestrator) |

Row counts are TOTAL across the chain, split `patients : encounters : observations
= 1 : 5 : 14`. The named tiers are presets; the builder also takes explicit
per-table counts, so the 100M GCP run is the same code path with a larger input.

## The machine (1M / 10M runs)

A development cloud container, **not** the GCP reference host:

- 4 vCPU, ~16 GB RAM (`total_ram_mb` 16091), x86-64, Linux 6.18.
- Python 3.12.3, engine 0.7.0, Rust companion present (`_kernel.cpython-312`).
- Single native thread (`--native-threads 1`).

The worker stamps `machine` and `run_date_utc` into every record, so the GCP
runs self-label; the 100M row and a clean single-hardware 1M/10M curve come from
the reference host.

## The exact config

Three FK-related tables, 40 columns, realistic HIPAA Safe-Harbor-style
de-identification. Strategy counts (from `_synthea_config.declared_strategies`):

- `hash` x12 -- FK keys (patient id, encounter id and their child references) and
  the clinical/org/provider/payer code identifiers. FK keys and their child
  columns share one namespace per edge, so a masked child FK equals its masked
  parent key and joins survive.
- `fpe` x7 -- SSN and ZIP-domain identifiers, driver/passport (ALPHANUM), and the
  cost/value numeric fields (digits; the FF1 >=6-digit domain floor is why ZIP, 5
  digits, uses truncation instead).
- `text_redact` x7 -- first/last name, street address, city, and the free-text
  descriptions.
- `bucket_perturb` x5 (month granularity) -- birth/death dates, encounter
  start/stop, observation date (Safe-Harbor date generalization).
- `categorical` x8 (deterministic remap) -- marital, race, gender, state,
  encounter class, observation category, units, type.
- `truncate` x1 -- ZIP to its 3-digit prefix.

Relationships (`orphan_policy: preserve`, `post_validation: true`):
`patients.id <- encounters.patient` (ns `synthea_patient_id`) and
`encounters.id <- observations.encounter` (ns `synthea_encounter_id`).

### Why these strategies (a finding the orchestrator must know)

The config deliberately uses **out-of-core-supported** strategies so the job runs
the memory-bounded streaming route and scales to 100M. The two most "natural"
PHI strategies are NOT out-of-core-supported and were therefore avoided:

- `date_shift` (per-value keyed date offset) and `faker` (synthetic replacement)
  are deferred Group-B strategies on the out-of-core route
  (`src/decoy_engine/execution/out_of_core/_compat.py`): `date_shift` needs a
  row-error/quarantine channel and whole-column format detection the chunked
  kernel lacks; `faker` needs a registry-backed value pool and cross-batch cache
  it has no channel for.
- A config that uses them is routed to the **sequential** route instead
  (`route_reason: pure_mask_fk`), which is table-at-a-time resident, not
  chunk-bounded. Measured here, that route grew from ~1.4 GB peak at 1M to
  ~8.8 GB (and still climbing) at 10M -- i.e. it does not hold a fixed ceiling and
  would not complete a 100M observations table within a sane memory bound.

So a realistic de-identification config that must scale to 100M uses the
strategies above. **Flag for the orchestrator:** if the 100M claim must cover a
`faker`/`date_shift` config, that is an engine gap (out-of-core support for those
two strategies), not something the harness can measure around -- it belongs to a
separate engine work item, per the handoff's "stop and write it up" rule.

## Results (measured, native companion ON)

Auto-routed to the out-of-core streaming route
(`route_reason: byte_estimate_bounded_out_of_core`); no route forcing. Every
column ran the compiled native kernel (37/37 with an `executed_backend` stamp).
FK parity passed on every edge (order-independent referential-integrity sample +
the engine's own `post_validation` FK join), row counts preserved.

| Tier | Route | Native cols | Wall (mask) | Throughput | Peak RSS (ext.) | Spill (temp disk) | Reorder lane | Parity |
|---|---|:---:|---:|---:|---:|---:|---|:---:|
| 1M | out_of_core | 37/37 | 23.7 s p50 / 24.8 s p95 (3 repeats) | ~43,200 rows/s | 502.6 MB | 42.6 MB | not taken (all batch) | ok |
| 10M | out_of_core | 37/37 | 255.6 s (1 run) | ~39,100 rows/s | 1956.2 MB | 3162.0 MB | taken for `observations` | ok |
| 100M | out_of_core | (GCP run) | -- | -- | declared ceiling 6.5 GiB | ~30 GB projected | both edges (proj.) | -- |

Peak RSS is measured **externally** by the supervisor (`wait4` `ru_maxrss`,
cross-checked against polled `VmHWM`), not in-process.

Observations on the trend (the honest reading, no flattering):

- **Throughput is roughly constant** (~40k rows/s single-threaded), so wall-clock
  is about linear in rows: 100M projects to ~42-45 min on this class of box.
- **Peak RSS grows sub-linearly, not flat**: 0.5 GB at 1M, 2.0 GB at 10M (10x
  rows, ~4x memory). The growth is the FK join bookkeeping, which scales with the
  number of distinct keys, not a per-chunk cost. The 6.5 GiB figure for 100M is a
  **declared ceiling to confirm**, extrapolated from this sub-linear trend -- not
  a measured fact. If the 100M run exceeds it, that is a finding, not a harness bug.
- **Routing shifts with scale**: at 1M every table fits one batch; at 10M the
  7M-row `observations` table crosses the ~2M-key reorder threshold and runs the
  bounded external-sort lane (`reorder taken`), while the smaller edges stay on
  the batch joiner. At 100M every table is over the threshold, so all run the
  bounded lane -- which is what should keep RSS near the ceiling.
- **Spill scales with the data**: 43 MB at 1M, 3.2 GB at 10M, so a 100M run needs
  tens of GB of fast temp disk (budget ~30 GB); the worker records
  `temp_disk_peak_mb` so the GCP run confirms the real figure.

Raw records: `docs/records/synthea-scale/synthea-1m.json`,
`synthea-10m.json`.

## Assumptions a reader must know

1. **Dev box, not the reference host.** 1M/10M are on a 4-vCPU/16 GB container;
   production numbers come from the GCP `n2-standard-8` run.
2. **Synthetic data.** Values are drawn from fixed pools (sized for speed at tens
   of millions of rows); cardinality is realistic, not a specific hospital export.
   Deterministic seed, so runs reproduce.
3. **FK shape.** A single-path chain (patients -> encounters -> observations) with
   a denormalized, coherently-masked `observations.patient`. The `1:5:14` fan-out
   is a plausible-but-chosen ratio, not drawn from a specific Synthea population.
4. **Parity is referential, not positional.** The check verifies that every
   sampled masked child FK still resolves to a masked parent key and that keys
   were genuinely masked, in bounded memory, without assuming the route preserved
   row order (the batch route may reorder). The engine's `post_validation` is the
   independent second check.
5. **One native thread.** More threads reduce wall time, not the memory ceiling.

## Reproduce

```bash
export PYTHONPATH="$PWD/src:$PWD"
python scripts/synthea-scale/build_synthea_dataset.py --tier 1m  --out-dir /tmp/synthea1m
python scripts/synthea-scale/bench_worker_synthea.py  --data-dir /tmp/synthea1m  --rss-ceiling-mb 8192 --repeats 3 --out docs/records/synthea-scale/synthea-1m.json
python scripts/synthea-scale/build_synthea_dataset.py --tier 10m --out-dir /tmp/synthea10m
python scripts/synthea-scale/bench_worker_synthea.py  --data-dir /tmp/synthea10m --rss-ceiling-mb 8192 --out docs/records/synthea-scale/synthea-10m.json
```

## For the GCP 100M run (orchestrator)

The platform `scripts/gcp-bench/engine-bench.sh` battery dispatcher takes a
`--battery` name and runs a worker from `scripts/fk-scale/`. A `synthea` battery
reuses that plumbing: on the node, build the fixture with explicit per-table
counts (`--patients 5000000 --encounters 25000000 --observations 70000000`) then
run `bench_worker_synthea.py --data-dir <fixture> --rss-ceiling-mb 6656`, and sync
the `BENCH_JSON` record. This record leaves the 100M row for that run; wiring the
battery case is left to the orchestrator to avoid colliding with the live D4
`fk-scale` battery lane.

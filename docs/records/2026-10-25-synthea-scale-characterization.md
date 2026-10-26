# Record: Synthea scale characterization (RW-A / A-2)

Measured 2026-10-26, on the engine rebased onto current `main`. Engine code under
test is `origin/main` @ `e3abfdf` (the vault write-seam + tokenize registration +
route-coverage phase-1 B3-B7 OOC/SEQ); the branch adds only the harness, this record
and the reader guide on top and touches no `src/` engine code, so the numbers come
from unmodified `main`. (The runs executed with the working tree at `a39c648`; the
only `main` change since is ruff-formatting of test files under `tests/parity/`, so
`src/decoy_engine` is byte-identical and the engine runtime is the same.) (These
figures are also stable across the route-coverage merge itself: a run against the
prior `bbe4dce` base measured within noise, see the trend notes.) At that commit
BOTH version literals
(`pyproject.toml` version and the in-tree `decoy_engine.__version__` constant) read
`0.7.0`, frozen on `main`; the `0.14.2` version is cut on the release branch and is
not reachable from this checkout, so it is NOT asserted here (an earlier draft named
a release-tag SHA that does not resolve in this repo; dropped). The real
discriminators for reproducing these numbers are the commit SHA and the
`faker==40.23.0` pin. Config digest (sha256 of the structural mask config, source
and target paths excluded): `fb5abdb7e64a`. Built with the Rust native companion
from `decoy-engine-native` (`maturin develop --release`, CPython 3.12). Harness:
[`scripts/synthea-scale/`](../../scripts/synthea-scale/README.md). Reader table:
[`docs/guides/sizing-and-hardware.md`](../guides/sizing-and-hardware.md).

This records what was measured, the exact config, the machine, and the
assumptions a reader must know. It characterizes **speed, memory and throughput
only**; V1 makes no fidelity or DP claim and this harness asserts none. The 1M
and 10M tiers are measured; the 100M tier is an extrapolation (method below), not
a run.

## What was measured, and what is extrapolated

| Tier | Total rows | patients / encounters / observations | Status |
|---|---:|---|---|
| 1M | 1,000,000 | 50,000 / 250,000 / 700,000 | measured (this box, below) |
| 10M | 10,000,000 | 500,000 / 2,500,000 / 7,000,000 | measured (this box, below) |
| 100M | 100,000,000 | 5,000,000 / 25,000,000 / 70,000,000 | **extrapolated from 1M/10M (not run)** |

Row counts are TOTAL across the chain, split `patients : encounters : observations
= 1 : 5 : 14`. The named tiers are presets; the builder also takes explicit
per-table counts. **Decision (Cam, 2026-10-26): there is no paid 100M GCP run.**
The 100M row is a projection from the measured curve; the real large-scale
benchmarks come from the prodsim work, not a one-off 100M masking job.

## The machine (1M / 10M runs)

A development cloud container, **not** a dedicated reference host:

- 4 vCPU, ~16 GB RAM (`total_ram_mb` 16091), x86-64, Linux 6.18
  (`node: vm`, `platform: Linux-6.18.44-fc-v153-x86_64-with-glibc2.39`).
- Python 3.12.3, engine at `origin/main` @ `e3abfdf` (`__version__` reads 0.7.0; see
  header), Rust companion present (`_kernel.cpython-312`).
- Single native thread (`--native-threads 1`).

The worker stamps `machine` and `run_date_utc` into every record, so a run on any
other box self-labels. Wall-clock throughput varies between container instances
even on this same image (see "Observations"); the memory and spill figures are
deterministic and reproduce closely.

## The exact config

Three FK-related tables, 40 columns, realistic HIPAA Safe-Harbor-style
de-identification. Strategy counts (from `_synthea_config.declared_strategies`):

- `hash` x12 -- FK keys (patient id, encounter id and their child references) and
  the clinical/org/provider/payer code identifiers. FK keys and their child
  columns share one namespace per edge, so a masked child FK equals its masked
  parent key and joins survive.
- `categorical` x12 (deterministic remap) -- the direct-identifier name/address
  cells (first name, last name, street address, city) remapped onto a synthetic
  pool, plus the small coded fields (marital, race, gender, state, encounter
  class, observation category, units, type). Deterministic: the same source value
  maps to the same synthetic value within the column namespace.
- `fpe` x7 -- SSN, driver/passport licenses (ALPHANUM), and the four cost/value
  numeric fields (digits). ZIP is NOT fpe: a 5-digit ZIP is below the FF1
  >=6-digit domain floor, so ZIP uses truncation instead (see `truncate` below).
- `bucket_perturb` x5 (month granularity) -- birth/death dates, encounter
  start/stop, observation date (Safe-Harbor date generalization). These are
  keyed month-bucket perturbations, not per-value `date_shift` (which is not
  out-of-core supported; see below).
- `truncate` x1 -- ZIP to its 3-digit prefix (the HIPAA Safe-Harbor ZIP
  treatment).
- `text_redact` x3 -- the free-text description columns (encounter description,
  reason description, observation description). `text_redact` is a span-redaction
  pass: it removes detected PII spans *inside* a free-text cell. On these
  synthetic descriptions (short coded phrases with no embedded identifiers) and
  with no NER model configured it finds no spans, so it is a pass-through here --
  the measurement captures its per-cell scan cost, not a content change. It is
  the correct strategy for a genuine free-text notes column; it is NOT used on the
  structured name/address cells (those are `categorical`, above), because on a
  single-token identifier cell it would detect nothing and leave the value
  unchanged.

Relationships (`orphan_policy: preserve`):
`patients.id <- encounters.patient` (ns `synthea_patient_id`) and
`encounters.id <- observations.encounter` (ns `synthea_encounter_id`).

**FK validation.** The harness checks referential integrity with its own
order-independent parity sample (`_synthea_parity`): every sampled masked child
FK still resolves to a masked parent key, and both ends are genuinely masked. The
engine's built-in `post_validation` fk_validity scan is **not** run here, and
`post_validation` is deliberately left off. It is a run_pipeline runtime kwarg
(not a `global_settings` field), and turning it on requires the full frame
resident: the router declines an opted-in job from the out-of-core route
(`post_validation_requested`) and diverts it to full-frame, or fail-closed-rejects
it when it is too large. Passing it would therefore defeat the out-of-core
characterization this harness exists to produce (and would reject outright at
100M). The harness parity sample is the FK check; it runs on the committed
out-of-core output directly.

### Why these strategies (a finding the orchestrator must know)

The config deliberately uses **out-of-core-supported** strategies so the job runs
the memory-bounded streaming route and can scale. The two most "natural" PHI
strategies for per-value replacement are NOT out-of-core-supported and were
therefore avoided:

- `date_shift` (per-value keyed date offset) and `faker` (synthetic replacement)
  are deferred Group-B strategies on the out-of-core route
  (`src/decoy_engine/execution/out_of_core/_compat.py`): `date_shift` needs a
  row-error/quarantine channel and whole-column format detection the chunked
  kernel lacks; `faker` needs a registry-backed value pool and cross-batch cache
  it has no channel for.
- A config that uses them is not out-of-core-compatible and is routed to the
  **sequential** route instead (`run_sequential`), which is table-at-a-time
  resident and O(cardinality), not chunk-bounded. That route does not hold a fixed
  memory ceiling as the data grows and would not complete a 100M observations
  table within a sane memory bound. (This harness did not benchmark that route;
  the point is the routing fact, not a measured peak.)

So a realistic de-identification config that must scale uses the strategies
above. The name/address columns use deterministic `categorical` because it is the
only native, name-shaped strategy on the out-of-core route: it produces
name-shaped output (from a caller-supplied pool), is genuinely transforming (not a
no-op like `text_redact` on a single-token cell), and keeps the job on the bounded
native route. **Flag for the orchestrator:** if a large-scale claim must cover a
`faker`/`date_shift` config, that is an engine gap (out-of-core support for those
two strategies), not something this harness can measure around.

## Results (measured, native companion ON)

Auto-routed to the out-of-core streaming route
(`route_reason: byte_estimate_bounded_out_of_core`); no route forcing. Every
stamped mask column ran the compiled native kernel (37 of the 40 columns carry an
`executed_backend` stamp; all 37 ran native -- the 3 unstamped are FK key/child
columns resolved through the join path). FK parity passed on every edge (the
harness's order-independent referential-integrity sample), row counts preserved.

Wall time below is the median (p50) over the runs; throughput is `total rows ÷
that wall`, so the two are on one basis. The raw per-run detail (including the
per-run throughput field, which the worker stamps from the last run) is in the JSON
records. Config digest for every row: `fb5abdb7e64a` (see header).

| Tier | Route | Native cols | Wall (mask) | Throughput | Peak RSS (ext.) | Spill (temp disk) | Reorder lane | Parity |
|---|---|:---:|---:|---:|---:|---:|---|:---:|
| 1M | out_of_core | 37/37 | 25.4 s p50 (26.2 s p95, 3 runs) | ~39,000 rows/s | 509.2 MB | 59.4 MB | not taken (all batch) | ok |
| 10M | out_of_core | 37/37 | 274.0 s (~4 min 34 s, 1 run) | ~36,500 rows/s | 1983.5 MB | 3162.0 MB | taken for `observations` | ok |
| 100M | out_of_core | (extrapolated) | ~46 min (extrap.) | ~36,500 rows/s (extrap.) | ~7.7 GB (extrap.) | ~31 GB (extrap.) | `encounters` + `observations` (expected) | -- |

Peak RSS is measured **externally** by the supervisor (`wait4` `ru_maxrss`,
cross-checked against polled `VmHWM`), not in-process.

Observations on the trend (the honest reading, no flattering):

- **Throughput is roughly constant** across the two tiers (~36-39k rows/s
  single-threaded on this instance; 10M came in marginally below 1M, within
  instance noise), so wall-clock is about linear in rows. NOTE on box variance:
  wall-clock throughput varies widely between container instances even on this same
  image. Earlier A-2 runs of the same code measured ~24 s / ~43k rows/s at 1M on a
  fast instance and ~38 s / ~26k rows/s at 1M with ~428 s at 10M on a slow one; this
  re-run (engine rebased onto current `main`) landed near the fast end (~25 s at 1M,
  ~274 s at 10M). The config shape is not the variable: a back-to-back A/B on one
  instance measured the old `text_redact` names/address config at 39.3 s and the
  current `categorical` config at 38.7 s for the same 1M fixture (within noise).
  Wall-clock is an order-of-magnitude reference, not an SLA; the memory and spill
  figures (next bullets) are the stable signal.
- **Peak RSS grows sub-linearly, not flat**: 0.5 GB at 1M, 2.0 GB at 10M (10x
  rows, under 4x memory). The growth is the FK join bookkeeping, which scales with
  the number of distinct keys, not a per-chunk cost. These memory figures reproduce
  closely across instances AND across the route-coverage merge: the 10M peak held at
  1983.5 MB on `a39c648` against 1992.7 MB on the prior `bbe4dce` base and 1996.9 MB
  on an earlier instance (all within ~0.7%); the 1M peak held at ~509 MB on both
  recent runs; and the 10M spill reproduced to within 0.1 MB across all three. The
  route-coverage phase-1 changes to the out-of-core path did not move the memory or
  spill numbers.
- **Routing shifts with scale**: inside the out-of-core route, a non-root table
  takes the bounded external-sort (reorder) lane when its **largest incoming FK
  edge's deduped parent-key count** reaches ~2M (`out_of_core_reorder_threshold_rows`,
  default 2M; see `_pipeline_routing.py` / `_route_policy.decide_route`). The
  threshold is on the *parent's* key count, so the **root `patients` table has no
  incoming edge and never enters the reorder lane** -- it always runs the batch
  joiner. At 10M only `observations` reorders (parent `encounters` has 2.5M keys
  >= 2M; `encounters`' parent `patients` has 500k keys < 2M, so `encounters` stays
  batch). At 100M (extrapolated) both `encounters` (parent 5M keys) and
  `observations` (parent 25M keys) cross the threshold while `patients` stays
  batch.
- **Spill scales with the data** once the reorder lane is active: ~59 MB at 1M (no
  reorder, a small staging high-water mark), 3.2 GB at 10M, so a 100M run needs tens
  of GB of fast temp disk (extrapolated ~31 GB).

### How the 100M row is extrapolated (not measured)

The 100M row in both tables is a projection from the two measured points, not a
run:

- **Wall / throughput:** hold throughput at the 10M figure (~36,500 rows/s, the
  point closest to 100M behaviour); 100,000,000 ÷ 36,500 ≈ 2,740 s ≈ ~46 min. This
  is faster than an earlier draft's ~71 min because that projection held a slower
  instance's ~23,300 rows/s; the wall projection tracks whatever instance measured
  the 10M point, which is exactly why it is labelled order-of-magnitude, not an SLA.
- **Peak RSS:** fit `peak_rss ≈ a · rows^b` to (1M, 509 MB) and (10M, 1984 MB),
  giving `b ≈ 0.59`; extended to 100M that is ~7.7 GB. This is a touch above the
  earlier draft's ~7 GB fit: this run's 1M peak (509 MB) came in below that draft's
  542 MB while the 10M anchor held, which steepens the slope. A two-point power-law
  fit is fragile; both drafts agree the 100M peak is in the 7-8 GB band, and only a
  real run settles it.
- **Temp disk:** scale the 10M spill (~3.2 GB) roughly linearly with rows (the
  reorder lane is active at both 10M and 100M), giving ~31 GB.

These are order-of-magnitude projections to set expectations; the authoritative
large-scale numbers come from prodsim.

Raw records: `docs/records/synthea-scale/synthea-1m.json`,
`synthea-10m.json`.

## Assumptions a reader must know

1. **Dev box, not a dedicated reference host.** 1M/10M are on a 4-vCPU/16 GB
   container. Memory/spill reproduce closely; wall-clock/throughput vary between
   instances (see above).
2. **Synthetic data.** Values are drawn from fixed pools (sized for speed at tens
   of millions of rows); cardinality is realistic, not a specific hospital export.
   Deterministic seed, so runs reproduce.
3. **FK shape.** A single-path chain (patients -> encounters -> observations) with
   a denormalized, coherently-masked `observations.patient`. The `1:5:14` fan-out
   is a plausible-but-chosen ratio, not drawn from a specific Synthea population.
4. **Parity is referential, not positional, and is the only FK check here.** The
   check verifies that every sampled masked child FK still resolves to a masked
   parent key and that keys were genuinely masked, in bounded memory, without
   assuming the route preserved row order (the batch route may reorder). The
   engine's `post_validation` scan is not run (it forces full-frame; see above).
5. **One native thread.** More threads were not measured. They would be expected
   to reduce wall time, not the memory ceiling, but this harness makes no measured
   thread-scaling claim.

## Reproduce

```bash
export PYTHONPATH="$PWD/src:$PWD"
python scripts/synthea-scale/build_synthea_dataset.py --tier 1m  --out-dir /tmp/synthea1m
python scripts/synthea-scale/bench_worker_synthea.py  --data-dir /tmp/synthea1m  --rss-ceiling-mb 8192 --repeats 3 --out docs/records/synthea-scale/synthea-1m.json
python scripts/synthea-scale/build_synthea_dataset.py --tier 10m --out-dir /tmp/synthea10m
python scripts/synthea-scale/bench_worker_synthea.py  --data-dir /tmp/synthea10m --rss-ceiling-mb 8192 --out docs/records/synthea-scale/synthea-10m.json
```

## On the 100M tier (no GCP run)

Per Cam's 2026-10-26 decision there is **no paid 100M GCP run** and **no `synthea`
battery is wired into the GCP harness**. The 100M row in this record and in the
sizing guide is the extrapolation above. Authoritative large-scale benchmarks come
from the prodsim work. If a future need requires a real 100M masking measurement,
it is a separate, scoped work item, not a one-off here.

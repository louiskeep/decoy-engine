# Sizing and hardware: how long a masking job takes

This page answers a buyer's question: **for a dataset of N rows, about how long
does a Decoy masking job take, and how much memory does it need?**

The numbers come from one instrumented harness
([`scripts/synthea-scale/`](../../scripts/synthea-scale/README.md)) that masks a
realistic, Synthea-shaped healthcare dataset through the normal engine run path
with the native (Rust) companion on. The **1M and 10M rows are measured** from the
running process (wall-clock around the mask step; peak resident memory from the
OS, outside the worker). The **100M row is an extrapolation** from those two
measured points, clearly labelled as such and with its method shown; it is not a
measured number, and real large-scale benchmarks come from the prodsim work, not
a one-off 100M masking job. V1 makes no distribution-fidelity or
differential-privacy claim, so this page characterizes **speed, memory and
throughput only** -- it says nothing about statistical quality.

## The workload

Three related tables with realistic PHI columns and a realistic de-identification
config (HIPAA Safe-Harbor style):

- **patients** -> **encounters** -> **observations**, joined by foreign keys, in a
  `1 : 5 : 14` row ratio (each patient ~5 encounters, each ~2.8 observations).
- 40 columns in all. Identifiers (SSN, licenses, costs/values) are
  format-preserving encrypted; dates are generalized to the month; names, street
  addresses and cities are consistently remapped onto a synthetic pool (same
  source value -> same synthetic value, a deterministic categorical remap); ZIP is
  truncated to its 3-digit prefix; clinical codes and foreign keys are
  keyed-hashed so joins still work after masking; small coded fields (gender,
  race, encounter class, units) are consistently remapped; the free-text
  description columns run a span-redaction pass.
- Foreign-key joinability is preserved and checked on every edge after masking
  (the harness's own order-independent referential-integrity sample).

"Rows" below is the **total across all three tables**.

## Size-to-time

> **Hardware for the measured rows below: a 4-vCPU / 16 GB x86-64 Linux cloud
> container (the development reference box), single native thread, engine built from
> `main` (Faker 40.23.0 pin) with the Rust companion.** These are a development-box
> reference point, not a production SLA. Throughput varies between container
> instances even on the same image (see the caveats); treat the wall-clock
> numbers as order-of-magnitude. Measured 2026-10-26; config digest `fb5abdb7e64a`.

| Total rows | Tables | Columns | Wall time | Throughput | Peak memory | Temp disk (spill) | Native columns |
|---:|:---:|:---:|---:|---:|---:|---:|:---:|
| 1,000,000 | 3 | 40 | ~25 s (p50 25.4 s, p95 26.2 s; 3 runs) | ~39,000 rows/s | ~0.5 GB | ~0.06 GB | 37 / 37 |
| 10,000,000 | 3 | 40 | ~4 min 34 s (1 run) | ~36,500 rows/s | ~2.0 GB | ~3.2 GB | 37 / 37 |
| 100,000,000 | 3 | 40 | _~46 min (extrapolated)_ | _~36,500 rows/s (extrap.)_ | _~7.7 GB (extrapolated)_ | _~31 GB (extrapolated)_ | _37 / 37 (expected)_ |

Throughput in each row is `total rows ÷ the wall time shown`, so wall and
throughput are on one basis (an earlier version of this table mixed the median
wall with the fastest run's throughput). Read the first row as: *a one-million-row
job across three related tables and 40 masked columns finishes in about half a
minute and needs about half a gigabyte of memory, on a modest 4-core development
container.*

### How the 100M row is extrapolated (not measured)

The 100M row is a projection from the two measured points, shown here so a reader
can check it rather than take it on faith. **It is not a measured number.** The
true large-scale numbers come from the prodsim benchmarking work; this harness
deliberately does not run a paid 100M job.

- **Wall / throughput:** measured throughput is roughly flat with scale (~39k
  rows/s at 1M, ~36.5k rows/s at 10M), so the projection holds throughput at the
  10M figure (~36,500 rows/s, the point closest to 100M behaviour) and divides:
  100,000,000 ÷ 36,500 ≈ 2,740 s ≈ **~46 min** on this instance. (An earlier draft
  projected ~71 min from a slower instance's ~23,000 rows/s; the wall projection
  tracks whatever instance measured the 10M point, which is why it is labelled
  order-of-magnitude, not an SLA.)
- **Peak memory:** the two measured points (0.5 GB at 1M, 2.0 GB at 10M) fit a
  sub-linear power law `peak_rss ≈ a · rows^b` with `b ≈ 0.59`. Extended to 100M
  that gives **~7.7 GB**. (A touch above an earlier draft's ~7 GB fit, because this
  run's 1M peak came in a little lower while the 10M anchor held; a two-point fit is
  fragile, and only a real run settles it.)
- **Temp disk:** spill is driven by the external-sort (reorder) lane, which is
  active from the 10M tier. Scaling the 10M spill (~3.2 GB) roughly linearly with
  rows gives **~31 GB** at 100M, so budget tens of GB of fast temp disk.

## What to take from this

- **Time scales about linearly with rows.** Measured throughput is roughly
  constant (~36-39k rows/s single-threaded on this box), so wall-clock grows with
  total rows: ~25 s at 1M and ~4.5 minutes at 10M (measured), extrapolating to the
  order of ~46 minutes at 100M on this instance. All runs here use a
  single native thread (`--native-threads 1`); multi-thread scaling was not
  measured, so this page makes no claim about it.
- **Memory grows much more slowly than the data (sub-linear), not in proportion
  to it.** Peak memory went from ~0.5 GB at 1M to ~2.0 GB at 10M -- a 10x increase
  in rows for under a 4x increase in memory -- because the engine streams the job
  out-of-core and spills to disk rather than loading whole tables. It is **not
  flat**: the join bookkeeping grows with the number of distinct keys. The
  extrapolated ~7.7 GB at 100M follows that same sub-linear curve; only a real run
  confirms it.
- **Plan for temp disk.** Out-of-core spill scales with the data once the reorder
  lane is active: ~3.2 GB of scratch at 10M, extrapolating to tens of GB at 100M
  (budget ~31 GB).
- **Routing shifts with scale.** Inside the out-of-core route, a non-root table
  switches from the batch joiner to the bounded external-sort (reorder) lane when
  its **largest incoming FK edge's deduped parent-key count** reaches ~2M. That
  threshold is on the *parent's* key count, not the table's own row count, so the
  **root `patients` table has no incoming edge and never enters the reorder
  lane** -- it always runs the batch joiner. At 10M only `observations` is on the
  reorder lane (its parent `encounters` has 2.5M keys ≥ 2M; `encounters`' parent
  `patients` has 500k keys < 2M, so `encounters` stays batch). At 100M
  (extrapolated) both `encounters` and `observations` cross the threshold and run
  the bounded lane while `patients` stays batch -- which is what keeps the memory
  ceiling sub-linear.

## Caveats (read these)

- **Hardware matters, and varies between instances.** The 1M/10M rows were
  measured on a 4-vCPU / 16 GB development container. The *memory* and *spill*
  numbers are deterministic and reproduce closely run to run; the *wall-clock /
  throughput* numbers vary noticeably between container instances even on the same
  image. Runs of the same code have measured anywhere from ~24 s / ~43k rows/s at 1M
  on a fast instance to ~38 s / ~26k rows/s on a slow one; the numbers in the table
  above (~25 s at 1M) sit near the fast end. Treat wall-clock as order-of-magnitude,
  not an SLA.
- **Synthetic data.** The dataset is generated to be Synthea-shaped and
  FK-consistent; values are drawn from fixed pools, so cardinality is realistic
  but not identical to a specific hospital export.
- **Config shapes the number.** This is one realistic de-identification config
  built from out-of-core-supported strategies. A config that uses per-row
  synthetic replacement (`faker`) or per-value `date_shift` is not out-of-core
  supported and runs on a different, memory-heavier route (sequential), so it
  would be slower and use more memory at scale (see the measurement record in
  `docs/records/`).
- **No quality claim.** These are speed and memory numbers. They do not measure
  how well the masked data preserves distributions or protects against
  re-identification.

The full configuration, machine details, the raw measured records, and the 100M
extrapolation method are in
[`docs/records/2026-10-25-synthea-scale-characterization.md`](../records/2026-10-25-synthea-scale-characterization.md).

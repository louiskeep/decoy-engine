# Sizing and hardware: how long a masking job takes

This page answers a buyer's question: **for a dataset of N rows, about how long
does a Decoy masking job take, and how much memory does it need?**

The numbers come from one instrumented harness
([`scripts/synthea-scale/`](../../scripts/synthea-scale/README.md)) that masks a
realistic, Synthea-shaped healthcare dataset through the normal engine run path
with the native (Rust) companion on. Every number is **measured from the running
process, not estimated or extrapolated** (wall-clock around the mask step; peak
resident memory from the OS, outside the worker). V1 makes no
distribution-fidelity or differential-privacy claim, so this page characterizes
**speed, memory and throughput only** -- it says nothing about statistical
quality.

## The workload

Three related tables with realistic PHI columns and a realistic de-identification
config (HIPAA Safe-Harbor style):

- **patients** -> **encounters** -> **observations**, joined by foreign keys, in a
  `1 : 5 : 14` row ratio (each patient ~5 encounters, each ~2.8 observations).
- 40 columns in all. Identifiers (SSN, licenses, costs) are format-preserving
  encrypted; dates are generalized to the month; names and street addresses are
  redacted; ZIP is truncated to its 3-digit prefix; clinical codes and foreign
  keys are keyed-hashed so joins still work after masking; small coded fields
  (gender, race, encounter class, units) are consistently remapped.
- Foreign-key joinability is preserved and checked on every edge after masking.

"Rows" below is the **total across all three tables**.

## Size-to-time

> **Hardware for the rows below: a 4-vCPU / 16 GB x86-64 Linux cloud container
> (the development reference box), single native thread, engine 0.7.0 with the
> Rust companion.** These are a development-box reference point. The 100M row is
> run by the release process on the GCP reference host (`n2-standard-8`); the
> same host should re-run 1M and 10M there for a clean single-hardware curve.

| Total rows | Tables | Columns | Wall time | Throughput | Peak memory | Temp disk (spill) | Native columns |
|---:|:---:|:---:|---:|---:|---:|---:|:---:|
| 1,000,000 | 3 | 40 | ~24 s (p95 ~25 s) | ~43,000 rows/s | ~0.5 GB | ~0.04 GB | 37 / 37 |
| 10,000,000 | 3 | 40 | ~4 min 16 s | ~39,000 rows/s | ~2.0 GB | ~3.2 GB | 37 / 37 |
| 100,000,000 | 3 | 40 | _~45 min (projected; GCP run fills this)_ | _~40,000 rows/s (proj.)_ | _declared ceiling 6.5 GiB_ | _~30 GB (proj.)_ | _37 / 37 (proj.)_ |

Read the first row as: *a one-million-row job across three related tables and 40
masked columns finishes in about 24 seconds and needs about half a gigabyte of
memory, on a modest 4-core machine.*

## What to take from this

- **Time scales about linearly with rows.** Throughput is roughly constant
  (~40,000 rows/s single-threaded on this box), so wall-clock grows with total
  rows: ~24 s at 1M, ~4 minutes at 10M, and on the order of ~45 minutes at 100M
  on this class of hardware. More cores / native threads shorten this.
- **Memory grows much more slowly than the data (sub-linear), not in proportion
  to it.** Peak memory went from ~0.5 GB at 1M to ~2.0 GB at 10M -- a 10x increase
  in rows for about a 4x increase in memory -- because the engine streams the job
  out-of-core and spills to disk rather than loading whole tables. It is **not
  flat**: the join bookkeeping grows with the number of distinct keys. The 100M
  row carries a *declared* 6.5 GiB ceiling that the release run must confirm; it
  is an expectation from the sub-linear trend, not yet a measured fact.
- **Plan for temp disk.** Out-of-core spill scales with the data: ~3 GB of
  scratch at 10M, so a 100M job needs tens of GB of fast temp disk (budget ~30 GB).
- **Routing shifts with scale.** Small tables are masked in one batch; once a
  table's key count passes ~2M the engine switches that table to a bounded
  external-sort (reorder) lane. At 100M every table is on the bounded lane, which
  is what keeps the memory ceiling in reach.

## Caveats (read these)

- **Hardware matters.** The 1M/10M rows were measured on a 4-vCPU / 16 GB
  development container, not the GCP reference host. Treat them as a reference
  point, not a production SLA; the release run re-measures on the reference host.
- **Synthetic data.** The dataset is generated to be Synthea-shaped and
  FK-consistent; values are drawn from fixed pools, so cardinality is realistic
  but not identical to a specific hospital export.
- **Config shapes the number.** This is one realistic de-identification config.
  A config that uses per-row synthetic replacement (`faker`) or per-value
  `date_shift` runs on a different, memory-heavier engine path and will be slower
  and use more memory at scale (see the measurement record in `docs/records/`).
- **No quality claim.** These are speed and memory numbers. They do not measure
  how well the masked data preserves distributions or protects against
  re-identification.

The full configuration, machine details, and the raw measured records are in
[`docs/records/2026-10-25-synthea-scale-characterization.md`](../records/2026-10-25-synthea-scale-characterization.md).

# Bug 5 calibration — engineering-correctness results

> **Captured:** 2026-05-09
> **Hardware:** Intel Core i7-1265U (2P + 8E, ~15W TDP) / 32 GB / Windows 11 / Python 3.10
> **Pipeline:** `source.file → filter (10% pass rate) → mask (single hash rule) → target.file`
> **Fixture:** HIPAA-shaped parquet, 10 columns, constant per-column values

## Numbers

| Rows | pandas elapsed | pandas peak RSS | hybrid elapsed | hybrid peak RSS |
|---|---|---|---|---|
| 1M | 1.66 s | 465 MB | **0.59 s** (2.8× faster) | 880 MB (1.9× more) |
| 5M | 6.12 s | 1259 MB | **3.14 s** (1.9× faster) | 1916 MB (1.5× more) |
| 10M | 12.06 s | 2697 MB | **5.47 s** (2.2× faster) | 4697 MB (1.7× more) |

## Findings

**CPU win is real and consistent.** Hybrid is 2–3× faster across all scales — DuckDB streaming the parquet read + Polars filtering an Arrow buffer is genuinely faster than pandas materializing then filtering.

**Memory claim is inverted for this pipeline shape.** Hybrid uses 1.5–1.9× *more* peak RSS than pandas at every scale. The architectural promise ("handles bigger data") doesn't hold here.

## Why

For pipelines with cross-engine transitions, multiple representations of the same data are alive simultaneously during stage transitions:

1. DuckDB reads parquet → Arrow table (10M rows)
2. Polars consumes Arrow → Polars frame **(10M rows briefly in both formats)**
3. Polars filter → 1M-row Polars frame
4. Polars → Arrow → pandas (1M rows)
5. mask → pandas → Arrow → DuckDB → parquet

The peak is hit during step 2 — the ~2 GB gap exactly matches one extra 10M-row representation. Pandas-only doesn't pay this because there's a single backend; the 10M-row source frame transforms in place into the 1M-row filtered frame.

## What this means for the architectural claim

The original sales line was "tens of millions out of the box, hundreds of millions on a good box," motivated by the assumption that DuckDB + Polars streaming would push the OOM frontier outward.

This calibration shows:

- **For source → relational → mask → sink pipelines** (the most common shape we'll see): hybrid is faster on CPU but heavier on memory. Pandas can hold *more* rows in a fixed RAM budget. Architectural promise inverted.
- **For aggregation/join-heavy pipelines**: DuckDB pushes computation down without materializing intermediates. Advantage probably reappears. **Not tested at this scale yet.**

The honest framing: **"hybrid is faster on CPU; pipeline shape determines whether it's lighter on memory."**

## Out of scope (caveats this calibration doesn't address)

- **50M+ row tier (marketing-correctness)** — needs cloud VM (32 GB laptop OOMs at hybrid memory growth past ~15M).
- **Aggregation/join pipelines** — no `join` or `aggregate` graph ops shipped yet (Item 19 queued).
- **Real-world fixture diversity** — constant-value columns are friendly to compression; high-cardinality data may shift the absolute numbers but not the qualitative pattern.
- **Cache eviction depth** — the 2 GB gap suggests the runner's eager-eviction holds dual representations during cross-engine op execution; a separate investigation may find a tighter bound.

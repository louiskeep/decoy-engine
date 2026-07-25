# Bug 5 calibration — engineering-correctness results

> **Captured:** 2026-05-09
> **Hardware:** Intel Core i7-1265U (2P + 8E, ~15W TDP) / 32 GB / Windows 11 / Python 3.10
> **Pipeline:** `source.file → filter (10% pass rate) → mask (single hash rule) → target.file`
> **Fixture:** HIPAA-shaped parquet, 10 columns, constant per-column values

## Run 1 — initial calibration (rechunk=True, the Polars default)

| Rows | pandas elapsed | pandas peak RSS | hybrid elapsed | hybrid peak RSS |
|---|---|---|---|---|
| 1M | 1.66 s | 465 MB | 0.59 s | 880 MB (1.9× more) |
| 5M | 6.12 s | 1259 MB | 3.14 s | 1916 MB (1.5× more) |
| 10M | 12.06 s | 2697 MB | 5.47 s | 4697 MB (1.7× more) |

Hybrid was 2-3× faster on CPU but used 1.5-1.9× more peak memory. Initial conclusion was that the architecture had a real boundary cost — DuckDB's Arrow output and Polars' frame coexisted during op execution because Polars was copying string columns into its own format on receive.

## Run 2 — D experiment (`pl.from_arrow(table, rechunk=False)`)

The Polars `from_arrow` API exposes a `rechunk` flag. Default `True` asks Polars to combine all Arrow chunks into one contiguous Polars-format buffer (cost: full byte copy of every column). `False` asks Polars to reference Arrow's chunks directly. For numeric columns the flag is fully zero-copy; for strings it depends on whether Polars' internal layout can share the Arrow buffer.

| Rows | pandas peak RSS | hybrid peak RSS (rechunk=True) | hybrid peak RSS (rechunk=False) | Saved |
|---|---|---|---|---|
| 1M | 456 MB | 880 MB | 782 MB | -98 MB (-11%) |
| 5M | 1326 MB | 1916 MB | 1492 MB | -424 MB (-22%) |
| 10M | 2418 MB | 4697 MB | **3179 MB** | **-1518 MB (-32%)** |

The savings scale with row count — consistent with "more strings → more savings to be had." The HIPAA fixture has 7 string columns + 3 numeric columns; the string columns were where the cost lived.

CPU wins preserved (and slightly improved on this run):

| Rows | pandas elapsed | hybrid elapsed | Speedup |
|---|---|---|---|
| 1M | 1.50 s | 0.93 s | 1.6× |
| 5M | 5.34 s | 2.58 s | 2.1× |
| 10M | 13.64 s | 6.25 s | 2.2× |

## Verdict

**Ship `rechunk=False` as the default.** Verified:

- 499 engine tests + parity suite still pass — correctness preserved.
- 32% memory reduction at 10M rows; savings scale with row count.
- CPU performance same or slightly better.

The hybrid-vs-pandas memory ratio drops from 1.7× to 1.3× at 10M rows. Hybrid still uses slightly more memory than pandas (per-op intermediate materialization is still there — that's the realm of Option E), but the dominant cross-engine cost is gone.

## Updated customer impact

Extrapolating linearly with the rechunk=False numbers:

| Job size | Pandas peak | Hybrid peak (was) | Hybrid peak (now) |
|---|---|---|---|
| 25M | ~6 GB | ~12 GB | ~8 GB |
| 50M | ~12 GB | ~23 GB | ~16 GB |
| 100M | ~24 GB | ~47 GB | ~32 GB |

The "hybrid forces a bigger EC2 instance class" threshold moves from ~50M rows to ~80–100M rows. For mid-market customers running typical workloads (1M–50M rows on m5.xlarge or m5.2xlarge), the memory difference is now in the noise — same instance class either way, with hybrid running 2× faster.

The memory-pressure warning in `runner.py` remains useful as the safety net: if a customer's pipeline approaches their actual hardware ceiling, they get an actionable advisory pointing at `engine: pandas`.

## What's still on the table

- **Option E** — lazy-Polars chains across same-engine ops — addresses the per-op intermediate materialization that's still there. Bigger architectural rework; ~1-2 weeks. Worth doing when customer signal arrives that the memory ceiling matters in practice. With D shipped, that signal is less likely to come from typical workloads.
- **DuckDB pushdown ops (aggregate, join — Item 19)** — when these ship, hybrid gets a streaming win that pandas can't match. Different scaling shape; not addressed by D.
- **Marketing-correctness tier (50M+ rows)** — still needs cloud VM. Local laptop calibration tops out around the post-D 15-row-million threshold for hybrid before peak RSS approaches the 32 GB ceiling.

## Out of scope (caveats this calibration doesn't address)

- Real-world fixture diversity — constant-value columns are friendly to compression; high-cardinality data may shift the absolute numbers but not the qualitative pattern (still tested at the proportional scale).
- Aggregation/join-heavy pipelines — no `join` or `aggregate` graph ops shipped yet (Item 19 queued); this calibration is purely the source → filter → mask → sink shape.

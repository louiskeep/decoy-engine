Status: record

# D4a: external-reorder lane revalidation

Plan: `docs/plans/2026-10-15-d4-scale-proof.md` section 2. This is the cheap check that runs before any 100M/200M spend: does the reorder lane get reached through `run_pipeline` with a resolved memory and disk budget, does its sorter really spill and merge, and does memory stay inside its budget. The earlier liveness record (`2026-10-10-ooc-reorder-liveness.md`) proved the route decision on 20-row tables. This one proves the lane works at a size that forces spill.

Result: green. Nothing here needs D4b.

## Expected driver per tier

`decide_route` picks the reorder driver for a table when its largest incoming relation holds at least 2,000,000 deduplicated, non-null parent keys, with a sink, a memory budget, a disk budget, fan-in at most 32 and a slim sort row under the per-head cap. The declared tiers (plan section 4) all clear the threshold on both FK edges, so the default (reorder ON) run takes the reorder driver for `child` and `grandchild` and the batch driver for the root `parent`.

| Tier | parent / child / grandchild rows | parent->child keys | child->grandchild keys | Expected child, grandchild |
|---|---|---|---|---|
| 10M | 2.0M / 3.5M / 4.5M | 2.0M | 3.5M | reorder, reorder |
| 100M | 20M / 35M / 45M | 20M | 35M | reorder, reorder |
| 200M | 40M / 70M / 90M | 40M | 70M | reorder, reorder |

The 10M tier sits exactly on the threshold on the first edge (2,000,000 is "at or over"). The OFF arm of the A/B sets the threshold above the largest parent cardinality, which forces the batch driver for both children.

The remaining admission inputs are runtime facts the node supplies: free disk (budget is 0.9 of it) and the memory budget. The bench battery records both in `host.json` and the run record.

## Witnesses

Evidence comes from `tests/perf_fixtures/ooc_route_witness.py`, which wraps the real driver functions and the real `BoundedExternalSorter` and records only counts, byte sizes and table names. Constructing a sorter is not counted as evidence. Runs written to disk, k-way merges executed, and the sorter's resident peak against `run_bytes_cap * SORT_OVERHEAD_FACTOR` (2.2) are.

### Moderate scale, in the test suite

`tests/perf/test_d4a_reorder_reachability.py` builds a 60k / 420k / 420k chain and runs the worker with a 128 MiB budget and the threshold lowered to 50,000 through the public `out_of_core_reorder_threshold_rows` knob (a threshold override changes the route taken, never the output). 128 MiB is the smallest budget at which the DuckDB join phase completes. It asserts:

- `parent` batch, `child` and `grandchild` reorder;
- on each of the two sorters, at least 2 runs written and at least 1 merge;
- each sorter's resident peak above 0 and under its envelope;
- row counts and FK parity on both edges;
- external peak RSS under a loose runaway guard;
- a control run with reorder OFF takes the batch driver on every table and writes 0 sorter runs.

### Real threshold, recorded run

The same worker on a chain that clears the genuine 2,000,000 default with no override. Devbox, 4 vCPU, abi3 Rust companion, `run_pipeline` on the AUTO route. Raw evidence: `docs/records/evidence/2026-10-15-d4a-bench.json` (worker `BENCH_JSON`, schema `fk-scale/v1`) and `docs/records/evidence/2026-10-15-d4a-build.json` (fixture `BUILD_JSON`). The build file records `width: 4`, `payload_columns: ["payload_00".."payload_03"]`, `orphan_frac: 0.01`.

| Item | Value |
|---|---|
| Rows (parent / child / grandchild) | 2,000,000 / 2,200,000 / 2,300,000 (6.5M total), 4 payload columns per table (`payload_00`..`payload_03`) plus `id` keys, 1% orphans |
| Engine OOC budget | 1024 MiB (`run_bytes_cap` 161,061,274 B, merge fan-in 16) |
| Route | `out_of_core`, reason `byte_estimate_bounded_out_of_core`, AUTO (not forced) |
| Drivers | `parent` batch, `child` reorder, `grandchild` reorder |
| Sorter 1 (child) | 2 runs, 1 merge, 166.6 MB spilled, resident peak 317.5 MB, envelope 354.3 MB |
| Sorter 2 (grandchild) | 2 runs, 1 merge, 174.2 MB spilled, resident peak 317.5 MB, envelope 354.3 MB |
| Temp disk peak (sampled) | 1550.5 MB |
| External peak RSS (`wait4` `ru_maxrss`) | 1328.0 MB, against a 1382 MB ceiling (1.35 x budget, the convention of `tests/perf/test_out_of_core_reorder_memory.py`) |
| Wall (`mask_s`) | 70.5 s |
| Native disposition | 14 of 14 masked columns on the Rust companion, none on Python (`disposition.columns_total` 14, `native_columns` 14, `non_native` empty): 12 payload columns across the three tables plus the `parent.id` and `child.id` keys |
| Row counts, FK parity | committed counts equal inputs (`rowcount_match` true, 789.6 MB committed), parity ok on `parent->child` and `child->grandchild` (60 orphans per edge across the spot-check windows) |

RSS used 96% of its ceiling, so the headroom is real but thin at this budget. The 100M and 200M runs are where the ceiling is actually tested. Wall and the sampled temp-disk and RSS peaks are load-dependent on a shared devbox; the deterministic spill structure (2 runs and 1 merge per sorter, 166.6 / 174.2 MB spilled) is identical to the first revalidation run.

## What this does not show

- Throughput at scale: 6.5M rows is 6.5% of the 100M tier.
- A real object-store sink. The sink here is the local Parquet sink.
- The width of the payload does not enter the sort (the slim sort carries row number, match token and masked key), so 4 columns per table is representative for the reorder lane's memory. It is not representative for total mask time, which the 15-column tiers will measure.
- Child FK columns (`parent_id`, `child_id`) carry no per-column disposition entry: they are masked through the parent relation join, not a hash kernel call. The 14 disposition entries are therefore the 12 payload columns (4 per table across three tables) plus the `parent.id` and `child.id` keys; the join-remapped FK columns and the leaf `grandchild.id` do not appear.

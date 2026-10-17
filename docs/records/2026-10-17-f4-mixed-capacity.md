Status: record

# F4 mixed generate + mask capacity: 1M and 10M light tiers (2026-10-17)

Plan: `docs/plans/2026-10-17-phase-f-f4-capacity.md`. Raw evidence:
`decoy-platform/docs/product/release-1-validation-runs/2026-10-17-f4-mixed/summary.json`.
Harness: `scripts/fk-scale/bench_worker_mixed.py` (+ `_mixed_layout.py`), tests in
`tests/perf/test_f4_mixed_capacity_{smoke,scale}.py`.

Host: devbox, 4 vCPU, 12 GiB RAM, `native_threads=1`, Rust companion present. Job: a
generated parent `people` (int sequence key + categorical) and a masked child `accounts`
(int FK, 4 hashed string columns, 1 redacted column) over one `preserve` FK edge, 1%
orphan child rows, `ParquetTransactionalSink`. Production `run_pipeline`, `auto` mode.

| Tier | Parent / child | Peak RSS (external) | mask_s p50 / p95 | rows/s |
|---|---|---|---|---|
| 1M | 0.2M / 0.8M | 849 MB (761-849 over 3 runs) | 5.77 / 5.80 s | ~174k |
| 10M | 2M / 8M | 2009 MB (1997-2009 over 3 runs) | 56.64 / 56.92 s | ~176k |

Both are well under the proposed 8192 MB ceiling, and both completed with route evidence
(`execution_mode=mixed`, streamed sink, empty `result.outputs`, 16 / 160 chunks, all four
hashed columns on `rust_companion`, `note`/FK passthrough on `arrow_python`) and FK parity
ok (full FK column scan: 0 mismatches, 8,000 / 80,000 orphans as expected; payload and
parent checks are sampled head/middle/tail windows).

Distinct diagnostics (10M): generation phase RSS 307 MB, parent Arrow table 37.7 MB;
resident child-key + resolved-key arrays 122 MB; temp disk 0 MB, staged output peak 585 MB.

Oracle byte-identity ran at 1M only, in its own process (peak 1966 MB, not a capacity
figure): production output equals the `full_frame` oracle on canonical schema and Arrow
IPC value bytes. 10M is capacity-only.

## Finding for the 100M step

The FK key resolve (`_build_carrier`, Python tuple/dict path in `_mixed_fk_bridge`) is the
dominant cost and the dominant transient. At 10M it takes 17 s of the 57 s run and lifts
the process high-water mark from about 424 MB (after generation) to about 2.0 GB, then
settles at 579 MB. The Arrow carrier itself is only 122 MB, so the spike is Python objects
built during the resolve, and it scales with child rows. A linear extrapolation to 100M
is roughly 20 GB transient, which would exceed the proposed 8 GiB ceiling. That is an
extrapolation from two points, not a measurement. The deferred 100M run decides it; a
chunked or Arrow-native resolve is the likely remedy if it misses.

Deferred and not claimed: 100M (parent 20M / child 80M), the GCP battery case, 200M.

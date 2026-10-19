Status: record

# F4a: lower-tier generation capacity evidence (2026-10-18)

Program plan: `docs/plans/2026-10-16-phase-f-program.md` section F4, prerequisite (a). This covers
the local 1M and 10M tiers only. The 100M tier and the platform evidence transport are separate
slices. Raw numbers: `docs/records/evidence/2026-10-18-f4a-gen-capacity.json`. Reproduce with
`tests/perf/test_f4a_gen_capacity.py` (`-m benchmark`; about 33 minutes).

Substrate: `origin/main` 28ddfb5f (FR0 + FR1; FR3 shelved). Companion venv with the Rust extension.
Host: 4 cores, 12 GB. Peak RSS is `wait4` `ru_maxrss` of the supervised worker. Each tier ran twice.

## Plans measured

Pure generation (`orders` at tier rows, `people` fixed at 100k): sequence, reference, categorical,
categorical with `null_probability`, pooled faker (`city`), categorical ISO-date anchor, and a
`windowed_date` column on that anchor. Streamed into a Parquet sink under a 1024 MB budget, batch
32768.

Mixed job: the production `run_pipeline` mixed route. Generated parent `people` (200k rows;
sequence, categorical, pooled faker, anchor, windowed_date) plus a masked Parquet child `accounts`
(tier rows; passthrough FK, 4 hashed columns, 1 redacted) over one FK edge.

Decisions to review:
- The windowed_date anchor is a categorical of ISO dates, not `date_of_birth`: that faker is not
  pool-eligible and runs the live per-row provider, which would make this a Faker-speed bench.
- The mixed parent has no null-masked column. A generated parent holding nulls makes the mixed
  route decline (`generation-shape-unsupported`) and fall to the resident full-frame path, which
  is unbounded and grows with row count (an un-committed smoke run, not in the evidence JSON, saw it
  climb into multiple GB and get killed at the 10M tier). Lifting that decline is F2 scope. Null
  masking is covered by the pure-generation plan.
- Content digests are computed in a separate process after the measured run. Reading the 10M-row
  output back inside the worker added 230 MB to its RSS and would have been charged to generation.

## Predeclared ceilings (set before the 10M runs, not changed after)

| Job | Ceiling | Growth bound 1M to 10M |
|---|---|---|
| Pure generation | 500 MB | peak at most 1.25x (flat) |
| Mixed | 1500 MB | job memory (peak minus pre-job baseline) at most 10x * 1.15 |

## Results

| Job | Tier | Peak RSS (MB) | Wall (s) | Rows/s | Digest rerun |
|---|---|---|---|---|---|
| Generation | 1M (+100k parent) | 210.6 / 211.1 | 74.8 / 74.1 | ~15.0k | identical |
| Generation | 10M (+100k parent) | 226.1 / 227.1 | 721 / 731 | ~13.9k | identical |
| Mixed | 1M child (+200k parent) | 840.7 / 840.8 | 25.8 / 25.3 | ~59k | identical |
| Mixed | 10M child (+200k parent) | 1435.5 / 1435.4 | 86.8 / 87.1 | ~154k | identical |

All four tiers are under their ceilings. Row counts and FK parity checks passed.

- Pure generation is flat: 1M to 10M raised the peak 1.07x.
- Mixed is bounded but not flat: 1.71x on peak, and the job's own memory (peak minus the
  pre-job baseline of about 215 MB) rose from about 625 MB to about 1220 MB for 10x rows, roughly
  0.07 MB per 1k child rows. The mixed route keeps child and resolved FK keys resident, so this is
  expected, not a leak. A linear extrapolation to a 100M child is about 7 GB. F4's 100M mixed
  tier must plan for that, or the carrier must be bounded first.

## Per-column executed backend

From the F0 witness on the same table definitions at 100k rows (above the 50k pooled-faker
threshold), because the witness keeps every value and cannot ride a streamed 10M run. Backend
follows the plan, not the row count.

| Column kind | Executed backend | Rust companion |
|---|---|---|
| sequence | no selection (arithmetic) | no |
| categorical, reference | Python RNG selection | no |
| pooled faker | numpy selection (pool built once) | no |
| null mask | post-process on the column | no |
| windowed_date | shared `apply_windowed_date`, outside the witness taxonomy; populated, admitted | no |

Mixed mask half (F2's own evidence, 200 chunks at 10M): hash columns ran on `rust_companion`;
passthrough and redact ran `arrow_python`.

So no generation column runs on the Rust companion on this substrate. That is the expected state
with FR3 shelved, and it is why generation is slow: `windowed_date` is about 87 percent of
generation time (per-row HKDF derive plus format in Python, profiled at 150k rows).
Pure generation throughput is therefore about 14k rows/s, and 100M would take about 2 hours.

## Not covered

100M tier; platform-side evidence transport; the digest is bound to the sink's row-group layout, so
it proves rerun determinism, not equivalence between layouts (route independence is FR0's tests).

## Follow-ups (from the F4a dennis gate, non-blocking)

- The mixed `_mixed_layout.py` disposition read assumes the chunked route was taken; a mixed run
  below the engine auto-chunk threshold KeyErrors (it fails loudly, worker returncode 3, not
  silently, and the committed tiers at 1M+ child always chunk). Guard the key access with a clear
  "route did not chunk at this row count" diagnostic before this harness runs at smaller tiers.
- The mixed absolute ceiling (1500 MB) runs at ~95.7% utilization at 10M. On-host reruns were
  extremely stable (1435.5 / 1435.4 MB), so the thin headroom is fine on the stated 4-core / 12 GB
  box, but a noisier host could flake. Revisit the headroom if this bench is ever run elsewhere. The
  secondary superlinear-growth gate for mixed has wide headroom (~11.5x bound vs 1.71x observed); the
  absolute ceiling is the real mixed gate.

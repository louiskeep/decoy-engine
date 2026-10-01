# Stage 2a: first probe batch, engine-direct entry point

Status: record (input to `docs/plans/2026-09-30-rust-coverage-evidence-audit.md`)

Twenty-four cells, run sequentially in the dedicated stage 2a environment
(`docs/records/audit-2026-09-30/environment.json`), one fresh interpreter per
cell (`scripts/audit/run_cells.py` -> `scripts/audit/probe.py`). Raw records:
`docs/records/audit-2026-09-30/runs.jsonl` (R001-R024). Every cell used the
engine-direct entry point (`decoy_engine.execution.run_pipeline`) with real
data, local Parquet unless the row states otherwise. `explain_plan=True` was
set on every call for the extra `quality_metrics["execution_plan"]`
diagnostic; this is a report-only flag and does not change routing or output
bytes.

Environment: Python 3.11.15, engine commit `7cadcde0` (branch
`docs/reality-2026-09-30`, on top of pinned baseline `8dc559e5`), platform
commit `0701a954` (origin/main, detached worktree), CLI commit `b8274b01`
(origin/main, referenced only, not installed). `decoy-engine-native` built
from this worktree's `decoy-engine-native/` via maturin:
`native_companion_status()` reports `present=True, ok=True, abi
decoy-native-abi-2, version 0.1.0`. Companion module SHA-256:
`1a3b2d18e4e092691e05f7ab0c8e1725f6aeb94bc19e46f9adaced472510b061`.

A note on `execution_mode`: `quality_metrics["execution"]["execution_mode"]`
names the FK-routing macro-route (`full_frame` / `sequential` /
`out_of_core`), not whether the mask step internally chunked. Every cell
below shows `full_frame` there, including R019 and R022, which DID chunk
internally -- that fact lives in `quality_metrics["auto_chunk"]["mode"]`
instead. Two separate questions, two separate keys.

## Results table

| Cell | Rows | Backend | Route | Auto-chunk mode | Per-node evidence | Parity vs pandas oracle | Wall (s) | Peak RSS (VmHWM) |
|---|---:|---|---|---|---|---|---:|---:|
| R001 hash | 10,000 | rust_companion | full_frame | (default, not stamped) | native_keyed_hash: rust | match | 0.085 | 233 MB |
| R002 categorical (det=True) | 10,000 | rust_companion | full_frame | (default) | native_categorical: rust | match | 0.055 | 201 MB |
| R003 categorical (det=False) | 10,000 | pandas | full_frame | (default) | declined, no binding | n/a (already pandas) | 0.048 | 200 MB |
| R004 bucket_perturb | 10,000 | rust_companion | full_frame | (default) | native_bucket_perturb: rust | match | 0.069 | 213 MB |
| R005 group_key (+ passthrough sibling) | 10,000 | mixed | full_frame | (default) | native_group_key: rust; native_passthrough: arrow/py | match | 0.071 | 211 MB |
| R006 date_shift | 10,000 | rust_companion | full_frame | (default) | native_date_shift: rust | match | 0.068 | 205 MB |
| R007 redact | 10,000 | arrow_python_native | full_frame | (default) | native_redact: arrow/py | match | 0.047 | 211 MB |
| R008 truncate | 10,000 | arrow_python_native | full_frame | (default) | native_truncate: arrow/py | match | 0.049 | 217 MB |
| R009 passthrough | 10,000 | arrow_python_native | full_frame | (default) | native_passthrough: arrow/py | match | 0.032 | 201 MB |
| R010 faker (pooled) | 10,000 | pandas | full_frame | (default) | declined (operator outside the 8) | n/a | 0.085 | 227 MB |
| R011 fpe | 10,000 | pandas | full_frame | (default) | declined | n/a | 1.081 | 224 MB |
| R012 text_mask | 10,000 | pandas | full_frame | (default) | declined | n/a | 1.038 | 229 MB |
| R013 mix (hash+redact+truncate+passthrough), Parquet | 10,000 | mixed | full_frame | (default) | hash: rust; redact/truncate/passthrough: arrow/py | match | 0.162 | 253 MB |
| R014 mix + one faker column, Parquet | 10,000 | pandas | full_frame | (default) | declined (whole table, per B028) | n/a | 0.287 | 252 MB |
| R015 mix, CSV (pandas `read_csv(dtype=str)` -> Arrow) | 10,000 | mixed | full_frame | (default) | same as R013 | match | 0.095 | 232 MB |
| R016 mix, fixed_width (`read_fixed_width`) | 10,000 | mixed | full_frame | (default) | same as R013 | match | 0.126 | 230 MB |
| R017 mix + `when` gate | 10,000 | pandas | full_frame | (default) | declined (whole table, per B017) | n/a | 0.168 | 243 MB |
| R018 hash only, 50k, default | 50,000 | rust_companion | full_frame | (not stamped: full_frame, default) | native_keyed_hash: rust | match | 0.291 | 264 MB |
| R019 hash only, 150k, default | 150,000 | pandas | full_frame (macro) | chunked | none (see finding 2) | n/a | 1.662 | 266 MB |
| R020 hash only, 150k, `auto_chunk=False` | 150,000 | rust_companion | full_frame | full_frame (forced) | native_keyed_hash: rust | match | 0.474 | 320 MB |
| R021 hash only, 1M, `auto_chunk=False` | 1,000,000 | rust_companion | full_frame | full_frame (forced) | native_keyed_hash: rust | match | 3.350 | 838 MB |
| R022 hash only, 1M, default | 1,000,000 | pandas | full_frame (macro) | chunked | none (see finding 2) | n/a | 10.790 | 457 MB |
| R023 generate only (Faker) | 10,000 | pandas (generate path) | full_frame | n/a | n/a, no mask node | n/a | 0.548 | 180 MB |
| R024 mask + generate | 10,000 | pandas | full_frame | (default) | declined (per B007, generate+mask) | n/a | 0.666 | 227 MB |

Parity: for every cell that ran a non-pandas backend, the same config and
resident source were re-run with `unified_slice_enabled=False` (the pandas
oracle) in the same process, and outputs were compared column-by-column
(values, Arrow type, schema, row count). All ten checked cells matched
exactly. Cells already on the pandas path have nothing to compare against
themselves, so parity is marked "n/a" there, not "failed".

## Surprises

1. **The preflight's "companion absent" finding does not describe the
   product; it described one borrowed `.venv`.** In this dedicated
   environment (companion built from this worktree's pinned HEAD), every
   hash/categorical(deterministic)/bucket_perturb/group_key/date_shift cell
   ran the compiled Rust kernel (`compiled_kernel_executed=True`). The
   ledger's B039 row has been corrected accordingly.

2. **The production auto-chunk route never reaches the compiled chunked
   dispatch (`execution.native._dispatch`), even though that dispatch layer
   exists, is tested, and includes Faker pool-select evidence
   (`pool_select_executed`).** `run_pipeline`'s chunked path always calls
   `execution._chunked.run_mask_pipeline_chunked`, the pandas-oracle-only
   chunk masker. R019 and R022 (150k and 1M hash-only, default routing, both
   chunked) show `overall_backend: pandas` with no node-level route evidence
   at all. Grep across this engine tree, `decoy-platform`, and `decoy-cli`
   turned up no caller of `run_native_or_oracle_chunked` or
   `NativeOrOracleChunkedAdapter` outside `execution/native/`,
   `execution/physical/` (where they are defined), and test files. This is a
   built-but-unreachable capability: real code, real tests, zero production
   callers. See the ledger's new "Stage 2a run-stage findings" section and
   the note added atop ledger section 9.

3. **The auto-chunk route is also slower, not just less native.** R021 (1M
   rows, forced full-frame, Rust) ran in 3.35s; R022 (same data, default
   routing, which auto-chunks at the 100k threshold) ran in 10.79s -- about
   3x slower (measured), on top of losing Rust coverage entirely. Auto-chunk exists to
   bound memory on large jobs, and R022's peak RSS (457 MB) is indeed lower
   than R021's (838 MB), so the trade is real, but today's engine-direct
   default silently pays both the memory-safety cost's intended price AND an
   avoidable throughput and Rust-coverage cost that a wired-up compiled
   chunked dispatch would remove.

4. **`when` gates are not expressible through the schema at all.**
   `PipelineConfig.model_validate` rejects a `when` key on a column
   (`extra_forbidden`); the R017 cell required injecting `when` into an
   already-validated config dict and calling `run_pipeline` directly. This
   matches the ledger's own pre-existing note on B049, now also confirmed at
   the unified-slice admission layer (B017) with a real run.

5. **Not a surprise, but a confirmation worth stating plainly**: Arrow/Python
   native kernels (passthrough, redact, truncate) show
   `compiled_kernel_executed=False` even when they ran inside the unified
   slice with `executed=True`. That is correct per the plan's own
   classification rule, not a gap -- flagged here only because it is the
   exact shape a careless read would mistake for "declared native but not
   actually native."

## Ledger ids witnessed this pass

B001, B007, B008, B017, B023, B024, B026, B027, B028, B029, B031 (admit case
only), B035 (admit case only), B039 (correction), B040 (admit case only),
B054, B072. Full detail and exact wording is in
`docs/records/audit-2026-09-30/branch-witness-ledger.md`'s per-row Witness
column and its two new correction notes (after section 1's original
correction, and atop section 9).

Not witnessed this pass (left for a later stage, per the plan's "later
stages cover the rest"): every decline-only fixture this batch did not
construct (duplicate columns, malformed schema metadata, an int64 column
bound to redact/truncate, a group_key order-dependence conflict, a
null-bearing int hash column, FK/relationship routing, out-of-core, the
platform admission/claim/dispatch layers, and the CLI entry points).

## What could not be done in this pass

- The adaptive-scheduler entry point (4) and any cgroup-scoped peak-memory
  read need an integration host (no supervisor socket, no cgroup
  delegation on this devbox; unchanged from preflight).
- The CLI, platform full-frame wrapper, claim -> worker, and subset entry
  points (2, 3, 5, 6) were not exercised this pass; stage 2a's scope was
  engine-direct only, per the plan's step 3.
- The 100M-scale claim stays "route inferred; capacity unproven" -- this
  batch's largest real run was 1,000,000 rows, per the devbox memory cap.

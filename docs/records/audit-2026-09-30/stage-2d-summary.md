# Stage 2d: remediating the reviewer's evidence gaps

Status: record (input to `docs/plans/2026-09-30-rust-coverage-evidence-audit.md`)

Runs R069-R084 (plus R085, added after the dennis re-gate: default `deterministic: false` Faker, 0 compiled calls) in `docs/records/audit-2026-09-30/runs.jsonl`, continuing from stage 2c's
R068, in the same dedicated environment (`docs/records/audit-2026-09-30/environment.json`):
engine editable from this worktree (commit `7cadcde08ea5876217de2490b06a7f9e1ced5a40`, no
`src/` change since; companion present, ok, `decoy-native-abi-2`, SHA-256
`1a3b2d18e4e092691e05f7ab0c8e1725f6aeb94bc19e46f9adaced472510b061`), platform installed
from the detached worktree at `origin/main` `0701a95422e405cff76eb38c12cfb4f372b151d4`,
CLI installed from a detached worktree at `origin/main`
`b8274b0194748d5a60262b78b5969a8c85aeeac7`. New scripts under `scripts/audit/`:
`stage2d_faker_compiled.py`, `stage2d_native_dispatch.py`, `stage2d_fk_backend.py`,
`stage2d_cli_per_node.py`, `stage2d_platform_per_node.py`, `stage2d_cloud_full_path.py`.
One fresh subprocess per cell; VmHWM read from `/proc/self/status` where memory mattered.

## Task 1: Faker compiled selection (reviewer blocker B1)

**Question:** does the production pandas Faker strategy (`FakerStrategyHandler.run`,
`execution/_strategies/_faker.py:86`, `PoolSampler().sample(...)`) actually reach the
compiled `derive_index_batch` kernel (`generation/pool/_sampler.py:48-63` selects it,
`:119-152` calls it), on every backend that runs it?

**Method:** instrumented the compiled-kernel selector inside the probe subprocess only
(monkeypatched `decoy_engine.generation.pool._sampler._compiled_index_kernel` with a
counting proxy around the real kernel object's `derive_index_batch`; never edited `src/`).

**Results (real counts, not inferred):**

| Cell | Shape | Backend / route | `derive_index_batch` calls |
|---|---|---|---:|
| R069 | R010's spec: pooled Faker, 10k rows | pandas, full_frame | 1 |
| R070 | R014's spec: native mix + 1 Faker column, 10k rows | pandas, full_frame | 1 |
| R071 | chunked Faker, 150k rows, default routing | pandas, chunked (3 chunks) | 3 (one per chunk) |
| R072 | R023's spec: generate-only, 10k rows | generation path, no mask node | 0 |
| R073 | FK tree, Faker payload column, forced sequential | sequential, `pure_mask_fk` | 1 |

**Answer:** yes, on every masking backend. R069, R070, R071, and R073 all show the
compiled kernel firing, even though every one of those cells is classified `overall_backend:
pandas` (or `sequential`) by the plan's own "Rust" rule (Faker is never admitted to the
unified/Rust slice, B028) -- Faker masking gets a real compiled kernel underneath a table
the map correctly never calls "Rust." R072 confirms the mirror claim from the ledger's
section 10 with a run, not just a grep: generation's own Faker path never touches the
compiled kernel at all (0 calls). Ledger: new section 11a (B313-B318).

## Task 2: per-node evidence for "Rust" cells that lacked it (H1)

**R029 (platform full-frame wrapper) re-run (R082):** same fixture (hash-only, 5k rows,
Rust-admitted, `api.jobs.v2_runner.run_v2_pipeline`). `unified_slice_activation.nodes` now
carries `{"t:h:scalar:hash": {"operator": "native_keyed_hash", "executed": true,
"compiled_kernel_executed": true}}` -- real per-node evidence, where R029's original
stage-2b record only had the table-level `unified_slice_activated: true` flag.

**R036/R037 (CLI default and `--native`) re-run (R080, R081):** ran the `decoy` CLI's
real `run` Typer command in-process (`typer.testing.CliRunner`, `decoy.__main__.app`),
with `decoy_engine.run_pipeline` monkeypatched (inside this subprocess) to capture the
`ExecutionResult` the CLI itself receives and then discards. Both R080 (default) and R081
(`--native`) show the SAME engine-level per-node evidence as R082:
`{"t:h:scalar:hash": {"operator": "native_keyed_hash", "executed": true,
"compiled_kernel_executed": true}}`, read from `quality_metrics["unified_slice_activation"]`
directly, not from the CLI's own `classify_route` label (`_native_gate.py`'s
`state`/`label`, B200, which only proves "at least one node was native," not which one or
with what evidence). The CLI's own label and the engine's per-node evidence agree in both
cells, but the label is not proof; the per-node record is.

## Task 3: settle the approach question with a run (H4)

**Question:** what actually happens when `decoy_engine.execution.native.run_native_or_oracle_chunked`
is called directly -- the compiled chunked dispatch stage 2a proved has zero production
callers -- on (a) a 1M-row hash+redact+truncate+passthrough job and (b) the same plus one
categorical column, at `native_threads=1` and `4`?

**Results:**

| Cell | Shape | Threads | `native_admitted` | `reroute_reason` | Native wall | Oracle wall | Byte parity | VmHWM |
|---|---|---:|---|---|---:|---:|---|---:|
| R074 | (a) hash+redact+truncate+passthrough | 1 | True | -- | 1.92s | 11.23s | match | 599,544 KB |
| R075 | (a) same | 4 | True | -- | 1.27s | 10.69s | match | 594,116 KB |
| R076 | (b) + categorical | 1 | False | `categorical_not_native_chunked_route:c` | 20.83s | 20.17s | match | 665,320 KB |
| R077 | (b) same | 4 | False | `categorical_not_native_chunked_route:c` | 20.97s | 20.02s | match | 674,128 KB |
| R084 | (a) + a deterministic pooled Faker column | 1 | True | -- | -- | -- | -- | -- |

(a) admits and runs natively at both thread counts (`compiled_kernel_executed=True`,
`kernel_calls={"hash": 5, "redact": 5, "truncate": 5, "passthrough": 5}` for the 5-chunk,
200k-chunk-size run), measured 5.9x-8.4x faster than the same chunks run through the pandas oracle
in the same process. (b) reroutes the WHOLE table to the oracle on the categorical column
specifically (`CHUNKED_ROUTE_VETOED_STRATEGIES`, B161) -- confirmed by a real run, not a
code read: `native_admitted=False`, every node's route downgraded to `oracle`,
`compiled_kernel_executed=False`. R084 (a bonus cell, not in the original task list, added
to witness the Faker pool-select path this same dispatch also owns) shows a deterministic
pooled Faker column admits alongside the four scalar strategies: `node_routes` includes
`{"column": "nm", "strategy": "faker", "route": "native_pool"}`, `pool_select_executed=True`,
`pool_select_calls=3` (one per chunk). Every cell's output byte-parity against the pandas
oracle (`run_mask_pipeline_chunked`, run separately on a fresh copy of the same chunks in
the same process) matched exactly: same column names, same Arrow types, same values, same
row counts.

**Missing parameters, compared with the two real production call sites:**

`run_native_or_oracle_chunked`'s full parameter list: `config, chunks, *, table,
engine_version, key_provider=None, route_evidence_sink=None, pool_cache=None,
native_threads=None`.

- `execution/_pipeline_route_exec.py:570-586` (the engine's OWN internal chunked caller)
  calls `_chunked.run_mask_pipeline_chunked(config, _slices(), table=table,
  engine_version=engine_version, registry=registry, adapter=adapter,
  vault_writer=vault_writer, chunk_result_sink=chunk_results, key_provider=key_provider)`
  -- passing `registry`, `adapter`, `vault_writer`, and `chunk_result_sink`, none of which
  `run_native_or_oracle_chunked` accepts.
- The platform's Phase 1 caller (`decoy-platform@origin/main:api/jobs/v2_runner.py:404-419`,
  `_run_v2_pipeline_streaming`) calls `run_mask_pipeline_chunked(seeded_cfg, batches,
  table=table, engine_version=engine_version, vault_writer=vault_writer, **chunked_kwargs)`
  where `chunked_kwargs` conditionally adds `base_row_offset` -- passing `vault_writer` and
  conditionally `base_row_offset`, neither accepted either.

So wiring either real caller onto this dispatch today would need it to grow at least
`vault_writer` (both callers pass it) and, depending on which caller, `registry`, `adapter`,
`chunk_result_sink`, and `base_row_offset` -- none of which exist on its signature now.

**Output schema question:** NOT guaranteed identical without caller reconciliation, per the
engine's own test suite. `tests/parity/native/test_phase2_gate.py`'s own docstring
("Output-schema reconciliation (carry-forward #4)") documents that native emits a stable
Arrow type per strategy across every batch shape, while the pandas oracle's
`pa.Table.from_pandas` round-trip infers a DIFFERENT type for two specific degenerate
shapes (an all-null column -> null-type; a zero-row batch -> float64, pandas' empty-column
default) -- the test suite explicitly allowlists exactly these two type-pairs
(`EMPTY_DOUBLE_NORMALIZATION`, `_GATE_ALLOWED_DIFFS`) before comparing, rather than treating
raw schema equality as the bar. On real, non-degenerate 1M-row data (this task's own R074 and
R075; R084 is 150k rows), schemas and values matched the oracle exactly with no reconciliation needed;
the gap is specific to those two degenerate shapes, and no caller anywhere applies the
reconciliation the test suite needs, because no caller invokes this function at all.

Ledger: B161, B168, B169 now carry real witnesses (was: code-inferred). Section 9's intro
note updated to describe the direct-call evidence.

## Task 4: cloud test, full path (M3)

**Question:** does the FULL production submission entry point
(`api.jobs.v2_submission.run_v2_pipeline_from_config`), not just `validate_v2_config` in
isolation, reject a `resolve_binding`-shaped S3 descriptor before any cloud client is
built?

**Method:** stored the same dirty S3 descriptor stage 2b's R025 used (engine-valid fields
plus the platform's own `connection_id`/`connection_name` provenance keys) in a
stored-job-style config dict, and called `run_v2_pipeline_from_config` directly with a
minimal stand-in `job`/`db` (only `job.trigger_detail` is read, and only after validation
succeeds, so a stand-in is sufficient for this input), `boto3.client` monkeypatched to
raise if constructed.

**Result (R083):** `PipelineConfigError`, exactly:

```
V2 pipeline config failed validation:
2 validation errors for PipelineConfig
sources.t.s3.connection_id
  Extra inputs are not permitted [type=extra_forbidden, input_value=42, input_type=int]
sources.t.s3.connection_name
  Extra inputs are not permitted [type=extra_forbidden, input_value='audit-connection', input_type=str]
```

`no_cloud_client_proven: true` -- `boto3.client` was never called. This confirms, through
the real entry point a job submission actually calls (`api/jobs/runner.py`), the same
rejection stage 2b already proved at the `validate_v2_config` layer directly: the failure
happens at the FIRST call inside `run_v2_pipeline_from_config`, before source resolution,
existence checks, or the orchestrator ever run.

**Aside, not a claim about the code's correctness:** importing `api.jobs.v2_submission`
(or `api.jobs.v2_orchestrator`) as the first import in a fresh interpreter raises
`ImportError: cannot import name 'run_v2_pipeline_from_config' from partially initialized
module` -- a real circular-import fragility between `v2_orchestrator.py`, `v2_full_frame.py`,
and `v2_runner.py`'s own late, `# noqa: E402` re-export of `v2_orchestrator`'s names.
Importing `api.jobs.v2_runner` on its own first avoids it. Worth a note for anyone writing
a standalone script against this module; not something this task's scope asked to fix.

## Task 5: FK backend evidence (M6)

**Question:** for one sequential cell and one out-of-core cell, which adapter/kernel
functions actually run the mask step?

**Method:** instrumented, inside the subprocess, `PandasExecutionAdapter.run` and
`PandasExecutionAdapter._dispatch_mask_node` (class-level patch, catches every caller);
`out_of_core/_runner.py`'s and `_stream_driver.py`'s own `mask_batch` bindings;
`_batch_join.py`'s, `_relation.py`'s, `_runner.py`'s, and `_stream_join.py`'s own
`mask_column` bindings (each `from ... import` binds a separate name, so the source
module's attribute alone would not be enough); and `_relation.py`'s and `_mask.py`'s own
`hash_array` bindings (the actual per-value hash kernel `decoy_engine.kernel.hash_array`
calls, imported separately into both).

**Results, same FK tree fixture (2k parent / 8k child rows) forced through each route:**

- **R078 (forced `sequential`):** `PandasExecutionAdapter.run`: **0** calls.
  `PandasExecutionAdapter._dispatch_mask_node`: **10** calls (2 tables x 5 columns each).
  Every `out_of_core/*` counter: 0. **Finding:** `execution/_sequential.py`'s
  `run_sequential` does not call `.run()` at all -- it calls the adapter's own per-node
  dispatch helper directly (`adapter._dispatch_mask_node(...)`, the SAME helper `.run()`
  uses internally for a full-frame job), confirmed by a real call count, not by reading the
  two functions side by side.
- **R079 (forced `out_of_core`):** `PandasExecutionAdapter.run` and
  `._dispatch_mask_node`: both **0**. `_runner.mask_batch`: **2** calls (one per table's
  non-FK-key payload columns). `_mask.hash_array` (the leaf hash kernel `mask_column`
  calls internally for a `hash`-strategy column): **3** calls. Every `mask_column` binding
  and `_relation.hash_array`: **0** (both are REMAP-orphan-policy-only paths; this fixture
  uses `orphan_policy: "preserve"`, so neither fires). **Finding:** OOC never touches
  `PandasExecutionAdapter` at all -- confirmed by a real 0 count on both its methods, not
  an absence of a citation. `decoy_engine.kernel.hash_array` (`kernel/_scalar.py`) is a
  pure per-value Python loop (`derive(seed, namespace, canonical(value)).hex()` in a list
  comprehension, read directly this pass) -- OOC's hash masking, key or payload, never
  reaches the compiled companion, consistent with the Codex independent audit's ranked
  gap #3.

## Task 6: provenance backfill (M2)

Added `docs/records/audit-2026-09-30/provenance-2b.md`: states the environment R025-R042
ran in (same dedicated venv as R001-R024 and R043-R068, from `environment.json`), the
commit timeline from `git log` (stage 2a committed 09:38 UTC, stage 2b 10:04, stage 2c
10:26, all 2026-09-30), and verifies `git diff --stat 8dc559e5 7cadcde0 -- src` is empty
(the four files that differ between the plan's pinned baseline and the commit every run
through R083 used are all under `docs/` and `scripts/audit/`, zero under `src/`). No row
in `runs.jsonl` was edited.

## Task 7: ledger (H2)

Filled all 205 previously-empty Witness cells in `branch-witness-ledger.md` (335 table
rows total after this pass, 0 empty). Rows with a real run from stage 2a-2d got that run's
id; the rest got `code-inferred: read from source only, not witnessed by a run this pass`
-- honest about the fact that no fixture was built for that specific branch, not a claim
of a witness that does not exist. Rows updated with REAL new witnesses this pass (beyond
the generic fill): B161, B168, B169 (task 3's direct-dispatch cells). Added a new
subsection, 11a, for the PoolSampler compiled-kernel switch the plan asked for
(`_sampler.py:48-63` and `:119-152`), six new rows (B313-B318) citing R069-R073 and R072.
Answered four of the six open questions from source reads (the `_chunked_group_key` /
`_chunked_text_mask` / `_chunked_code_set` / `_chunked_bucket_perturb` "unsafe" predicates;
`_mem_estimate.fits`/`raw_data_bytes` and `_probe.probe_peak_bytes`/`probe_fits`/
`MIN_PLAUSIBLE_K_FULL_FRAME`; `out_of_core/_compat.py`'s deferred Group B/C dicts and
`_group_c_conditional_rejection`; `_chunked_fk.gate_fk_child_edges`'s full predicate list,
including correcting its citation from `_chunked.py`, which only calls it, to
`_chunked_fk.py:247`, which defines it). Question 1 was already resolved in a prior pass;
question 6 (platform line-number drift risk) is a standing caveat, not a question with a
one-time answer, and is left as recorded.

## Task 8: fix small errors

- `stage-2b-summary.md`: `api/jobs/v2_submission.py:96` corrected to `:128` (verified:
  `grep -n "validate_v2_config(expanded)"` against the `origin/main` blob).
- `stage-2c-summary.md`: `~95-104s` corrected to `87.4 to 103.9s` (the raw
  `native-threadsweep.log`'s three 8-thread, 100M-row reps show `hash_ms` 87415, 94753,
  and 103880 -- read directly from the (gitignored, locally present) log this pass, not
  estimated); also corrected the adjacent `redact_ms`/`truncate_ms` ranges to the reps'
  actual values (~128-146s / ~159-178s) while there.
- `stage-2c-summary.md:251`'s "single-threaded Python reference" corrected: the 1280s
  hash-kernel baseline is the Phase 0 NATIVE single-thread hash time
  (`docs/plans/native-throughput-phase0-baseline.md`, ~234k rows/s/col, already compiled,
  already 2.9x faster than the pandas oracle, just single-threaded), not a per-row Python
  reference. The 13.5x combines kernel work (1280s to 299.7s at 1 thread, 4.3x) and threading
  (299.7s to 94.8s at 8 threads, about 3.2x); both ends are compiled.
- Noted in `stage-2c-summary.md` that the PR #129 raw `native-threadsweep.log` is
  gitignored (`decoy-platform/.gitignore:16`, a blanket `*.log` rule) and not itself
  committed, so a reader without local access to that file can only verify the numbers
  against this record.
- `codex-independent-audit.md`: removed the stray `489,233` line and the duplicated
  header/revisions block that followed it (both trailing terminal-output artifacts after
  the real ranked-gap table's last row); converted all 34 en-dashes to ASCII hyphens (the
  file had zero em-dashes); rewrote the "swapped" preamble -- the value it labels "decoy
  CLI" (`0701a95422e...`) is in fact this audit's real platform commit, but the value it
  labels "decoy-platform@origin/main" (`e04b2a2f1c8...`) is not a platform commit at all:
  it resolves in the `decoy-engine` repo to the PR #178 merge commit. So it is not a clean
  two-way swap; the platform tree Codex actually read is unverified, not merely mislabeled.

## Task 9: this summary

This document.

## Not done / deferred, with reason

- **Task 3's `native_threads` sweep** did not extend past 1 and 4 (the task's own ask);
  no 2- or 8-thread cells were run.
- **Task 5's REMAP-orphan-policy FK-key masking path** (`_batch_join._batch_remap_values`,
  `_runner._remap_values`) was not separately witnessed -- both cells used
  `orphan_policy: "preserve"`, so neither REMAP-specific `mask_column` call site fired;
  this was noted as a real (0-count) finding rather than papered over.
- **Ledger open question 6** (platform line-number drift if `origin/main` advances) is a
  standing caveat by its own nature, not something a single pass answers once.
- No production code was edited in either repo. No push, no merge, no PR.

## Commit

This stage's commit SHA, its companion SHA-256, and the platform/CLI commits it used are
recorded per-run in `runs.jsonl` (R069-R085) and repeated at the top of this document.

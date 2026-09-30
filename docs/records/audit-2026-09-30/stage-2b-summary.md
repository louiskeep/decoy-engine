# Stage 2b: specific checks + remaining entry points

Status: record (input to `docs/plans/2026-09-30-rust-coverage-evidence-audit.md`)

Runs R025-R042 in `docs/records/audit-2026-09-30/runs.jsonl`, all in the stage 2a
dedicated environment (`docs/records/audit-2026-09-30/environment.json`):
engine editable from this worktree (companion built, present, ok), platform
installed from the detached worktree at `origin/main` `0701a954`, decoy-cli
installed from a new detached worktree (`/home/cam/vscode/decoy/.claude/worktrees/audit-pinned`)
at `origin/main` `b8274b0194748d5a60262b78b5969a8c85aeeac7`. Two extra
dev-only packages (`flask`, `flask_cors`) were added to the throwaway audit
venv to run moto's `ThreadedMotoServer` (the platform declares `moto[s3]`,
not `moto[server]`); no production dependency file was touched. Scripts live
under `scripts/audit/stage2b_*.py`, one fresh subprocess per script, same
VmHWM/JSON-line convention as stage 2a where memory mattered; the cloud and
CLI checks are validation/functional proofs where wall time and memory are
not the point, so several records carry `peak_memory_vmhwm_kb: null` --
noted per cell below.

## Cells run

| Cell(s) | Entry point / shape | Backend per stage | Route | Parity | Wall | VmHWM |
|---|---|---|---|---|---|---|
| R025 (x4: s3/gcs x source/target) | platform `validate_v2_config`, dirty `resolve_binding`-shaped descriptor | n/a (rejected pre-execution) | rejected, no cloud client constructed | n/a | <0.01s each | n/a (validation-only) |
| R026 (x4) | same, platform-only keys stripped | n/a (validation-only) | S3: validated clean. GCS: validated clean once `region` also stripped | n/a | <0.01s each | n/a |
| R027 | S3 e2e: moto upload -> real `_stage_s3_source` -> engine `run_pipeline` | rust_companion (hash) | full_frame | staged bytes identical to upload; masked row count matched | 0.058s | not captured (moto server thread dominates process RSS; not a masking-memory question) |
| R028 | GCS e2e: fake-gcs-server upload -> real `_stage_gcs_source` -> engine `run_pipeline` | rust_companion (hash) | full_frame | staged bytes identical to upload; masked row count matched | 0.046s | not captured |
| R029 | platform full-frame wrapper (`run_v2_pipeline`), hash-only, Rust-admitted | rust_companion | full_frame | n/a (this cell IS the timings-gap witness) | n/a | not captured |
| R030 | same wrapper, `when`-gated hash (pandas-forced control) | pandas | full_frame (pandas oracle) | n/a | n/a | not captured |
| R031 (x2: parquet/csv) | platform output layer, mixed mix job vs. pandas oracle | mixed (hash: rust; redact/truncate/passthrough: arrow_python_native) vs. pandas | full_frame both | byte-identical AND value-identical | n/a | n/a |
| R032 | platform output layer, fixed_width target | n/a (schema-rejected) | rejected | n/a | n/a | n/a |
| R033 | engine-direct, all-native mix + FPE + text_mask columns | pandas (whole table) | full_frame | n/a | not captured (validation-shape cell) | not captured |
| R034 | platform Phase 1 functions (not a real DB claim), Parquet, hash+redact | pandas (`run_mask_pipeline_chunked`) | streaming (claim-function-equivalent) | n/a | 0.036s | not captured |
| R035 | same functions, fixed_width source | n/a (admitted, then raised `NotImplementedError`) | admitted-then-rejected | n/a | n/a | n/a |
| R036 | CLI default, hash-only 5k | rust_companion | native | n/a | 0.864s (process wall, includes interpreter startup) | not captured (CLI subprocess, not the audit harness's VmHWM convention) |
| R037 | CLI `--native`, same fixture | rust_companion | native | n/a | 0.966s | not captured |
| R038 | CLI `--no-native`, same fixture | pandas | pandas | n/a | 0.842s | not captured |
| R039 | CLI `--chunked`, same fixture | pandas (chunked) | pandas chunked stream | n/a | 0.835s | not captured |
| R040 | CLI `decoy subset --dry-run`, 2-table FK (200 parent / 1000 child) | Polars (subset, no masking) | subset preflight + estimate | n/a | not captured | not captured |
| R041 | CLI `decoy subset --out`, real materialization, same fixture | Polars | subset materialize | n/a | not captured | not captured |
| R042 | CLI `decoy run` on the subset output (subset then mask, 2 independent jobs) | pandas (relationship route) | full_frame/sequential (FK job) | n/a | 0.787s | not captured |

VmHWM was not the point of the stage 2b cells (all are validation/functional
correctness checks, not size-tier routing probes), so it was not captured
for most of them; this is a deliberate scope choice, not an omission the
plan's memory-measurement rule required here. R018-R022-style size-tier VmHWM
work is stage 2a's, already recorded.

## Specific checks (plan section "Specific checks", items 1-6)

### 1. Cloud descriptor keys

**(a) Answer: validation rejects both S3 and GCS `resolve_binding`-shaped
descriptors, with no cloud client constructed first.** S3 fails on 2 extra
keys (`connection_id`, `connection_name`); GCS fails on 3 (`region` as well
-- GCS's engine schema has no `region` field at all, one more platform-only
key than S3, a detail Codex's own writeup already named but which R025 also
independently reproduced with a live monkeypatch guard). Evidence: R025 (all
four sub-cells), `boto3.client`/`google.cloud.storage.Client` both
monkeypatched to raise `AssertionError` if constructed, guard held for every
sub-cell (`no_cloud_call_proven: true`). File:line: `api/jobs/binding_resolve.py:128-151`
(emission), `config/_sources.py:71-125` + `config/_targets.py:38-92`
(`extra="forbid"` models), `api/jobs/v2_config.py:29-46` (the choke-point
`validate_v2_config`), `api/jobs/v2_submission.py:128` (called first, before
`resolve_v2_source_paths`). Agrees with the Codex independent audit's check 2
answer exactly.

**(b) Answer: the stripped (engine-valid) descriptor works end to end for
both S3 and GCS.** R027 (S3, moto `ThreadedMotoServer`) and R028 (GCS,
`fsouza/fake-gcs-server`, already present in `docker images`, no new pull)
both: real upload -> real platform staging function (`_stage_s3_source` /
`_stage_gcs_source`, unmodified) -> byte-identical local download -> engine
`run_pipeline` -> Rust-companion mask -> correct row count. GCS was run (not
skipped) because the emulator was already available locally; the only
wrinkle was `fake-gcs-server`'s own `-external-url` requirement (it bakes its
reported host:port into object metadata, so a mismatched external URL sends
the later media-download request to the wrong address and hangs until
timeout -- fixed by using a fixed host port matching `-external-url`,
documented in `scripts/audit/stage2b_cloud_gcs_e2e.py`).

### 2. Rust timings gap

**Answer: confirmed live on a real platform-wrapper run, not just by code
reading.** `run_full_frame_branch` (`api/jobs/v2_full_frame.py:83-91`)
computes `execute_ms = sum(t.elapsed_ms for t in result.timings)` from the
engine's own `ExecutionResult.timings`. The unified-slice (Rust) lane returns
`ExecutionResult(..., timings=(), boundary_conversion_ms=0.0, ...)`
unconditionally (`decoy_engine/execution/_unified_slice.py:371-372`). R029
(rust-admitted hash job through `api.jobs.v2_runner.run_v2_pipeline`, the
platform full-frame wrapper) shows `len(result.timings) == 0` and the
platform's own formula, applied verbatim, yields `execute_ms=0.0` despite a
real non-zero wall time. R030 (pandas control, identical wrapper call) shows
1 timing entry and `execute_ms=49.7`, proving the formula itself works and
the zero is specific to the Rust lane's empty tuple.

### 3. Output

**Answer: CSV and Parquet outputs through the platform output layer are
byte-identical between the mixed (rust+arrow_python_native) route and the
pandas oracle** (R031, both formats) -- stronger than the plan's own bar of
"compare bytes/values." Fixed-width target: schema-rejected, exact error
`targets.t.file.format: Input should be 'csv' or 'parquet'`
(`config/_targets.py:29-38`, `FileTarget.format: Literal["csv", "parquet"]`
has no `fixed_width` member) (R032). File:line for the output layer itself:
`api/jobs/v2_cloud_materialize.py:53-91` (`_materialize_file_output`).

### 4. Non-native fallout

**Answer: adding one FPE column and one text_mask column to an otherwise
all-native mix declines the WHOLE table to pandas**, via the same operator
allowlist (`_unified_slice_admission.py:533`, `ALLOWED_OPERATOR_IDS`) R014
already witnessed for a faker column. R033: `overall_backend: pandas`,
`unified_slice_nodes: {}`. Confirms the fallout shape generalizes across all
three named non-native strategy families (faker, FPE, text_mask), not just
faker.

### 5. Platform Phase 1 streaming

**Answer: the engine function invoked per chunk is
`decoy_engine.execution.run_mask_pipeline_chunked`, backend pandas
(`PandasExecutionAdapter`), exactly as the plan expected and as engine PR
#166's deletion left it.** `settings.streaming_min_input_mb` was lowered
(256.0 -> 0.0001) so a ~136 KB fixture qualifies. **This did NOT drive a
real DB-backed claim** (`queue_worker._claim_next_job` against a migrated
Postgres schema with a live `Job` row) -- that would need standing up the
full Alembic-migrated schema and ORM fixtures, judged not worth the time
against the marginal evidence gain when the actual execution machinery could
be driven directly. Instead it called the SAME functions the claim path
calls: `api.jobs._phase1_eligibility.phase1_eligibility` (real eligibility
classification, real `StreamingPlan`) then `api.jobs.v2_runner._run_v2_pipeline_streaming`
(the real per-table streaming execution function, unmodified, with
`decoy_engine.execution.run_mask_pipeline_chunked` traced via a wrapper that
still calls the original). This is explicitly a fallback, not a claim, per
the task's own allowance. R034: admitted, ran, one traced call to
`run_mask_pipeline_chunked`, wrote a real 2000-row Parquet output.

**Also confirmed: Phase 1 admits fixed-width; the stream reader rejects it
(Codex's finding 7).** `_table_rejections` (`_phase1_eligibility.py:137-176`)
never reads `config["sources"][name]["format"]`, so a fixed-width source with
only allowlisted strategies is admitted exactly like Parquet. The very next
call into `_run_v2_pipeline_streaming` raises `NotImplementedError` from
`streams.iter_source_batches:184-191` ("format 'fixed_width' unsupported
(csv | parquet)"). R035 reproduces this end to end (admit, then real
exception), not merely by reading the two functions side by side. Agrees
with Codex's finding 7 exactly.

### 6. Upload ownership

**Answer: confirmed by reading the code only; no exploit was run.**
`resolve_binding`'s `LocalRef` branch fetches the uploaded file by primary
key with zero owner/session scoping: `f = db.get(UploadedFile, ref.local)`
(`api/jobs/binding_resolve.py:93-94`). The function's own signature
(`binding_resolve.py:44-49`) takes only `db`, `ref`, `fmt_default`,
`direction` -- no user/session/project parameter exists for it to check
against. Its sole call site (`api/jobs/_service.py:462-467`) passes none
either. `UploadedFile.owner_id` (`api/models.py:1850`) exists as a column
but is never read anywhere in this path. The `ConnectionRef` branch has the
identical gap one line up (`binding_resolve.py:92`, filters `CloudAccount`
by name only). This is the same missing-owner-check SHAPE as the
already-documented BLOCKER in `docs/audit/codex-review-remediate-2026-07-30.md`
(workflow `pipeline_id`/`source_file_id` forwarding), reproduced by reading
in the newer S2 per-run binding-resolve path; that finding's fix does not
appear to have been extended here.

## Entry points covered

| # | Entry point | Real completed run(s) | Route/backend evidence |
|---|---|---|---|
| 2 | Platform full-frame wrapper | R029, R030 | `api.jobs.v2_runner.run_v2_pipeline`; rust_companion and pandas respectively |
| 3 | Claim -> worker, `phase1_streaming_tables` | R034, R035 (functions-direct, not a real Postgres-backed claim -- see check 5 above for why) | `phase1_eligibility` + `_run_v2_pipeline_streaming` -> `run_mask_pipeline_chunked`, pandas |
| 5 | CLI: default, `--native`, `--no-native`, `--chunked` | R036, R037, R038, R039 | native, native, pandas, pandas-chunked respectively, all matching the ledger's pre-existing code-reading predictions exactly |
| 6 | Subset-only, subset-then-mask | R040 (dry-run), R041 (real), R042 (subset output fed into a second `decoy run` job) | Polars subset (no masking); the downstream mask job took the pandas relationship route |

Entry point 4 (adaptive-scheduler `DispatchPlan` worker): **not attempted**,
per the plan's own instruction and the preflight evidence already on record
(`docs/records/audit-2026-09-30/preflight.json`: `cgroup_job_supervisor_socket.path_exists: false`,
`cgroup_v2_delegation.can_create_child_cgroup: false`) -- this host has
neither the supervisor socket nor cgroup delegation; recorded as "needs
integration host," not "untested."

## Disagreements with codex-independent-audit.md

**None found.** Every stage 2b run either confirmed a Codex claim exactly
(check 2's timings gap, check 5's fixed-width admit/reject split, check 1's
S3/GCS rejection) or extended it with a stronger result the code audit could
not itself produce without executing anything (check 1's "no cloud call
before rejection" proof via monkeypatch, check 1's full end-to-end success
of the stripped descriptor against real S3/GCS-compatible backends, check
3's byte-identical rather than merely value-identical output parity). The
Codex audit's own routing tables (single-table mask, FK tree, etc.) were not
re-derived from scratch here; stage 2b's scope was the plan's six specific
checks plus the four remaining entry points, not a full re-audit of every
shape table cell.

## Surprises

1. **Output bytes, not just values, matched between the Rust-mixed route and
   the pandas oracle** (R031). Nothing in the plan or the ledger predicted
   byte-for-byte identity (only value/schema/row-count parity was asked
   for); this means `_materialize_file_output`'s Parquet/CSV writers are
   fully deterministic and encoding-agnostic to which backend produced the
   `pa.Table`, a stronger property than the acceptance bar required.
2. **`fake-gcs-server`'s external-URL gotcha** cost a full debugging round
   (the emulator's own reported host:port in object metadata overrides the
   client's `STORAGE_EMULATOR_HOST`-derived expectations for the actual
   media-download request) -- worth flagging for any future GCS-emulator
   test in this codebase, since it is not obvious from the SDK's own error
   message (`Connection refused` at `0.0.0.0:4443`, not an auth error).
3. **The upload-ownership gap (check 6) is broader than "cross-owner
   binding" alone**: the `ConnectionRef` path has the identical shape
   (`CloudAccount` filtered by name only), so the same missing-scope
   pattern covers both local-upload AND cloud-connection bindings, not just
   the one the plan named.

## Not done / deferred

- **Entry point 4** (adaptive-scheduler `DispatchPlan` worker): needs an
  integration host with the cgroup supervisor and delegation this devbox
  lacks. Not attempted, per instruction.
- **A real Postgres-backed claim** for check 5 / entry point 3: the
  functions-direct fallback was used instead (see check 5's answer above for
  the reasoning). The throwaway Postgres container (`v3-0-fix-pg`) was
  available and reachable (confirmed in `preflight.json`) but no throwaway
  database was created in it this pass, since no cell in this stage needed
  one -- nothing to drop at the end.
- **The group_key order-dependence decline, null-bearing-int hash decline,
  and int64-bound-to-redact/truncate gap-hunting fixtures** flagged as not
  run in stage 2a remain not run; out of this stage's scope (the plan's six
  specific checks plus entry points 2/3/5/6).
- **`--chunked --no-native` together** (B207) was not tested this pass
  (only the four single-flag CLI modes were run); the ledger's existing
  code-inferred citation for B207 stands unwitnessed.

## Correction (2026-09-30, after the audit record's final gate)

The upload-ownership check above said `ConnectionRef` has "the identical missing-scope shape". It does not have a gap. `CloudAccount` is an org-level resource: an admin registers it once and every user picks from the list (`api/models.py` `CloudAccount` docstring). Project access control that could scope connections exists but is dormant (`api/authz/projects.py`: "Nothing in this module is wired into an artifact route"). The real gap is local uploads: the files router checks `owner_id` (`api/files/router.py:781`), and `resolve_binding` does not.

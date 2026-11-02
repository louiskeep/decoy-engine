Status: plan

Summary: F22 revision 5 (targeted amendment of revision 4, `c96d73e0`). Build only the idempotent half of publication on main's A-4 `OutputTxn` (no private arena, no verbatim port): a `decoy-publication/v1` descriptor with `result_id`, a destination lock that judges `operation_id`, durable binding, `already_committed` / `operation_conflict` outcomes, an overwrite policy that refuses a different result and never replaces, vault-before-output ordering with the vault digest bound in the descriptor, and rule D: existing state is adopted or reported only when it is durable along its whole directory chain to a recorded anchor and equal to this request, output and vault. The managed `CURRENT` pointer (old a4) is deferred: no consumer. Seven gateable slices in three groups.

Baseline: engine `main` at `0173c054` (contains A-4, `da1a522f`), merged into this branch. B-6 vault core: branch `feat/b6-vault-core` at `52df1da8` (re-pinned from `ec3f4de3`; adds the `fchmod` private-mode restore, `b6:fileaccess/_output.py:74-85`, `b6:fileaccess/_local.py:255`, `:273`), read-only, not merged (slice b waits for it; a1 conflicts with it mechanically, 9). r5 implementation: `2fa4f8a5`, read-only reference; this branch no longer carries it (commit `e7ca46a`).

Risk: R2 for this plan; the build is R3 (publication, persisted format, crash ordering).

Rules consulted: handoff-named dev-rules `00-universal`, `development-loop`, `risk-and-exceptions`, `reuse-first`, `scope-discipline`; README-selected rulebooks `data-and-migrations`, `testing`, `security` (NOT readable here: no `~/dev-rules` or `/home/cam/dev-rules` exists in this environment and no repo carries them; applied through the copies below); decoy-platform `handoffs/queue` `docs/handoffs/`: `DEFECT-CATALOG.md` (plan contract, classes, worker rules), `V1-BAR.md`, `HOSTILE-PLAN-REVIEW.md`, `OWNER-DECISIONS-2026-10-30.md`, `tools/plan-lint`; engine `CLAUDE.md` (600/700 census, pre-GA hard delete, established methodology); POSIX `rename(2)`, `link(2)`, `fsync(2)`, `flock(2)`.

Grounding: `file:line` is at `0173c054` (paths under `src/decoy_engine/` unless they start with `tests/` or `docs/`). `b6:` means `52df1da8`, `r5:` means `2fa4f8a5`, `platform:` means decoy-platform `main`. Probes ran from `uv.lock` (`uv sync --frozen --extra dev`; Python 3.11.18, PyArrow 24.0.0, DuckDB 1.5.4) on `0173c054`; results in section 1.3.

# F22 publication: re-plan (revision 5)

## 0. Review dispositions (Codex plan gate of `c96d73e0`, 3 V1)

Revision 4's dispositions (Dennis review of `ff3f6cdc`) are closed; see `c96d73e0` section 0. The three findings are one class, the revision-4 class "adopting or reporting state durable" (class 3, V1): rule D said what to fsync but not how far up, and checked the adopted state against itself rather than against the request. One rule closes them: rule D, durable chain and equal intent (5.0).

| Finding | Class | Disposition | Where |
|---|---|---|---|
| V1-1: adopt and `reconcile` fsync the unit and its parent only; a crashed publish's created ancestors are never made durable | 3 (V1 rule 2) | Rule D chain: every adopt or report fsyncs the leaf, then each directory up to and including the anchor; the descriptor records `anchor_depth` at publish; strict policies anchor at the capability root | 2, 5.0, 5.1, 5.2, 5.6, 7.2 |
| V1-2: vault adopt and publish fsync the vault directory, not its entry in `upload_dir` | 3 (V1 rule 2) | the same chain for the vault path, engine (`VaultBinding.anchor_depth`; strict: root) and platform (Q4 requirement, owner named) | 5.5, 13 |
| V1-3: `already_committed` checks the vault against the descriptor, not against this request's writer | 3 (V1 rule 1) | Rule D intent: before `already_committed` the adopted vault's logical rows are compared with the current writer's (the step-2 compare); different gives `publication_vault_mismatch` | 5.3, 5.5, 7.2 |

## 1. FRAME on current main

### 1.1 What main already provides

| Piece | Where | What it gives F22 |
|---|---|---|
| `OutputDir` / `OutputTxn` protocol | `fileaccess/_types.py:179-232` | one capability per output; stage, publish once or abort |
| strict implementation | `fileaccess/_output.py:197-291` | fd-relative stage beside the leaf, `validate_component` per name, `O_NOFOLLOW`, issuer token (`:194`, `:212`), weakref discard on collection (`:229`) |
| ambient implementation | `fileaccess/_local.py:196-322` | the same contract on paths; `local_stage_path()` (`:255`) hands a stage to a child process |
| dir publish | `fileaccess/_output.py:271`, `fileaccess/_local.py:263-270` | one `rename(2)`: an absent or empty leaf is bound, a non-empty leaf fails `ENOTEMPTY`, stage kept for `abort()` |
| file publish | `fileaccess/_output.py:253-273`, `fileaccess/_local.py:277-302` | `link(2)` exclusive or `rename(2)` replace |
| table sink on `OutputTxn` | `execution/_transactional_sink.py:355-361` (`table_sink` label), commit `:371-406`, abort `:408-438` | staged Parquet, one rename, cleanup evidence |
| hold spill on `OutputTxn` | `execution/_chunked_output_sink.py:460-477` (`hold` label) | private spill beside the sink |
| publish state | `execution/_publication_observer.py:42-49` (`PublicationEvidence`), `:99-111` (`observed_commit`), `:114-136` (`observed_abort`); `reasons/_record.py:57-69` (`PublicationState`, `CleanupState`) | F29 state a consumer reads; F22 projects into it, no new evidence type |
| vault on main | `vault.py:269-351` | strict: exclusive `link` of a file stage; ambient: `vault_direct`, truncating in place (`fileaccess/_local.py:226-232`), not crash-atomic |
| B-6 vault core (not merged) | `b6:fileaccess/_output.py:49` (`STAGE_MODE`), `:55` (`DURABLE_LABELS={"vault"}`), `:74-85` (`fchmod` restore), `:278-310` (durable link publish), `b6:fileaccess/_local.py:262-275`, `:303-334`, `b6:fileaccess/adapters/serial.py:51-72` (`write_durable`), `b6:vault.py:357-385` (`_publish`) | durable exclusive vault: stage 0600, write, file fsync, `link`, directory fsync as the commit point; `vault_exists`, `vault_commit_failed`. A failed directory fsync keeps the linked leaf (`b6:fileaccess/_output.py:302-310`), so a later `vault_exists` can name a leaf that was never made durable (V1-1) |
| B-6 boundary rule B0 (not merged) | `b6:execution/_pipeline_context.py:254`, `b6:execution/_chunked.py:435`, `b6:execution/native/_chunked_entry.py:650`, `b6:execution/out_of_core/_runner.py:196` | the engine refuses a vault writer wherever it would publish output itself (`vault_requires_resident_output`) |

### 1.2 Durability: does `OutputTxn` fsync the file?

No, on main, on either policy. Stage files close through Python file objects with no `fsync` (`fileaccess/adapters/arrow.py:41-70`, `serial.py`); a dir stage is renamed with no fsync of its files, the stage or the parent (`fileaccess/_output.py:271`; `fileaccess/_local.py:263-270`); a `replace` file publish swallows its parent fsync failure (`fileaccess/_output.py:71-77`, `:272-273`; `fileaccess/_local.py:178-193`), a `link` publish has none (`fileaccess/_output.py:253-269`). B-6 makes only the `vault` label durable. So a crash can leave a bound unit with zero-length or torn Parquet (V1 rule 2); a1 adds a durable dir publish.

### 1.3 Probes (main `0173c054`, from `uv.lock`)

Scripts `probe_f22.py`, `probe_strict.py` and `probe_seal.py` were run from the session scratchpad and not committed; each becomes a test in 7.2.

- P1: two `ParquetTransactionalSink`s, same target, same result, staged then committed in turn: the second raises `OSError` errno 39 (`ENOTEMPTY`). There is no `already_committed`; a retry of a committed job fails.
- P2: same target, a different result: the same raw `OSError` 39. There is no `operation_conflict`, and the caller cannot tell a retry from a collision. The first unit survives (2 rows).
- P3, P5: `os.fsync` calls during a table write and commit: 0 on the ambient policy, 0 on a strict `RootedFileAccess`.
- P4: a sink that committed zero tables (`ensure_dir`, `execution/_transactional_sink.py:392-395`) leaves an empty directory. A later sink with a different result renames over it and replaces the committed (empty) unit silently.
- P6: after a successful commit, a second `commit()` returns silently and a `write` is accepted into a fresh stage whose commit fails `OSError` 39. There is no sealed state.
- Platform order (read at platform `main` `d8908f42`, not run): two `_persist_job_vault` callers, both after output. Resident: `platform:api/jobs/v2_full_frame.py:168` publishes (`write_v2_outputs`) before `:206` writes the vault. Relationship routes: `platform:api/jobs/route_dispatch_exec.py:233-235` (`execute_sequential` materialises every declared target) before `:247`; the engine sink there writes a platform-private `stage_dir` (`platform:api/jobs/v2_sequential.py:201-209`, `platform:api/jobs/v2_out_of_core.py:332`) and the platform publishes the real, possibly non-local, targets. Neither fails the job on a vault failure (`platform:api/jobs/v2_vault.py:76-97`), so output can outlive its only re-identification map. `v2_runner.py:516` is a re-export. Q4.

### 1.4 What F22 still owes V1

Only the idempotent half, on top of 1.1: the descriptor with `result_id` (5.2); a destination lock across judge and bind (5.1); outcomes as receipts, not raw `OSError` (5.3); refuse-different, never replace (5.3); durable binding (1.2); rule D (5.0); vault before output (5.5); a sink state table with restart (5.6); the isolated route on the same bind (5.7). The protected backup (Q2) and the managed `CURRENT` pointer (5.4) are deferred.

## 2. Guarantee (V1 rules 1 to 3; ratchet plus named limits)

Rules, each with the test that holds it (7.2):

- **G1 (rule 2, atomic and durable).** A destination holds nothing, the complete prior unit, or the complete new unit. When `publish` returns `published`, every bound file, the unit directory and every directory from the parent up to and including the anchor (5.0) have been fsynced. Tests: `test_durable_dir_publish_fsync_order`, `test_adopt_syncs_ancestor_chain` (revision 4 cited `test_kill_sweep_unit`, which 7.2 never defined).
- **G1a (rules 1 and 2, rule D).** F22 never reports or builds on existing state unless, in the same call, it fsynced that state's whole chain to the anchor and found it equal to this request: `already_committed` (and `reconcile` sealing) only after the unit tree and its chain were fsynced and the vault matched this writer, else `commit_uncertain` or `publication_vault_mismatch`; a `VaultCommit` for an existing vault only after its leaf and vault-path chain were fsynced, else `vault_commit_failed`. Tests: `test_retry_already_committed`, `test_adopt_syncs_ancestor_chain`, `test_vault_adopt_or_conflict`, `test_vault_ancestor_chain_durable`.
- **G2 (rule 2, never replace).** At most one unit is bound per destination. A different result is refused (`target_occupied` or `operation_conflict`), and the prior unit is byte- and inode-identical afterwards. Tests: `test_two_writers_*`, `test_refused_overwrite_survivor`.
- **G3 (rule 1, idempotent retry).** Re-running a committed operation with the same result returns `already_committed`, not a failure, in this or a fresh process. Tests: `test_retry_already_committed`, `test_restart_recovery`.
- **G4 (rule 2, vault before output).** No unit whose request carries a vault is bound before its vault is durable, whether this call wrote it or adopted it. The descriptor carries the vault digest, and `already_committed` is reported only when the vault on disk has that digest and the same rows as this request's writer. Tests: section 7.2 group V, `test_two_writers_same_output_different_vault`.
- **G5 (rule 3, value-free reports).** F22 receipts, the `str()` of F22 errors and the F29 records F22 writes hold no data value, path, file name or OS message text. They carry registered codes, errno names, digests and counts. Test: `test_marker_corpus_user_surfaces`.

Named limits (what is not claimed):

- L1: durability holds where the filesystem honours `fsync(2)` and `flock(2)`, and an anchor (5.0: the strict capability root, the first pre-existing ambient ancestor, the platform `upload_dir`) is durable by deployment. If `flock` is not supported, publication is refused (`publication_lock_unavailable`); there is no lock-free fallback.
- L2: a SIGKILL leaves the attempt's private stage (`_decoy_publication_stage_*`, mode 0700) beside the destination. That costs disk only; reclaim belongs to F25 (Q1).
- L3: readers of an immutable directory are not coordinated; they see absent or complete.
- L4: F22 never deletes a committed unit, generation or vault. Expiry is F26 or the operator's job.
- L5: the quarantine sidecar written after the table commit (`execution/_sequential.py:594` then the DE-08 sidecar) keeps today's `publication_partial` contract.
- L6: same-uid hostile processes and hostile in-process objects are out of scope (owner decision 2026-10-30).
- L7: ambient policy only: a directory an earlier attempt created and was killed before fsyncing (one `mkdir`-to-`fsync` window; `begin()` fsyncs each directory it creates at once) counts as pre-existing, so it can become the next attempt's anchor. Strict policies have no such window (anchor = root).

## 3. Class table

| # | Class | Closing structure | Proof (test id, 7.2) | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | F22 errors carry the registered summary only, raised outside handlers (B-6 `_publish` pattern, `b6:vault.py:367-385`); receipts hold tokens, digests, counts; artifact names live only in the descriptor (data) | `test_marker_corpus_user_surfaces` (with the receipt field walk) | put the leaf name in a refusal message; add `path` to `ArtifactSummary` |
| 2 | Cleanup missing on an exit path | `OutputTxn` is the one owner of the stage; every non-bind exit of `publish_unit` calls `txn.abort()` in `finally`; a refused or `already_committed` judge discards the stage | `test_exit_matrix_publish` | drop `txn.abort()` on the `already_committed` branch |
| 3 | State machine and concurrency | destination `flock` held across read-judge-bind; sink `SinkState` enum plus a literal transition table (5.6) under one lock; rule D chain and intent before any adopt or report (5.0) | `test_two_writers_*` (judge-bind hook), `test_sink_transition_cells`, `test_restart_recovery`, `test_adopt_syncs_ancestor_chain`, `test_vault_ancestor_chain_durable` | judge before taking the lock; allow `write` in SEALED; issue an adopted `VaultCommit` or `already_committed` before its fsync; stop the chain at the parent; skip the writer compare on republish |
| 4 | Route parity and silent fallback | every route that binds output calls `publish_unit` (sink commit, `publish_tables`, isolated parent); capability row per route | `test_route_outcome_matrix` | revert the isolated parent to `atomic_commit` |
| 5 | Referential integrity | the unit (all tables plus descriptor) binds with one rename, so FK closure inside a complete unit stays the producer's (F06) | `test_fk_unit_all_or_none` | bind one rename per table |
| 6 | Untrusted producer | the isolated parent measures staged artifacts itself (size, sha256, rows) and builds the descriptor; a child-written descriptor or extra file is refused; descriptor reads are bounded | `test_isolated_parent_measures`, `test_descriptor_bounded_read` | trust a child `_decoy_publication.json`; drop the read cap |
| 7 | Check then use on a path (TOCTOU) | judge and bind under one lock through one `OutputDir` capability (fd-relative on strict policies); no `exists()` then write | `test_two_writers_same_result`, `test_strict_policy_confined` | replace the lock with an `exists()` check |
| 8 | Weak tests and overclaimed evidence | red-first on the slice start commit from `uv.lock`; every generated test carries a one-line non-vacuity assertion (the marker is present in the data, the lock probe saw `EWOULDBLOCK`, the hook fired) | the non-vacuity lines themselves; reviewer reruns | delete a non-vacuity line: the test must then be able to pass on a no-op build (checked once by the worker) |
| 9 | Behavior-preserving refactor drift | sink moves are characterization-first (P1 to P6 as tests); module census exact; physical-seam and module-size sentries unchanged in scope | `tests/sentry/test_module_size.py`, `test_sink_characterization` | move a function out of `_isolated_run.py` without updating the census |
| 10 | Frozen contract drift | `decoy-publication/v1` schema snapshot; F29 registry minor bump; additive `OutputDir` methods | `test_descriptor_schema_snapshot`, `tests/unit/test_reason_registry.py` | add a descriptor field without a snapshot update; add a code without the bump |

## 4. Enumerations

### 4.1 Exit paths of everything that owns files or state

`publish_unit` (new, slices a3, b; stage = the caller's `OutputTxn`, lock = destination `flock`):

| Id | Exit | Result | Stage | Binding |
|---|---|---|---|---|
| E1 | lock unsupported or open fails | refused `publication_lock_unavailable` | aborted | untouched |
| E2 | lock timeout | refused `publication_lock_timeout` | aborted | untouched |
| E3 | existing descriptor unreadable, oversize or invalid; or the leaf is non-empty without one | refused `publication_target_occupied` | aborted | untouched |
| E4 | same `result_id`, vault check passes (5.5), unit tree and chain fsynced (D2) | `already_committed` | aborted | untouched |
| E4a | same `result_id`, a D2 or D3 fsync fails | `commit_uncertain` (visible, not confirmed durable) | aborted | untouched |
| E5 | same `operation_id`, different result | `operation_conflict` | aborted | untouched |
| E6 | different operation, different result | refused `publication_target_occupied` | aborted | untouched |
| E7 | same result, vault missing, digest differs or rows differ from this writer's (V1-3); or the request has a vault and the descriptor's `vault` is `null`, or the reverse | refused `publication_vault_mismatch` | aborted | untouched |
| E8 | vault publish fails (`vault_commit_failed`), or `vault_exists` and an adopt fsync of the leaf or its chain fails (D1) | refused `vault_commit_failed`, vault kept if linked (B-6 rule 7) | aborted | never bound |
| E9 | vault exists with different content, or over the adopt read cap | refused `publication_vault_conflict` | aborted | never bound |
| E10 | descriptor write into the stage fails | failed | aborted | never bound |
| E11 | stage fsync walk fails | failed | aborted | never bound |
| E12 | `rename` fails | failed (stage intact, then aborted) | aborted | untouched |
| E13 | a chain fsync after `rename` fails | `commit_uncertain` (visible, not durable) | gone (renamed) | bound |
| E14 | published | `published` | gone (renamed) | bound |
| E15 | `KeyboardInterrupt` or SIGKILL at any point | lock released by the kernel; stage left (L2) | left | bound only if the rename completed |

Current exits F22 replaces: `execution/_transactional_sink.py:392-395` (empty commit, `ensure_dir`), `:397` (publish), `:398-403` (commit failure), `:408-438` (abort); `execution/_isolated_commit.py:73-86` (`atomic_commit`); `execution/_isolated_run.py:313-335` (in-process fallback commit), `:619-651` (`_commit`, `rmtree` on failure at `:643`, `:649`).

Vault step (slice b): `b6:vault.py:366-367` step 1 (stage created, policy error shape), `b6:vault.py:368-374` write, file fsync, link, directory fsync, `b6:vault.py:376-385` the two codes; adoption (D1): `OutputDir.adopt_file` (5.1), then `vault.load_vault(None, key, vault=BytesHandle(blob))` (`b6:vault.py:440`) on exactly the bytes that were fsynced.

### 4.2 Execution routes

- sequential: `execution/_sequential.py:594` (`observed_commit`), `:638` (`observed_abort`); sink `ParquetTransactionalSink`.
- out-of-core: `execution/out_of_core/_runner.py:311`, `:321`; same sink.
- full-frame streamed: `execution/_pipeline_full_frame.py:245` through `OutputPublish` (`execution/_chunked_output_sink.py:156-172`).
- mixed: `execution/_pipeline_mixed_route.py:267`, same `OutputPublish`.
- streamed generation: `generation/_streamed.py:471-474`.
- isolated parent: `execution/_isolated_run.py:647` (`atomic_commit`); child sink `execution/_isolated_worker.py:217` (preparation only).
- isolated in-process fallback: `execution/_isolated_run.py:313-335`.
- resident `publish_tables` (new, slice a3; the vault route, slice b).
- `_CallableSinkAdapter` (`execution/_transactional_sink.py:107-153`): not a publisher, reports `partial`/`unknown`; out of F22, unchanged.

### 4.3 FK shapes

- single, composite, self-referencing, cross-table chain (parent, child, grandchild), children in a later batch than their parent.
- F22's claim for each is all-or-none of the unit (one rename binds every table plus the descriptor, `fileaccess/_output.py:271`). FK closure inside the unit is the producer's. Tested per route in `test_fk_unit_all_or_none`.

### 4.4 Fields of public result types

Report types (user-visible; G5 applies):

- `CommitReceipt` (new; replaces r5's `r5:publication/_types.py:136-155`): `outcome` (closed `Outcome`), `operation_id` (caller text, shape-checked `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`), `reason_code` (registered or `None`), `result_id`, `descriptor_digest` (hex64), `vault_digest` (hex64 or `None`), `artifacts` (`ArtifactSummary`), `visible`, `durable` (bool), `os_error` (errno name or `None`). Dropped from r5: `generation`, `backup_ref` (relative paths), `backup_status`, `evidence_digest`.
- `ArtifactSummary`: `role`, `size`, `rows`, `sha256`. No path, no name.
- `PublicationEvidence` (`execution/_publication_observer.py:42-49`): `state`, `cleanup`, `last_operation`, `stored_bytes` (config-declared table names, F29-owned; unchanged).
- `IsolatedRunResult` (`execution/_isolated_common.py:321-388`): adds `publication_receipt`; `error` keeps `commit_error_text` (`execution/_isolated_common.py:253-259`).
- `ExecutionResult` (`execution/_adapter.py:43`), `GenerationResult` (`generation/_streamed.py:80`): add `publication_receipt`.

Data carriers (data by contract; never sent to a user-visible surface whole):

- `PublicationRequest`: `operation_id`, `unit` (`immutable_directory/v1` only; `managed_dataset/v1` and `expected_current` deferred with a4), `overwrite` (`"refuse_different"` only), `vault` (`VaultBinding | None`), `lock_timeout_s`.
- `VaultBinding(writer, out, anchor_depth=0)` (vault-path directories above the vault directory that the caller may have created, 5.5); `VaultCommit(sha256, size, entry_count, adopted)` (issued only by the vault module).
- descriptor document: binds artifact relative paths by design.

### 4.5 Output surfaces

- receipts on `sink.receipt`, `ExecutionResult`, `GenerationResult`, `IsolatedRunResult.publication_receipt`.
- `str()` of `PublicationRefused`, `PublicationFailed`, `CommitUncertain` (new, `DecoyError` subclasses with registered codes): reaches `Job.log`, the UI and notifications (V1 rule 3).
- F29 `ReasonRecord` via `capture_publication_failure` (`execution/_publication_observer.py`), `IsolatedRunResult.reason`, `error`.
- progress `publish_stored(table, bytes)` (`execution/_transactional_sink.py:318-320`), unchanged.
- persisted: `_decoy_publication.json` inside the unit (artifact relative paths, digests, `operation_id`); lock file `.<leaf>.decoy-lock` (empty, 0600). Vault: B-6's, unchanged.
- logs: F22 adds no log line; existing `execution/_isolated_commit.py` and `quarantine.py` logs unchanged.
- inbound, not outbound: the isolated child's staged files and envelope (`execution/_isolated_worker.py:217-222`).

## 5. Design

### 5.0 Rule D: durable chain and equal intent (class 3; V1-1 to V1-3)

Pattern: PostgreSQL's `SyncDataDirectory` / `fsync_fname` (state found on disk is fsynced before it is trusted; files `O_RDWR`, directories `O_RDONLY`), extended up the path as `durable_rename` does for a created parent. Rule: anything F22 binds, adopts or reports is (a) fsynced in the same call through the same capability along its whole chain, the leaf (file or unit tree) and then every directory from its parent up to and including the anchor, and (b) for adoption, equal to this request: output by `result_id`, vault by its logical rows against the current writer. A failed fsync is never reported durable; a difference is never reported as success.

Anchor: the directory below which this path may hold non-durable entries. Strict policies: the capability root (`RootedOutputDir._root_fd`), always, so the chain is the full `rel` path. Ambient output: the first ancestor that pre-existed `begin()`, recorded in the descriptor as `anchor_depth` (5.2) and read back on adoption. Vault: strict, the root; ambient, `VaultBinding.anchor_depth` from the caller, since the `vault` label creates no parent (`fileaccess/_output.py:45`). Sites:

- D1 vault adopt (5.5 step 2): `adopt_file` chain, then the row compare; failure `vault_commit_failed`, nothing bound.
- D2 `already_committed` (5.3, E4): `sync_chain` on the unit tree and its chain; failure `commit_uncertain` (E4a).
- D3 republish vault check (5.5): `adopt_file` chain, digest and row compare; fsync failure `commit_uncertain`, difference `publication_vault_mismatch`.
- D4 `reconcile` finding a binding (5.6): D2, plus D3 with a vault, before SEALED; failure UNCERTAIN. (D5, an adopted generation and `CURRENT`, is deferred with 5.4.)

### 5.1 Durable dir publish and the lock, in `fileaccess` (slice a1; classes 2, 7)

Pattern: SQLite atomic commit and PostgreSQL `durable_rename` (fsync the file, fsync the directory), as B-6 already cites (`b6:fileaccess/adapters/serial.py:51-56`); `flock(2)` advisory lock as in r5 (`r5:publication/_lock.py:1-7`).

- New stage label `publication` (prefix `_decoy_publication_stage_`, stage mode 0700, files 0600 through B-6's `STAGE_MODE` and `fchmod` restore, `b6:fileaccess/_output.py:49`, `:74-85`; Q8). Added to B-6's `DURABLE_LABELS` if B-6 has merged; otherwise a1 introduces the set and B-6 rebases onto it (Q3).
- Durable dir publish, both policies: (1) walk the stage fd-relative (no link followed), `fsync` every regular file and every directory, deepest first; (2) `rename(2)` the stage onto the leaf; (3) `fsync` the parent: the commit point; (4) fsync each directory above the parent, deepest first, up to and including the anchor (5.0). `begin()` fsyncs each directory it creates (`CREATES_PARENTS`) and its parent at once, and records the count for the descriptor's `anchor_depth`. A failure in (1) or (2) raises with errno only and leaves the stage for `abort()`. A failure in (3) or (4) raises `CommitUncertain`; the leaf is bound and is never removed.
- `OutputDir.lock(timeout_s) -> ContextManager`: opens `.<leaf>.decoy-lock` in the leaf's parent (`O_CREAT|O_NOFOLLOW|O_CLOEXEC`, 0600, via the parent fd on strict policies), polls `flock(LOCK_EX|LOCK_NB)` to a deadline, and never unlinks the file. `ENOLCK`/`EOPNOTSUPP`/`EINVAL` raise `LockUnavailable`.
- `OutputDir.read_member(component, *, limit) -> bytes | None`: reads one file inside a bound directory leaf, bounded, through the same capability; `None` when the leaf or member is absent. This is what judge and restart use; no path is handed out.
- `OutputDir.sync_chain(anchor_depth) -> None` (D2, D4; replaces revision 4's `sync_leaf`): the step (1) walk over the bound leaf (directory or file), then the parent and `anchor_depth` ancestors, fd-relative from the root on strict policies (which ignore the argument and go to the root), `O_NOFOLLOW`; an `anchor_depth` deeper than the path is refused as occupied; raises with errno only. Step (4) of publish is the same walk.
- `OutputDir.adopt_file(*, limit) -> bytes | None` (D1, D3; LOW-2, `read_member` is directory-only): opens the file leaf via the parent fd (`O_RDWR|O_NOFOLLOW|O_CLOEXEC`, regular file by `fstat` else occupied), reads at most `limit + 1` bytes (over: `LeafTooLarge`), fsyncs that fd and then the chain (`anchor_depth` argument, as `sync_chain`), and only then returns the bytes; `None` when absent. One fd, so the bytes returned are the bytes made durable.

### 5.2 Descriptor and `result_id` (slice a2; class 10)

Pattern: Git content addressing plus RFC 8785-style canonical JSON (sorted keys, fixed separators, ASCII, no NaN), as `r5:publication/_codec.py:1-10` cited; Delta Lake's transaction identifier for retries.

- `_decoy_publication.json`, schema `decoy-publication/v1`: `schema`, `unit`, `operation_id`, `result_id`, `artifacts` (sorted `[{role, path, size, sha256, rows}]`), `vault` (`null` or `{sha256, size, entry_count}`), `anchor_depth` (int, 0 to the path depth), `engine_version`.
- `result_id = sha256(b"decoy-publication-result/v1\x00" + canonical(artifacts))`. It does not include `operation_id` or `vault`: the result is the output content; the vault is judged by the row compare (5.5), not by a digest of its plaintext rows, which would put a value-derived hash in the output unit (rule 3). `descriptor_digest = sha256(descriptor bytes)`.
- The parser is strict: duplicate keys, unknown schema, non-finite numbers, absolute or `..` paths and a `result_id` that does not recompute are refused (`publication_target_occupied`). The read is capped at 1 MiB.
- Measured by the publisher from the stage via the stage fd (size, sha256, Parquet row count from the footer), never taken from a producer.

### 5.3 `publish_unit` for `immutable_directory/v1` (slice a3; classes 2, 3, 7)

```
def publish_unit(out: OutputDir, txn: OutputTxn, request) -> CommitReceipt:
    try:
        with out.lock(request.lock_timeout_s):
            existing = parse(out.read_member(DESCRIPTOR, limit=1 MiB))
            new = measure(txn)                         # artifacts, result_id
            verdict = judge(existing, new, request)    # 5.3 table
            if verdict is ALREADY:                     # rule D before the report
                check_vault(existing, request)         # D3, 5.5; slice b
                out.sync_chain(existing.anchor_depth)  # D2; OSError: commit_uncertain
            if verdict is not BIND: return receipt(verdict)
            _after_judge()                             # test hook, no-op (7.1)
            vault = commit_vault(request.vault)        # slice b, D1; NoVault in a3
            write_descriptor(txn, new, vault)
            txn.publish()                              # 5.1 durable
            return receipt(PUBLISHED)
    finally:
        txn.abort()                                    # no-op after publish
```

Judge (under the lock):

| Existing at leaf | Same `result_id` | Same `operation_id` | Outcome |
|---|---|---|---|
| absent or empty directory | n/a | n/a | bind |
| valid descriptor | yes | any | `already_committed` after the vault check against this writer (5.5) and D2; a D2 or D3 fsync failure gives `commit_uncertain`; different vault rows give `publication_vault_mismatch` |
| valid descriptor | no | yes | `operation_conflict` |
| valid descriptor | no | no | refused `publication_target_occupied` |
| non-empty, no or invalid descriptor | n/a | n/a | refused `publication_target_occupied` |

Overwrite policy: `PublicationRequest.overwrite: Literal["refuse_different"] = "refuse_different"`. r5's `Replace` and `ErrorIfDifferent` classes are not carried (pre-GA hard delete). A zero-table commit binds a unit holding only the descriptor, so probe P4's empty-directory replacement cannot recur for F22 output.

Test hook (V2-1): `_after_judge` is a module-level no-op called inside the lock after the judge said bind and before any vault or bind I/O. The two-writer tests replace it in the winner's process to block until the loser has seen the lock held (7.1); a mutation that judges before the lock then fails deterministically.

`publish_tables(out, tables, request)`: resident entry. It stages each table with `arrow_adapter.write_table` into a `publication` txn, then calls `publish_unit`.

### 5.4 Managed dataset and `CURRENT` (deferred; was slice a4)

No consumer of `managed_dataset/v1` exists: the platform publishes targets itself (`platform:api/jobs/v2_sequential.py:295-336`) and no engine route or platform caller reads a `CURRENT` pointer (platform grep at `d8908f42`). Deferred (11) until a consumer is named. Corrected rule for that slice (V2-4): under the root lock refuse only same `operation_id` with a different result (`operation_conflict`, against the generation `CURRENT` names) and a stale `expected_current` (`publication_current_moved`); a different result with a matching `expected_current` binds `generations/<result_id>/` through 5.3 and moves `CURRENT` by a durable pointer `replace`; adoption follows D5.

### 5.5 Vault before output (slice b; rule V, class 3)

Pattern: write-ahead ordering (the log record is durable before the page it describes, as in PostgreSQL WAL), using B-6's durable vault publish (`b6:vault.py:357-385`, commit `b7f70a17`).

Rule V, inside `publish_unit`, under the destination lock, after the judge says bind:

1. `commit_vault(binding)`: `binding.writer.publish(binding.out) -> VaultCommit`. This is B-6 `_publish` (stage 0600, `write_durable` = write plus file fsync, exclusive `link`, directory fsync), extended to fsync the vault-path chain to its anchor after the directory (5.0, `binding.anchor_depth`; V1-2) and to return `sha256` of the exact bytes it fsynced and the entry count. `VaultCommit` can be built only with the vault module's issuer token, the same device as `fileaccess/_output.py:194`.
2. `vault_exists`: adopt durably (D1) or refuse. `blob = binding.out.adopt_file(limit=2 * len(own) + 64 KiB, anchor_depth=binding.anchor_depth)` (`own` = the writer's serialized vault; a larger leaf cannot hold the same rows: `publication_vault_conflict` without decrypting). `adopt_file` fsyncs the leaf and its chain before returning; its failure refuses `vault_commit_failed`. Then `same_rows(blob, writer)`: `load_vault(None, key, vault=BytesHandle(blob))` (`b6:vault.py:440`; `BytesHandle` is a read-only in-memory `FileHandle`), compare `(ambiguous_dropped, sorted rows)` with the writer's. Equal: `VaultCommit(sha256(blob), len(blob), entries, adopted=True)`; this covers a leaf whose B-6 directory fsync failed or was killed (`b6:fileaccess/_output.py:302-310`) and never rewrites it. Different: refuse `publication_vault_conflict`, vault untouched.
3. `vault_commit_failed` from `_publish`: refuse; nothing is bound. A linked vault is kept (B-6 rule 7); the next retry reaches step 2 and fsyncs it.
4. Only now `write_descriptor(txn, new, vault)` and `txn.publish()`. `write_descriptor` and the bind take `VaultCommit | NoVault`, and `NoVault` is accepted only when `request.vault is None`.

Republish (`already_committed` candidate, D3): `request.vault` and the descriptor's `vault` must both be set or both `null` (LOW-1), else `publication_vault_mismatch`. If set, `binding.out.adopt_file(limit=descriptor vault.size, anchor_depth=binding.anchor_depth)` fsyncs the chain and reads the leaf; size and `sha256` must match the descriptor, and `same_rows(blob, request.vault.writer)` must hold (V1-3: equal masked output can carry a different reverse map, X for Alice in the bound run, X for Bob in this one); missing, larger, different digest, different rows or a blob this writer's key cannot open: `publication_vault_mismatch`; fsync failure: `commit_uncertain`. Then D2 and `already_committed`. Nothing is written, and `writer.publish` is never called, so a retry makes no second vault.

Ordering is by construction: the bind needs a `VaultCommit`, and a `VaultCommit` exists only after the vault chain fsync returned in this call: in `_publish` (step 1), or in `adopt_file` (step 2). "Output bound, vault missing" is therefore reachable only by deleting the vault out of band, and E7 reports that.

Route scope: B-6 rule B0 stays. Every engine sink keeps refusing a vault writer; an engine vault job whose target is a local engine `OutputDir` publishes through `publish_tables(..., request.vault=...)` (resident output). Platform routes that publish targets themselves order the vault outside the engine and owe the same chain (Q4). Lifting B0 for F22 sinks is Q3.

### 5.6 Sink state table and restart recovery (slices c1a, c1b; class 3)

`ParquetTransactionalSink(out, *, request=None)`: with no request, `operation_id` is a fresh uuid4, so the sink is not idempotent across processes but is still judged. Commit calls `publish_unit`. States: `OPEN`, `WRITING`, `COMMITTING`, `RECOVERING(prior)`, `SEALED(R)`, `UNCERTAIN`, `REFUSED`, `FAILED`, `ABORTED`. One `RLock`; transitions under it; I/O outside it.

| state | write | commit | reconcile | abort |
|---|---|---|---|---|
| OPEN | WRITING | COMMITTING | RECOVERING(OPEN) | ABORTED, txn aborted |
| WRITING | busy `publication_write_in_progress` | busy | busy | pending |
| COMMITTING | busy `publication_commit_in_progress` | busy | busy | pending |
| RECOVERING(p) | busy `publication_recovery_in_progress` | busy | busy | pending |
| SEALED(R) | refuse `publication_attempt_sealed` | return R | return R | no-op |
| UNCERTAIN | refuse | refuse `publication_commit_uncertain` | RECOVERING(UNCERTAIN) | no-op |
| REFUSED, FAILED | refuse | refuse | refuse | ABORTED, txn aborted |
| ABORTED | refuse `publication_attempt_aborted` | refuse | refuse | no-op |

In-flight exits:

| From | Outcome | Final state |
|---|---|---|
| WRITING | ok | OPEN |
| WRITING | exception | FAILED |
| COMMITTING | published, already_committed | SEALED |
| COMMITTING | operation_conflict, refused | REFUSED |
| COMMITTING | `CommitUncertain` | UNCERTAIN |
| COMMITTING | other exception before the rename | FAILED |
| RECOVERING(OPEN), nothing written | found same operation, D4 fsync ok | SEALED |
| RECOVERING(OPEN), nothing written | found same operation, D4 fsync fails | UNCERTAIN |
| RECOVERING(OPEN), bytes written | found | OPEN (commit judges) |
| RECOVERING(OPEN) | absent | OPEN |
| RECOVERING(UNCERTAIN) | found with our `result_id`, D4 ok (unit tree and chain to the anchor, not the parent alone) | SEALED |
| RECOVERING(UNCERTAIN) | found, D4 fsync fails | UNCERTAIN |
| RECOVERING(UNCERTAIN) | absent | REFUSED `publication_commit_not_visible` |

A pending abort applies on the exit from WRITING, COMMITTING or RECOVERING, except when the exit is SEALED or UNCERTAIN. Restart: a fresh sink with the same `operation_id` calls `reconcile()` (the routes call it before the first write when the request carries a caller `operation_id`), and a found binding seals it only after D4. Evidence: `SinkState` maps onto `PublicationState`/`CleanupState` in the sink (SEALED to `published`, UNCERTAIN to `unknown`, REFUSED/FAILED/ABORTED to `not_published`), so `observed_commit` keeps working for foreign sinks unchanged.

### 5.7 Isolated route on the same bind (slice c2; classes 4, 6)

- The parent opens a `publication` txn for `output_dir` on the ambient policy (the route is ambient-only, `execution/_isolated_run.py:261-272`). It hands `txn.local_stage_path()` (`fileaccess/_local.py:255`) to the child as `staging_output_dir`, and on success calls `publish_unit`. The parent measures the artifacts and refuses any child-written descriptor (class 6).
- The child writes plain staged files only (V2-5): no publishing sink, lock, descriptor or rename. Its `ParquetTransactionalSink(Path(staging_output_dir))` (`execution/_isolated_worker.py:217`) would, after c1, bind a unit with a descriptor inside the parent's stage; c2 swaps it for `StagedParquetWriter` (the sink's write half split out in c1a). The parent is the only descriptor writer.
- The in-process fallback (`execution/_isolated_run.py:313-335`) does the same through `publish_tables`.
- `_isolated_commit.sibling_staging_dir`, `stage_tables` and `atomic_commit` (`execution/_isolated_commit.py:56-86`) are deleted (pre-GA).

## 6. Reuse inventory

- A-4 `fileaccess`: `OutputDir`, `OutputTxn`, `RootedOutputTxn`, `LocalOutputTxn`, `validate_component`, issuer token, `_rmtree_fd`, `local_stage_path`, `arrow_adapter.write_table` / `parquet_writer`. F22 adds the `publication` label, `OutputDir.lock`, `read_member`, `sync_chain`, `adopt_file` and the durable dir publish (about 170 lines) instead of a second mechanism.
- B-6 core (`52df1da8`): `DURABLE_LABELS`, `STAGE_MODE`, `serial.write_durable`, `vault._publish`, `vault_exists`, `vault_commit_failed`, `load_vault`, rule B0 and its four sites. F22 extends `_publish` to return a `VaultCommit`; it does not copy it.
- F29: `PublicationEvidence`, `observed_commit`, `observed_abort`, `capture_publication_failure`, `PublicationState`, `CleanupState`, the reason registry (`reasons/_registry.py:24`, `1.2.0` on main, `1.3.0` on B-6), `tests/unit/test_reason_registry.py`, `tests/fixtures/f29/`.
- Sentries: `tests/sentry/test_module_size.py`, `test_physical_seam_disconnection.py`, `test_public_import_boundary.py`, `tests/sentry/file_access_registry.toml` (new labels registered there).
- r5 (`2fa4f8a5`), read for rules only, not copied: `r5:publication/_codec.py` (canonical JSON, strict parse), `r5:publication/_lock.py` (no lock-free fallback), `r5:publication/_types.py:19-27` (outcome vocabulary). Its tests serve as idea sources for 7.2, not ports.
- New machinery and why: the descriptor, judge, rule D adoption and the sink state table do not exist on main or B-6.

## 7. Hostile corpus (acceptance tests, red first)

### 7.1 Rules for every test

- Recorded red on the slice's start commit, from `uv.lock`, in the same environment as the branch; exact ids in the slice evidence file. A test green at baseline is labelled characterization and paired with its mutation.
- Crash tests run the scenario in a `spawn` child that installs one monkeypatch wrapper, which calls `os.kill(os.getpid(), SIGKILL)` after the named step. The parent asserts the child died by SIGKILL and that the step's marker file exists (non-vacuity: a hook that never fired fails the test).
- Two-writer tests are ordered by the `_after_judge` hook (5.3, V2-1), not by timing: the winner W (a `multiprocessing` process) replaces the hook to set `w_in_hook` and then wait on `l_probed` (timeout fails the test). The loser L waits on `w_in_hook`, probes the lock file with `flock(LOCK_EX|LOCK_NB)`, must get `EWOULDBLOCK` (non-vacuity: W holds the lock between judge and bind), sets `l_probed`, then calls `publish_unit`, which blocks on the lock until W returns and judges W's binding. Under the mutation "judge before lock" W is parked between judge and bind, so L's judge sees no binding every run and the test fails deterministically.
- Every generated test asserts its non-vacuity in one line (marker present in the data, hook fired, probe saw `EWOULDBLOCK`). No oracle-twin harness (V2-LOG).

### 7.2 Tests per guarantee

| Test | Slice | Holds | Red before (commit, expected failure) |
|---|---|---|---|
| `test_durable_dir_publish_fsync_order` (both policies: every staged file and directory fsynced, then `rename`, then the parent, then each directory up to and including the anchor (strict: the root; ambient: the first pre-existing), deepest first, by spy order; descriptor `anchor_depth` equals the count) | a1 | G1 | `0173c054`: 0 fsync calls (probes P3, P5) |
| `test_durable_publish_faults` (fsync file EIO, fsync stage EIO, rename EIO, then EIO on each chain fsync; the chain faults give `CommitUncertain` with the leaf kept) | a1 | G1 | `0173c054`: no fsync, so no failure raised; red |
| `test_lock_contention_and_unsupported` (`EWOULDBLOCK` to deadline gives `LockTimeout`; patched `ENOLCK` gives `LockUnavailable`; the lock file is never unlinked) | a1 | G2 | absent API: red by `AttributeError`; mutation: return without `flock` |
| `test_strict_policy_confined` (a symlinked leaf parent is refused, lock and `read_member` stay under the root) | a1 | class 7 | characterization of A-4 plus the new methods; mutation: open the lock by path |
| `test_descriptor_canonical_and_strict`, `test_descriptor_schema_snapshot`, `test_descriptor_bounded_read` | a2 | class 10, 6 | absent: red; mutation: drop the duplicate-key hook |
| `test_retry_already_committed` (same and new process, same `operation_id` and tables; D2 fsync spy: every unit file and directory and the chain before the receipt) | a3 | G3, G1a | `0173c054`: `OSError` 39 (P1); mutation: drop `out.sync_chain` |
| `test_adopt_syncs_ancestor_chain` (V1-1; publish into `a/b/out` with `a` and `b` absent, both policies; SIGKILL after the rename and before each chain fsync in turn (`b`, `a`, anchor); fresh-process retry: spy shows unit tree, `b`, `a`, anchor fsynced in that order before `already_committed`; EIO on each of those on the retry gives `commit_uncertain`, `durable=False`; non-vacuity: kill marker present, `anchor_depth` 2 on ambient) | a3 | G1, G1a | `c96d73e0` design: the retry fsyncs `out` and `b` only and reports `already_committed`; red by mutation `sync_chain` stopping at the parent; `0173c054`: absent |
| `test_two_writers_different_result` (judge-bind hook, 7.1; different tables; case 1 W and L share `operation_id`: `operation_conflict`; case 2 different ids: `publication_target_occupied`): exactly one binding, equal to the winner's | a3 | G2 | `0173c054`: loser gets raw `OSError` 39, no outcome (P2) |
| `test_two_writers_same_result` (judge-bind hook; same tables, same `operation_id`): exactly one `published`, one `already_committed`, one binding | a3 | G2, G3 | `0173c054`: loser `OSError` 39 |
| `test_refused_overwrite_survivor` (after each refusal above: the prior unit's tree, bytes, inode numbers and descriptor digest are unchanged; no stage left) | a3 | G2 | characterization at `0173c054` for non-empty units; red for the empty unit (probe P4 replaces it) |
| `test_exit_matrix_publish` (E1 to E14 and E4a injected; asserts outcome, binding untouched or bound per 4.1, stage aborted) | a3 | class 2 | absent: red |
| `test_marker_corpus_user_surfaces` (every `CommitReceipt`/`ArtifactSummary` field is a closed token, hex64, int, bool or a shape-checked id; `PATHMARK` in the destination path, `TBLMARK` as a table name, `OSMARK` as injected `OSError` text, across E1 to E14: absent from receipts, `str(exc)`, `ReasonRecord`, `IsolatedRunResult.error`; non-vacuity: `TBLMARK` is present in the descriptor's `artifacts[].path`) | a3, c2 | G5 | absent: red; mutation: put the leaf in the refusal text |
| `test_vault_order_is_structural` (spies on `os.link`, `os.fsync`, `os.rename`: the vault link and the vault directory fsync precede the output rename; with each vault step faulted (stage write, file fsync, link EIO, dir fsync EIO, `vault_exists` with different content) the output leaf is never bound) | b | G4 | absent: red; mutation: call `commit_vault` after `txn.publish()` turns it red |
| `test_output_bound_vault_missing_unreachable` (kill sweep over every hook point in `publish_unit` with a vault: after each kill, if the output leaf is bound then the vault leaf exists and its sha256 equals the descriptor's; at the point after `commit_vault` returns, a fresh-process retry gives `published` with the vault inode unchanged (adopted, not rewritten)) | b | G4, G1a | `0173c054`: the platform-shaped order `run_pipeline(sink=..., vault_writer=w)` then `w.write()`, SIGKILL before `write`, leaves output bound and no vault: red; on `52df1da8` B0 refuses it and the F22 API is absent: red; mutation: swap the order |
| `test_republish_requires_vault_digest` (bound unit; vault deleted, or replaced by another valid vault: `publication_vault_mismatch`; untouched: `already_committed`; LOW-1 presence both ways: mismatch; D3 fsync EIO: `commit_uncertain`; nothing written in any case) | b | G4 | absent: red; mutation: skip the digest compare |
| `test_two_writers_same_output_different_vault` (V1-3; W binds tables T with vault rows X for Alice; L has the same T and vault leaf, rows X for Bob, same and different `operation_id`, sequential and under the judge-bind hook: `publication_vault_mismatch`, vault bytes and inode unchanged; non-vacuity: L's `result_id` equals W's, and a third writer with W's rows gets `already_committed`) | b | G4, G1a | `c96d73e0` design: L gets `already_committed` (descriptor digest matches); red by mutation: skip `same_rows` on republish; `0173c054`: absent |
| `test_vault_adopt_or_conflict` (equal rows adopted only after the spy saw leaf fsync then the chain, before `VaultCommit` and the output rename; leaf or chain fsync EIO: `vault_commit_failed`, not bound; different rows or over the cap: `publication_vault_conflict`; vault bytes and inode unchanged) | b | G4, G1a | absent: red; mutation: issue `VaultCommit` before the fsyncs |
| `test_vault_ancestor_chain_durable` (V1-2; vault leaf in a `vaults` directory the caller just created, `anchor_depth=1`, and strict under the root: spy shows leaf, `vaults`, anchor fsynced before the `VaultCommit` and the output rename, in `_publish` and in adopt; EIO on each chain fsync: `vault_commit_failed`, leaf kept, not bound; retry with the fault cleared, and after SIGKILL between `link` and each chain fsync: chain spied, then `published`) | b | G4, G1a | `c96d73e0` design: only leaf and `vaults` fsynced; red by mutation: chain stops at the vault directory; `0173c054`: absent |
| `test_sink_transition_cells` (every cell of both 5.6 tables, with hook-held in-flight calls; asserts the arriving result, final state and no I/O under the lock) | c1a | class 3 | `0173c054` (probe P6): after a successful commit a second `commit()` returns silently, a `write` is accepted into a new stage, and its commit fails `OSError` 39; no SEALED state; red |
| `test_restart_recovery` (child SIGKILLed after the bind rename, before return; fresh sink, same `operation_id`: `reconcile()` gives SEALED, `write` refused `publication_attempt_sealed`, `commit()` returns R, binding intact; D4 chain by fsync spy before SEALED, including a kill after the rename before each created-ancestor fsync, and a D4 fsync EIO leaves UNCERTAIN) | c1b | G3, G1a | `0173c054`: the fresh sink stages and fails `OSError` 39 |
| `test_route_outcome_matrix` (sequential, out-of-core, full-frame streamed, mixed, streamed generation, isolated parent, isolated fallback, `publish_tables`: each returns a receipt; a retry gives `already_committed`; a conflict gives `operation_conflict`) | c1b, c2 | class 4 | `0173c054`: the isolated route raises `CommitError`, the others raise `OSError` 39 |
| `test_fk_unit_all_or_none` (4.3 shapes by route, SIGKILL mid-stage or a refused commit: the destination holds all tables or none) | c1b | class 5 | characterization; mutation: one rename per table |
| `test_isolated_parent_measures` (child plants `_decoy_publication.json` or an extra file: refused; sizes and digests come from the parent; the unmodified child leaves no descriptor, lock file or nested unit in the stage, V2-5) | c2 | class 6 | absent: red |
| `test_sink_characterization` (P1 to P6 behaviours pinned before c1a changes the sink, then deliberately updated in c1a's red commit) | c1a | class 9 | characterization |

## 8. Frozen contracts touched

- `decoy-publication/v1` descriptor: new (r5 never merged); frozen in a2 by `test_descriptor_schema_snapshot`.
- F29 reason registry: additive codes (`publication_lock_unavailable`, `publication_lock_timeout`, `publication_target_occupied`, `publication_operation_conflict`, `publication_commit_uncertain`, `publication_commit_not_visible`, `publication_vault_mismatch`, `publication_vault_conflict`, `publication_write_in_progress`, `publication_commit_in_progress`, `publication_recovery_in_progress`, `publication_attempt_sealed`, `publication_attempt_aborted`). Registry minor bump to the next version after main's at slice start (`1.3.0` today, `1.4.0` if B-6 lands first), manifest and consumer-contract fixtures regenerated.
- A-4 `OutputDir` protocol: additive (`lock`, `read_member`, `sync_chain`, `adopt_file`); `StageLabel` gains one label (`publication`); `tests/sentry/file_access_registry.toml` updated.
- `TransactionalSink` protocol (`execution/_transactional_sink.py:66-104`): methods unchanged; `ParquetTransactionalSink.commit()` returns a `CommitReceipt` (was `None`); new keyword `request`.
- On-disk output: every engine unit gains `_decoy_publication.json` (pre-GA; Q5).
- Vault file format (`VAULT_FORMAT_VERSION`): unchanged. `VaultWriter.write` keeps its signature; `VaultWriter.publish` is new; `vault.BytesHandle` is new (internal). Descriptor gains `vault.size` and `anchor_depth` (new schema, frozen in a2). `VaultBinding` gains `anchor_depth`.
- `IsolatedRunResult`, `ExecutionResult`, `GenerationResult`: additive `publication_receipt`. `CommitError` from `_isolated_commit` is deleted (pre-GA).

## 9. Slices

Repo `decoy-engine`, one integration branch from `main` per group. Each slice lands its red tests in its own first commit and updates `docs/publication.md` (new) for what it adds. Size = production plus test lines changed, estimated from r5's measured modules with the dropped features removed (r5: `_codec.py` 239, `_lock.py` 81, `_local_directory.py` 419, `_managed.py` 582, `_types.py` 209). Cap: 10 production files and 800 lines per slice. a4 (managed dataset) is deferred (5.4). Group (a) is three slices and group (c) three (c1 pre-split, V2-3). The platform half of vault ordering is not an engine slice: it is handoff `p1-platform-vault-before-output-order-fix` (decoy-platform, branch `fix/vault-before-output-order`), independent of F22 and B-6, listed below so the order is visible.

| Slice | Content | Prod files | Size (prod + tests) | Depends on |
|---|---|---|---|---|
| a1 | durable dir publish with the chain to the anchor, `publication` label, `OutputDir.lock`, `read_member`, `sync_chain`, `adopt_file` with chain, both policies | 4 (`fileaccess/_output.py`, `_local.py`, `_types.py`, `__init__.py`) | about 255 + 400 = 655 | main; rebases mechanically on B-6 `52df1da8` (`fchmod` restore and `DURABLE_LABELS` in the same two modules) whichever lands second |
| a2 | descriptor codec (vault `size`, `anchor_depth`), `result_id`, request and receipt types, strict parse | 3 (`publication/__init__.py`, `_descriptor.py`, `_types.py`) | about 285 + 210 = 495 | a1 |
| a3 | `publish_unit` judge and bind under the lock, D2 chain on `already_committed`, `_after_judge` hook, outcomes, `publish_tables`, registry codes | 4 (`publication/_publish.py`, `_types.py`, `reasons/decoy/_core.py`, `reasons/_registry.py`) | about 285 + 420 = 705 (fixtures regenerated, not counted) | a2 |
| b | `VaultCommit`, `VaultWriter.publish` with the vault chain, `BytesHandle`, `same_rows`, adopt durably (D1) or refuse, Rule V inside `publish_unit`, republish presence, digest and writer-row check (D3) | 3 (`vault.py`, `publication/_vault_order.py`, `publication/_publish.py`) | about 225 + 530 = 755 | a3 and B-6 core merged |
| c1a | sink state table and transition cells, `StagedParquetWriter` split out of the sink's write half, characterization | 2 (`execution/_sink_state.py` new, `_transactional_sink.py`) | about 260 + 320 = 580 | a3 |
| c1b | `reconcile` with the D4 chain, restart, evidence projection, route wiring: receipts out and `reconcile()` before the first write | 10 (`execution/_transactional_sink.py`, `_publication_observer.py`, `_adapter.py`, `generation/_streamed.py`, `_sequential.py`, `out_of_core/_runner.py`, `_pipeline_full_frame.py`, `_pipeline_mixed_route.py`, `_pipeline_context.py`, `_chunked_output_sink.py`) | about 170 + 430 = 600 | c1a |
| c2 | isolated parent and fallback on `publish_unit`; child on `StagedParquetWriter`; delete `atomic_commit` | 4 (`execution/_isolated_run.py`, `_isolated_commit.py`, `_isolated_worker.py`, `_isolated_common.py`) | about 100 (net negative) + 260 = 360 | c1b |
| platform (external) | vault durable before any target on both `_persist_job_vault` sites; vault failure fails the job | decoy-platform only | sized by that handoff | none |

Total engine about 4,150 lines (revision 4: about 4,145; net +5, inside the estimate's noise). Revision 5 adds about 210: production +35 (chain walk and anchor recording in a1, `anchor_depth` in a2, the `sync_chain` call in a3, `same_rows` on republish and the vault chain in b), tests +175 (chain cases in a1 +30, descriptor field +10, `test_adopt_syncs_ancestor_chain` +70, `test_two_writers_same_output_different_vault` +65). Cut to pay, about 205: `sync_leaf` folded into `sync_chain` (-10); the D2 kill case moved out of `test_retry_already_committed` into the chain test (-40); `test_two_writers_different_operations` folded into `test_two_writers_different_result` (-40); `test_receipt_field_walk` folded into `test_marker_corpus_user_surfaces` (-40); `test_republish_vault_presence` folded into the digest test (-25); `test_vault_committed_output_not_bound` folded into the kill sweep (-50); `test_vault_dir_fsync_eio_then_retry` replaced one for one by `test_vault_ancestor_chain_durable` (0). Next cut candidate if a slice measures over: delete the isolated in-process fallback instead of rewiring it (c2, owner decision). c1b is at the 10-file cap; an eleventh module splits it by route (streamed: full-frame, mixed, context, chunked sink; batch: sequential, out-of-core, generation), not the cap.

c1b route edits (V2-3): `execution/_sequential.py:594`, `:638`, `execution/out_of_core/_runner.py:311`, `:321` (receipt from `commit()`, `reconcile()` before the first write); `execution/_pipeline_full_frame.py:245`, `execution/_pipeline_mixed_route.py:267` through `OutputPublish` (`execution/_chunked_output_sink.py:156-172`), request threaded from `execution/_pipeline_context.py`; `generation/_streamed.py:471-474` (receipt on `GenerationResult`).

Module census (main LOC; rule): `execution/_sequential.py` 699, call sites only, net <= 0 (census `tests/sentry/test_module_size.py:116`). `execution/out_of_core/_runner.py` 644 on main, 649 on B-6, net <= 0 against whichever census entry is current (`:120`). These two are c1b's net-zero modules: each added line is paid by a removed one in the same module (the commit-and-observe pair moves behind one helper in `_publication_observer.py`). `_pipeline_context.py` 508 (518 on B-6), `_chunked_output_sink.py` 565: at most 600. `quarantine.py` 638, untouched. `execution/_isolated_run.py` 651, net <= 0 (logic goes to `_isolated_commit.py`). `execution/_transactional_sink.py` 438, at most 600 after c1 (the state machine lives in `_sink_state.py`). `fileaccess/_output.py` 389 (443 on B-6), `fileaccess/_local.py` 378 (471 on B-6), each at most 600 after a1 (about 110 lines each lands near 555 and 580 on B-6; over 600 moves the walk to a new `fileaccess/_durable.py`). New modules at most 600 each.

Commands (from `uv.lock`, one environment; record `python -V`, PyArrow and DuckDB versions):

```bash
uv sync --frozen --extra dev
```

```bash
uv run pytest tests/unit/publication tests/fileaccess tests/sentry -q -p no:cacheprovider
```

```bash
uv run pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider
```

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/decoy_engine
```

`scripts/dev/gates.sh` does not exist at `0173c054`; the baseline is the same commands on the slice's start commit, both failure sets in the evidence file.

## 10. Worker evidence rules

The handoff worker rules and the V1 bar apply as written. Plan-specific: apply each mutation named in sections 3 and 7.2 once and show its test red; never raise a census number; a repeat of a class this plan closes is stopped and reported.

## 11. V2-LOG (logged, not planned)

From revision 1 (`50ad3ba8`): the PEP 578 containment harness, oracle twins, the AST creation-site census, attack A6 and the safe-parent precondition, `_report.py` exact-type validation, and "every entry point" completeness with proof P7 (r1 lines 44, 164, 216, 244, 246, 274, 282).

Found while framing:

- `execution/_isolated_commit.py:83-86`: `CommitError` text holds the output path and the `OSError` text; it reaches only `__cause__` (users see `commit_error_text`, `execution/_isolated_common.py:253-259`). Deleted in c2 anyway.
- `fileaccess/_local.py:298-302`: a log line in the generic `LocalOutputTxn.publish` names `quarantine sidecar` for any label (admin log, wording).
- `fileaccess/_output.py:71-77`, `fileaccess/_local.py:178-193`: swallowed parent fsync for non-F22 file outputs (quarantine, pack, subset). Durability of those outputs is outside F22.
- `execution/_isolated_commit.py:56-62`: a bare-path sibling stage, theoretical TOCTOU on an ambient-only route; deleted in c2.
- Deferred in revision 4: managed dataset, `CURRENT` CAS and D5 (old slice a4, about 520 lines; 5.4): no consumer named. Its tests `test_managed_current_cas` and `test_kill_sweep_managed` move with it, rewritten to the 5.4 rule (a different result with a matching `expected_current` advances).

## 12. Pre-mortem

- **R1: B-6 core does not merge, or merges changed.** Slice b blocks; a1 to a3, c1a, c1b and c2 do not depend on it. If `DURABLE_LABELS` lands first in a1, B-6 rebases its one-line set (Q3).
- **R2: `flock` on a network filesystem behaves as a no-op.** L1. The lock refuses only when `flock` errors. Docs say "local filesystems"; no detection is attempted (V2).
- **R3: fsync cost on large units.** One fsync per file plus directories; units are tens of files. If a bench regresses more than 5%, report it; do not drop the fsync.
- **R4: the adoption compare in 5.5 is slow for a large vault.** It decrypts the existing vault once, on the crash-retry path and, since V1-3, on every `already_committed` with a vault; a retry is rare, and correctness of the reverse map outranks it.
- **R5: the descriptor file breaks a consumer that lists every file in an output directory.** Q5. The platform `target.file` listing and the CLI are checked in c1b (grep), and a hit is reported, not patched in F22.
- **R6: a third round.** Most likely cause: a route missed in 4.2. `test_route_outcome_matrix` fails on it.

## 13. Open questions for the owner (recommended defaults)

- Q1: orphan `_decoy_publication_stage_*` after SIGKILL (L2) are reclaimed by F25, not F22. This supersedes the 2026-10-30 Q1 answer ("F22 ships `reclaim_orphan_arenas`"): there is no arena, and a stage carries no owner lock. **Default: F25.**
- Q2: protected backup. Nothing is ever replaced and prior generations are kept, so a backup adds no V1 protection. **Default: defer to V2; not built.**
- Q3: B-6 rule B0 stays for every sink; vault jobs publish through `publish_tables` with `request.vault`. Lifting B0 for F22 sinks (chunked and out-of-core vault jobs) is a later item. Also: `DURABLE_LABELS` is introduced by whichever of a1 and B-6 merges first. **Default: keep B0; B-6 merges first if ready.**
- Q4: the platform writes output before the vault and swallows a vault failure at two sites (1.3): resident `platform:api/jobs/v2_full_frame.py:168` / `:206`, and the sequential and out-of-core relationship routes `platform:api/jobs/route_dispatch_exec.py:233-235` / `:247`. `publish_tables(..., vault=...)` cannot serve the second: the engine writes only a platform-private `stage_dir`, the platform publishes possibly non-local targets later, and `publish_tables` is resident-only (Q3). **Default, both sites: write the vault durably before any target is materialised (relationship routes: after the engine fills `stage_dir`, before the platform publishes it) and fail the job on a vault failure; no F22 dependency. `publish_tables` only where the target is a local engine `OutputDir`. Code fix: handoff `p1-platform-vault-before-output-order-fix` (9), not planned here.** Revision 5 requirement (V1-2, rule D chain): on that branch (`fix/vault-before-output-order` `7be71b6`) `_persist_job_vault` creates `vaults` with `mkdir(parents=True)` and fsyncs the file and `vaults` only (`platform:api/jobs/v2_vault.py:127`, `:137`); it must also fsync each directory above `vaults` up to and including the anchor `upload_dir`, on every persist rather than only when `mkdir` created it (so L7 does not apply there), before the first target publish, and a failure fails the job as a vault failure. Test `test_vault_dir_entry_durable_before_target` (fresh `upload_dir` with no `vaults`: spy order file, `vaults`, `upload_dir`, then the first target publish; EIO on the `upload_dir` fsync fails the job with no target and no vault leaf), red on `7be71b6`. Owner: the platform builder lane on that branch, now in Codex gate; the orchestrator relays this requirement to it. This plan does not edit platform code or that handoff.
- Q5: every engine output directory gains `_decoy_publication.json`. **Default: accept (pre-GA); document it in `docs/publication.md`.**
- Q6: parent-directory fsync failure after the rename gives `commit_uncertain` (bound, not durable), and `reconcile` re-fsyncs and seals. B-6 reports the same vault case as `vault_commit_failed` with the vault kept. **Default: keep the two vocabularies; the vault code is B-6's.**
- Q7: the managed dataset (old a4) is deferred: no consumer of `managed_dataset/v1` is known (5.4). **Default: defer until a consumer is named; the corrected rule in 5.4 is the starting point.**
- Q8 (LOW-3): F22 output becomes owner-only (files 0600, directories 0700, via B-6 `STAGE_MODE`; main gives `0666 & ~umask`), so a reader under another uid cannot read it; and the descriptor's per-artifact `sha256` reads every output byte once more (R3). **Default: accept both (masked output, V1 rule 3 favours owner-only); the platform handoff confirms its readers share the engine uid. Alternative: keep `0666 & ~umask` and hash while writing, which moves work into c1a.**

## 14. What this plan did not verify

- The dev-rules rulebooks named in "Rules consulted" were not readable in this environment.
- No build, test suite or mutation was run; only probes P1 to P6 (section 1.3).
- B-6 line numbers are re-pinned to `52df1da8` by reading `git show`, not by running B-6; they move if B-6 is revised before merge. The V1-1 premise (a failed vault-directory fsync keeps the linked leaf, so a retry sees `vault_exists`) is read from `b6:fileaccess/_output.py:302-310` and `b6:fileaccess/_local.py:303-334`, not probed.
- `fsync` on an `O_RDWR` descriptor opened only to read (`adopt_file`) is PostgreSQL's `fsync_fname` convention; behaviour on macOS (`F_FULLFSYNC`) was not examined.
- Whether a platform relationship route hands `vault_writer` to an engine call that also takes a sink (`platform:api/jobs/route_dispatch_exec.py:116`, `:137`, `:154`), which B0 would refuse once B-6 merges, was not traced; the platform handoff owns that.
- The CLI was not grepped for a `CURRENT` or `managed_dataset/v1` consumer (5.4).
- Rule D's chain and anchor (5.0) and L7 are reasoned from `fileaccess/_output.py:45`, `:313-325` and `b6:fileaccess/_local.py:225-239`, and the platform gap from `platform:api/jobs/v2_vault.py:127-137` at `7be71b6`; none was probed. Whether the platform worker has the Q4 requirement was not checked. The `same_rows` cost on a large vault (R4) was not measured.
- `flock` behaviour on NFS or overlay filesystems was not probed.
- Slice sizes are estimates from r5 module sizes plus line counts of the modules each slice edits, not measured diffs.
- Platform and CLI consumers of output directory listings (R5) were not grepped beyond `v2_full_frame.py` and `v2_vault.py`.

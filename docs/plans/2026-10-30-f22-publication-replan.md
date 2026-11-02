Status: plan

Summary: F22 revision 8 (targeted amendment of revision 7, `5ce8322b`; gate order Dennis, then Codex). Build only the idempotent half of publication on main's A-4 `OutputTxn` (no private arena, no verbatim port): a `decoy-publication/v1` descriptor with `result_id`, a destination lock that judges `operation_id`, durable binding, `already_committed` / `operation_conflict` outcomes, an overwrite policy that refuses a different result and never replaces, vault-before-output ordering with the vault digest bound in the descriptor, and rule D: existing state is adopted or reported only when it is durable along its whole directory chain to the anchor and equal to this request (output by measured `result_id`, never by `operation_id` alone; vault by rows). The managed `CURRENT` pointer (old a4) is deferred: no consumer. Eight gateable engine slices in three groups.

Baseline: engine `main` at `0173c054` (contains A-4, `da1a522f`), merged into this branch; main `f5b46a2c` (0.18.0 release prep), merged in revision 8, moves no line this plan cites (`git diff 0173c054 f5b46a2c` leaves `fileaccess/`, `vault.py`, `reasons/` and the cited `execution/` modules unchanged; `quarantine.py` stays 638 lines). B-6 vault core: branch `feat/b6-vault-core` at `52df1da8` (re-pinned from `ec3f4de3`; adds the `fchmod` private-mode restore, `b6:fileaccess/_output.py:74-85`, `b6:fileaccess/_local.py:255`, `:273`), read-only, not merged (slice b waits for it; a1a and a1b conflict with it mechanically, 9). r5 implementation: `2fa4f8a5`, read-only reference; this branch no longer carries it (commit `e7ca46a`).

Risk: R2 for this plan; the build is R3 (publication, persisted format, crash ordering).

Rules consulted: `/home/cam/dev-rules` (owner's box): handoff-named `00-universal`, `development-loop`, `risk-and-exceptions`, `reuse-first`, `scope-discipline`; README-selected rulebooks `data-and-migrations`, `testing`, `security`, and `documentation` (named by engine `CLAUDE.md` for the plan lifecycle); decoy-platform `handoffs/queue` `docs/handoffs/`: `DEFECT-CATALOG.md` (plan contract, classes, worker rules), `V1-BAR.md`, `HOSTILE-PLAN-REVIEW.md`, `OWNER-DECISIONS-2026-10-30.md`, `tools/plan-lint`; engine `CLAUDE.md` (600/700 census, pre-GA hard delete, established methodology); POSIX `rename(2)`, `link(2)`, `fsync(2)`, `flock(2)`; Linux `renameat2(2)` (`RENAME_NOREPLACE`, man-pages on Debian 12).

Grounding: `file:line` is at `0173c054` (paths under `src/decoy_engine/` unless they start with `tests/` or `docs/`). `b6:` means `52df1da8`, `r5:` means `2fa4f8a5`, `platform:` means decoy-platform `main`, `pv:` means decoy-platform `fix/vault-before-output-order` `502a8228`. Probes ran from `uv.lock` (`uv sync --frozen --extra dev`; Python 3.11.18, PyArrow 24.0.0, DuckDB 1.5.4) on `0173c054`; results in section 1.3.

# F22 publication: re-plan (revision 8)

## 0. Review dispositions (rev 7 `5ce8322b`: Codex NO-GO, 1 V1; Dennis GO, 0 V1)

| Finding | Class | Disposition | Where | New test |
|---|---|---|---|---|
| V1-1 (Codex f22r7): under a umask that strips an owner bit, a SIGKILL between `mkdirat` and `fchmodat` left a same-uid directory that every retry refuses by 5.0 rule (2) | 3, 2 (V1 rule 1) | a new directory is built as a 0700 stage and exposed by `renameat2(RENAME_NOREPLACE)`, so it never appears under its real name without its final mode | 2 (G1, L1, L2), 3, 5.0, 5.1, 7.2, 9, 11, 14 | `test_mkdir_crash_window_retry_publishes` |

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
| B-6 vault core (not merged) | `b6:fileaccess/_output.py:49` (`STAGE_MODE`), `:55` (`DURABLE_LABELS={"vault"}`), `:74-85` (`fchmod` restore), `:278-310` (durable link publish), `b6:fileaccess/_local.py:262-275`, `:303-334`, `b6:fileaccess/adapters/serial.py:51-72` (`write_durable`), `b6:vault.py:357-385` (`_publish`) | durable exclusive vault: stage 0600, write, file fsync, `link`, directory fsync as the commit point; `vault_exists`, `vault_commit_failed`. A failed directory fsync keeps the linked leaf (`b6:fileaccess/_output.py:302-310`), so a later `vault_exists` can name a non-durable leaf |
| B-6 boundary rule B0 (not merged) | `b6:execution/_pipeline_context.py:254`, `b6:execution/_chunked.py:435`, `b6:execution/native/_chunked_entry.py:650`, `b6:execution/out_of_core/_runner.py:196` | the engine refuses a vault writer wherever it would publish output itself (`vault_requires_resident_output`) |

### 1.2 Durability: does `OutputTxn` fsync the file?

No, on main, on either policy: stage files close with no `fsync` (`fileaccess/adapters/arrow.py:41-70`); a dir stage is renamed with no fsync (`fileaccess/_output.py:271`; `fileaccess/_local.py:263-270`); a `replace` publish swallows its parent fsync failure (`fileaccess/_output.py:71-77`; `fileaccess/_local.py:178-193`); a `link` publish has none (`fileaccess/_output.py:253-269`). B-6 makes only the `vault` label durable. A crash can leave a bound unit with torn Parquet (V1 rule 2); a1 adds a durable dir publish.

### 1.3 Probes (main `0173c054`, from `uv.lock`)

Probe scripts ran from a scratchpad, not committed; each becomes a test in 7.2.

- P1: two `ParquetTransactionalSink`s, same target, same result, staged then committed in turn: the second raises `OSError` errno 39 (`ENOTEMPTY`). There is no `already_committed`; a retry of a committed job fails.
- P2: same target, a different result: the same raw `OSError` 39. There is no `operation_conflict`, and the caller cannot tell a retry from a collision. The first unit survives (2 rows).
- P3, P5: `os.fsync` calls during a table write and commit: 0 on the ambient policy, 0 on a strict `RootedFileAccess`.
- P4: a sink that committed zero tables (`ensure_dir`, `execution/_transactional_sink.py:392-395`) leaves an empty directory. A later sink with a different result renames over it and replaces the committed (empty) unit silently.
- P6: after a successful commit, a second `commit()` returns silently and a `write` is accepted into a fresh stage whose commit fails `OSError` 39. There is no sealed state.
- Platform order (read at platform `main` `d8908f42`, not run): both `_persist_job_vault` callers run after output: resident `platform:api/jobs/v2_full_frame.py:168` before `:206`; relationship routes `platform:api/jobs/route_dispatch_exec.py:233-235` before `:247`, where the engine fills a platform-private `stage_dir` (`platform:api/jobs/v2_sequential.py:201-209`, `platform:api/jobs/v2_out_of_core.py:332`) and the platform publishes the real targets. Neither fails the job on a vault failure (`platform:api/jobs/v2_vault.py:76-97`). Q4.

### 1.4 What F22 still owes V1

Only the idempotent half, on top of 1.1: the descriptor with `result_id` (5.2); a destination lock across judge and bind (5.1); outcomes as receipts, not raw `OSError` (5.3); refuse-different, never replace (5.3); durable binding (1.2); rule D (5.0); vault before output (5.5); a sink state table with restart (5.6); the isolated route on the same bind (5.7). The protected backup (Q2) and the managed `CURRENT` pointer (5.4) are deferred.

## 2. Guarantee (V1 rules 1 to 3; ratchet plus named limits)

Rules, each with the test that holds it (7.2):

- **G1 (rule 2, atomic and durable).** A destination holds nothing, the complete prior unit, or the complete new unit. When `publish` returns `published`, every bound file, the unit directory and every directory from the parent up to and including the anchor (5.0) have been fsynced, whoever created them. Tests: `test_durable_dir_publish_fsync_order`, `test_adopt_syncs_ancestor_chain`, `test_ancestor_created_by_other_attempt`, `test_unreadable_ancestor_fail_closed`, `test_mkdir_crash_window_retry_publishes`.
- **G1a (rules 1 and 2, rule D).** F22 never reports or builds on existing state unless, in the same call, it fsynced that state's whole chain to the anchor and found it equal to this request: `already_committed` or a `reconcile` seal only after the unit tree and chain were fsynced and the vault matched this writer (else `commit_uncertain` or `publication_vault_mismatch`); an adopted `VaultCommit` only after the leaf and vault chain were fsynced (else `vault_commit_failed`). Tests: `test_retry_already_committed`, `test_adopt_syncs_ancestor_chain`, `test_vault_adopt_or_conflict`, `test_vault_ancestor_chain_durable`.
- **G2 (rule 2, never replace).** At most one unit is bound per destination. A different result is refused (`target_occupied` or `operation_conflict`), and the prior unit is byte- and inode-identical afterwards. Tests: `test_two_writers_*`, `test_refused_overwrite_survivor`.
- **G3 (rule 1, idempotent retry).** Re-running a committed operation with the same result returns `already_committed`, not a failure, in this or a fresh process; the same `operation_id` with a different result is `operation_conflict`. Tests: `test_retry_already_committed`, `test_restart_recovery`, `test_restart_same_operation_different_result`.
- **G4 (rule 2, vault before output).** No unit with a vault is bound before its vault is durable, written or adopted. The descriptor carries the vault digest; `already_committed` needs that digest on disk and this writer's rows. Tests: 7.2 slices b1, b2, `test_two_writers_same_output_different_vault`.
- **G5 (rule 3, value-free reports).** F22 receipts, the `str()` of F22 errors and the F29 records F22 writes hold no data value, path, file name or OS message text. They carry registered codes, errno names, digests and counts. Test: `test_marker_corpus_user_surfaces`.

Named limits (what is not claimed):

- L1: durability holds where the filesystem honours `fsync(2)` and `flock(2)`, and an anchor (5.0: the strict capability root, the ambient mount boundary, the platform `upload_dir`) is durable by deployment, and so is a directory another uid owns and left unsynced (5.0). If `flock` is not supported, publication is refused (`publication_lock_unavailable`); there is no lock-free fallback. If `renameat2` fails `EINVAL` or `ENOSYS` (no `RENAME_NOREPLACE`), directory creation is refused (5.0 rule 1); there is no fallback to a plain `rename`. A same-uid directory Decoy cannot open (an operator `chmod`, or one made outside 5.0 rule (1)) is refused by 5.0 rule (2); the operator fixes its mode.
- L2: a SIGKILL leaves the attempt's private stage (`_decoy_publication_stage_*`, mode 0700) beside the destination, and empty `._decoy_mkdir_*` directory stages in any parent on the chain (5.0 rule 1). That costs disk only; reclaim belongs to F25 (Q1).
- L3: readers of an immutable directory are not coordinated; they see absent or complete.
- L4: F22 never deletes a committed unit, generation or vault. Expiry is F26 or the operator's job.
- L5: the quarantine sidecar written after the table commit (`execution/_sequential.py:594` then the DE-08 sidecar) keeps today's `publication_partial` contract.
- L6: same-uid hostile processes and hostile in-process objects are out of scope (owner decision 2026-10-30).

## 3. Class table

| # | Class | Closing structure | Proof (test id, 7.2) | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | F22 errors carry the registered summary only, raised outside handlers (B-6 `_publish` pattern, `b6:vault.py:367-385`); receipts hold tokens, digests, counts; artifact names live only in the descriptor (data) | `test_marker_corpus_user_surfaces` (with the receipt field walk) | put the leaf name in a refusal message; add `path` to `ArtifactSummary` |
| 2 | Cleanup missing on an exit path | `OutputTxn` is the one owner of the stage; every non-bind exit of `publish_unit` calls `txn.abort()` in `finally`; a refused or `already_committed` judge discards the stage | `test_exit_matrix_publish` | drop `txn.abort()` on the `already_committed` branch |
| 3 | State machine and concurrency | destination `flock` held across read-judge-bind; sink `SinkState` enum plus a literal transition table (5.6) under one lock; rule D chain and intent before any adopt or report (5.0) | `test_two_writers_*` (judge-bind hook), `test_sink_transition_cells`, `test_restart_recovery`, `test_restart_same_operation_different_result`, `test_adopt_syncs_ancestor_chain`, `test_ancestor_created_by_other_attempt`, `test_unreadable_ancestor_fail_closed`, `test_mkdir_crash_window_retry_publishes`, `test_vault_ancestor_chain_durable` | `mkdirat` straight at the real name; judge before taking the lock; allow `write` in SEALED; issue an adopted `VaultCommit` or `already_committed` before its fsync; stop the chain at the parent, at the first pre-existing ancestor or at any `EACCES`; skip the writer compare on republish; seal in `reconcile()` on `operation_id` |
| 4 | Route parity and silent fallback | every route that binds output calls `publish_unit` (sink commit, `publish_tables`, isolated parent); capability row per route | `test_route_outcome_matrix` | revert the isolated parent to `atomic_commit` |
| 5 | Referential integrity | the unit (all tables plus descriptor) binds with one rename, so FK closure inside a complete unit stays the producer's (F06) | `test_fk_unit_all_or_none` | bind one rename per table |
| 6 | Untrusted producer | the isolated parent measures staged artifacts itself (size, sha256, rows) and builds the descriptor; a child-written descriptor or extra file is refused; descriptor reads are bounded | `test_isolated_parent_measures`, `test_descriptor_bounded_read` | trust a child `_decoy_publication.json`; drop the read cap |
| 7 | Check then use on a path (TOCTOU) | judge and bind under one lock through one `OutputDir` capability (fd-relative on strict policies); no `exists()` then write | `test_two_writers_same_result`, `test_strict_policy_confined` | replace the lock with an `exists()` check |
| 8 | Weak tests and overclaimed evidence | red-first on the slice start commit from `uv.lock`; every generated test carries a one-line non-vacuity assertion (the marker is present in the data, the lock probe saw `EWOULDBLOCK`, the hook fired) | the non-vacuity lines themselves; reviewer reruns | delete a non-vacuity line: the test then passes on a no-op build (checked once) |
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

Vault step (slice b1): `b6:vault.py:366-367` step 1 (stage created, policy error shape), `b6:vault.py:368-374` write, file fsync, link, directory fsync, `b6:vault.py:376-385` the two codes; adoption (D1): `OutputDir.adopt_file` (5.1), then `vault.load_vault(None, key, vault=BytesHandle(blob))` (`b6:vault.py:440`) on exactly the bytes that were fsynced.

### 4.2 Execution routes

- sequential: `execution/_sequential.py:594` (`observed_commit`), `:638` (`observed_abort`); sink `ParquetTransactionalSink`.
- out-of-core: `execution/out_of_core/_runner.py:311`, `:321`; same sink.
- full-frame streamed: `execution/_pipeline_full_frame.py:245` through `OutputPublish` (`execution/_chunked_output_sink.py:156-172`).
- mixed: `execution/_pipeline_mixed_route.py:267`, same `OutputPublish`.
- streamed generation: `generation/_streamed.py:471-474`.
- isolated parent: `execution/_isolated_run.py:647` (`atomic_commit`); child sink `execution/_isolated_worker.py:217` (preparation only).
- isolated in-process fallback: `execution/_isolated_run.py:313-335`.
- resident `publish_tables` (new, slice a3; the vault route, slice b2).
- `_CallableSinkAdapter` (`execution/_transactional_sink.py:107-153`): not a publisher, reports `partial`/`unknown`; out of F22, unchanged.

### 4.3 FK shapes

- single, composite, self-referencing, cross-table chain (parent, child, grandchild), children in a later batch than their parent.
- F22's claim for each is all-or-none of the unit (one rename binds every table plus the descriptor, `fileaccess/_output.py:271`). FK closure inside the unit is the producer's. Tested per route in `test_fk_unit_all_or_none`.

### 4.4 Fields of public result types

Report types (user-visible; G5 applies):

- `CommitReceipt` (new; replaces r5's `r5:publication/_types.py:136-155`): `outcome` (closed `Outcome`), `operation_id` (caller text, shape-checked `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`; contract: one `operation_id` names one intended result for one destination; reusing it for a different result is `operation_conflict`, and a matching `operation_id` is never taken as evidence of an equal result; stated again in `docs/publication.md`), `reason_code` (registered or `None`), `result_id`, `descriptor_digest` (hex64), `vault_digest` (hex64 or `None`), `artifacts` (`ArtifactSummary`), `visible`, `durable` (bool), `os_error` (errno name or `None`). Dropped from r5: `generation`, `backup_ref` (relative paths), `backup_status`, `evidence_digest`.
- `ArtifactSummary`: `role`, `size`, `rows`, `sha256`. No path, no name.
- `PublicationEvidence` (`execution/_publication_observer.py:42-49`): `state`, `cleanup`, `last_operation`, `stored_bytes` (config-declared table names, F29-owned; unchanged).
- `IsolatedRunResult` (`execution/_isolated_common.py:321-388`): adds `publication_receipt`; `error` keeps `commit_error_text` (`execution/_isolated_common.py:253-259`).
- `ExecutionResult` (`execution/_adapter.py:43`), `GenerationResult` (`generation/_streamed.py:80`): add `publication_receipt`.

Data carriers (data by contract; never sent to a user-visible surface whole):

- `PublicationRequest`: `operation_id`, `unit` (`immutable_directory/v1` only; `managed_dataset/v1` and `expected_current` deferred with a4), `overwrite` (`"refuse_different"` only), `vault` (`VaultBinding | None`), `lock_timeout_s`.
- `VaultBinding(writer, out)` (no anchor argument: the chain is derived, 5.0); `VaultCommit(sha256, size, entry_count, adopted)` (issued only by the vault module).
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

Pattern: PostgreSQL's `SyncDataDirectory` / `fsync_fname` (state found on disk is fsynced before it is trusted), extended up the path as `durable_rename` does for a created parent; every fd is `O_RDONLY` (Linux fsyncs it; MEDIUM-4). Rule: anything F22 binds, adopts or reports is (a) fsynced in the same call through the same capability along its whole chain, the leaf (file or unit tree) and then every directory from its parent up to and including the anchor, whether this attempt, an earlier one, a concurrent `begin()` or a foreign process created it, and (b) for adoption, equal to this request: output by a measured `result_id` (never by `operation_id`), vault by its logical rows against the current writer. A failed fsync is never reported durable; a difference is never reported as success.

Anchor: strict policies, the capability root (`RootedOutputDir._root_fd`), so the chain is the full `rel` path. Ambient (output and vault alike): the mount boundary, walking up from the leaf's parent and stopping below the first ancestor whose `st_dev` differs or that rule (2) below lets stop; `/` ends the walk. Nothing is recorded, so there is no anchor field and no window where a created-but-unsynced directory becomes an anchor (revision 5's L7). Fsyncing a clean directory is cheap; the walk is a few opens per publish (R3). Unreadable directories (V1-1; classes 3, 7). A directory can be searchable and not readable (`d-wx------`): paths through it resolve, but `open(dir, O_RDONLY)` fails `EACCES`, so it cannot be fsynced. Main leaves new directory modes to the umask (`fileaccess/_output.py:318`, `fileaccess/_local.py:215`, `:233`, `:266`), so umask `0o477` makes one. Two rules, both policies, output and vault chains. (1) Creation: Decoy creates a directory only inside a directory it holds open `O_RDONLY|O_DIRECTORY` (strict already opens each component, `fileaccess/_output.py:312-327`; ambient opens the deepest existing ancestor and creates each missing component from it, replacing `mkdir(parents=True)`); if that open fails (any errno) it refuses before anything is created, errno only (5.1 step 1 mapping, not `publication_target_occupied`). A missing directory `name` appears under its real name only with its final mode (V1-1; pattern: Maildir's write in `tmp/`, then rename): `mkdirat(parent_fd, stage, 0o700)` with `stage` = `._decoy_mkdir_<uuid4 hex>`; `fchmodat(parent_fd, stage, 0o700)` (never `mode & ~umask`; as B-6 `STAGE_MODE`, Q8; umask `0o477` leaves the stage `0o300` until then); open the stage `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`, fsync it, close it; `renameat2(parent_fd, stage, parent_fd, name, RENAME_NOREPLACE)`; then `name` joins the created list. `renameat2` is called through `ctypes` the way `fileaccess/_openat2.py:36-50` binds `openat2` (`ctypes.CDLL(None, use_errno=True)`): glibc exports `renameat2@@GLIBC_2.28`, and `os` has no `renameat2` on Python 3.11 to 3.13. `EEXIST`: another attempt or process made `name` first; `unlinkat(parent_fd, stage, AT_REMOVEDIR)` and continue with `name` as pre-existing (rule 2 unchanged). `EINVAL` or `ENOSYS` (no `RENAME_NOREPLACE`; `rename(2)` lists ext4 from Linux 3.15 and xfs from 4.0): remove the stage and refuse errno only, the 5.1 step 1 mapping, before any entry has the real name (L1). Never a plain `rename(2)`: it replaces an existing empty directory, so a concurrent `begin()` holding an fd on that directory would write into an unlinked directory and lose its output. Rejected: repair-on-retry (`fchmodat` 0700 on a same-uid directory missing owner bits) cannot tell a crashed Decoy directory from an operator's deliberate `0o500` or `0o300` one, because crash modes cover every owner-bit combination. `begin()` keeps the list it created in the txn, never on disk. (2) Walk: the leaf, every directory this attempt created and the parent of the topmost one are fsynced, and any open or fsync failure there raises (refusal before the rename, `CommitUncertain` after it). Above that, on the ambient policy, an `EACCES` on `open` ends the walk only when the directory's owner (`os.stat(..., follow_symlinks=False)`, which needs search permission only) differs from `geteuid()`. An unreadable directory owned by the engine's uid could be an earlier attempt's, so it raises: `vault_commit_failed` on D1, `commit_uncertain` on D2 to D4 and publish step (4), refusal before bind otherwise. Proof: by (1), a Decoy-made directory appears under its real name only with mode 0700, so no crash leaves one there that Decoy cannot open, traverse or create in, and a crash leaves at most an empty `._decoy_mkdir_*` stage, which no chain walk visits (L2); by (2), an unreadable same-uid directory is refused, never treated as a boundary. A directory owned by another uid and left unsynced by its creator is the deployment's (L1).

- D1 vault adopt (5.5 step 2): `adopt_file` chain, then the row compare; failure `vault_commit_failed`, nothing bound.
- D2 `already_committed` (5.3, E4): `sync_chain` on the unit tree and its chain; failure `commit_uncertain` (E4a).
- D3 republish vault check (5.5): `adopt_file` chain, digest and row compare; fsync failure `commit_uncertain`, difference `publication_vault_mismatch`.
- D4 `reconcile` from UNCERTAIN finding a binding with this sink's measured `result_id` (5.6): D2, plus D3 with a vault, before SEALED; failure UNCERTAIN. (D5, an adopted generation and `CURRENT`, is deferred with 5.4.)

### 5.1 Durable dir publish and the lock, in `fileaccess` (slices a1a, a1b; classes 2, 7)

Pattern: SQLite atomic commit and PostgreSQL `durable_rename` (fsync the file, fsync the directory), as B-6 already cites (`b6:fileaccess/adapters/serial.py:51-56`); `flock(2)` advisory lock as in r5 (`r5:publication/_lock.py:1-7`).

- New stage label `publication` (prefix `_decoy_publication_stage_`, stage mode 0700, files 0600 through B-6's `STAGE_MODE` and `fchmod` restore, `b6:fileaccess/_output.py:49`, `:74-85`; Q8). Added to B-6's `DURABLE_LABELS` if B-6 has merged; otherwise a1b introduces the set and B-6 rebases onto it (Q3).
- Durable dir publish, both policies: missing parents are made by 5.0 rule (1) (slice a1a) before the stage exists; then (1) walk the stage fd-relative (no link followed), `fsync` every regular file and every directory, deepest first; (2) `rename(2)` the stage onto the leaf; (3) `fsync` the parent: the commit point; (4) fsync each directory above the parent, deepest first, up to and including the anchor (5.0), pre-existing ones included, with 5.0 rule (2) over the directories `begin()` created. A failure in (1) or (2) raises with errno only and leaves the stage for `abort()`. A failure in (3) or (4) raises `CommitUncertain`; the leaf is bound and is never removed.
- `OutputDir.lock(timeout_s) -> ContextManager`: opens `.<leaf>.decoy-lock` in the leaf's parent (`O_CREAT|O_NOFOLLOW|O_CLOEXEC`, 0600, via the parent fd on strict policies), polls `flock(LOCK_EX|LOCK_NB)` to a deadline, and never unlinks the file. `ENOLCK`/`EOPNOTSUPP`/`EINVAL` raise `LockUnavailable`.
- `OutputDir.read_member(component, *, limit) -> bytes | None`: reads one file inside a bound directory leaf, bounded, through the same capability; `None` when the leaf or member is absent. This is what judge and restart use; no path is handed out.
- `OutputDir.sync_chain() -> None` (D2, D4): the step (1) walk over the bound leaf (directory or file), then the parent and each ancestor to the anchor (5.0), fd-relative from the root on strict policies, `O_RDONLY|O_NOFOLLOW`; raises with errno only. Step (4) of publish is the same walk. Open failures follow 5.0 rule (2): D1 maps a raise to `vault_commit_failed`, D2 to D4 to `commit_uncertain`; on a retry this attempt created nothing, so only the leaf and its parent are strict and an `EACCES` above them raises unless another uid owns the directory.
- `OutputDir.adopt_file(*, limit) -> bytes | None` (D1, D3; LOW-2, `read_member` is directory-only): opens the file leaf via the parent fd (`O_RDONLY|O_NOFOLLOW|O_CLOEXEC`, so a 0400 vault opens; regular file by `fstat` else occupied), reads at most `limit + 1` bytes (over: `LeafTooLarge`), fsyncs that fd and then the chain (as `sync_chain`), and only then returns the bytes; `None` when absent. One fd, so the bytes returned are the bytes made durable.

### 5.2 Descriptor and `result_id` (slice a2; class 10)

Pattern: Git content addressing plus RFC 8785-style canonical JSON (sorted keys, fixed separators, ASCII, no NaN), as `r5:publication/_codec.py:1-10` cited; Delta Lake's transaction identifier for retries.

- `_decoy_publication.json`, schema `decoy-publication/v1`: `schema`, `unit`, `operation_id`, `result_id`, `artifacts` (sorted `[{role, path, size, sha256, rows}]`), `vault` (`null` or `{sha256, size, entry_count}`), `engine_version`.
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
                check_vault(existing, request)         # D3, 5.5; slice b2
                out.sync_chain()                       # D2; OSError: commit_uncertain
            if verdict is not BIND: return receipt(verdict)
            _after_judge()                             # test hook, no-op (7.1)
            vault = commit_vault(request.vault)        # b1, wired in b2; NoVault in a3
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

Test hook (V2-1): `_after_judge` is a module-level no-op inside the lock, after a bind verdict and before any vault or bind I/O; the two-writer tests park the winner there (7.1).

`publish_tables(out, tables, request)`: resident entry. It stages each table with `arrow_adapter.write_table` into a `publication` txn, then calls `publish_unit`.

### 5.4 Managed dataset and `CURRENT` (deferred; was slice a4)

No consumer of `managed_dataset/v1` exists: the platform publishes targets itself (`platform:api/jobs/v2_sequential.py:295-336`) and nothing reads a `CURRENT` pointer (platform grep at `d8908f42`). Deferred (11). Starting rule (V2-4): refuse only same `operation_id` with a different result and a stale `expected_current`; otherwise bind `generations/<result_id>/` through 5.3 and move `CURRENT` by a durable `replace`.

### 5.5 Vault before output (slices b1, b2; rule V, class 3)

Pattern: write-ahead ordering (the log record is durable before the page it describes, as in PostgreSQL WAL), using B-6's durable vault publish (`b6:vault.py:357-385`, commit `b7f70a17`).

Rule V, inside `publish_unit`, under the destination lock, after the judge says bind:

1. `commit_vault(binding)`: `binding.writer.publish(binding.out) -> VaultCommit`. B-6 `_publish` (stage 0600, write and file fsync, exclusive `link`, directory fsync), extended to fsync the vault chain to the anchor (5.0) and to return the `sha256` and entry count of the bytes it fsynced. `VaultCommit` needs the vault module's issuer token (as `fileaccess/_output.py:194`).
2. `vault_exists`: adopt durably (D1) or refuse. `blob = binding.out.adopt_file(limit=2 * len(own) + 64 KiB)` (`own` = the writer's serialized vault; a larger leaf cannot hold the same rows: `publication_vault_conflict` without decrypting). `adopt_file` fsyncs the leaf and its chain before returning; its failure refuses `vault_commit_failed`. Then `same_rows(blob, writer)` (`publication/_vault_order.py`): `load_vault(None, key, vault=BytesHandle(blob))` (`b6:vault.py:440`; `BytesHandle` is a read-only in-memory `FileHandle` with the constant `display` `"<vault>"`, since `b6:vault.py:487` puts it in messages), comparing `(ambiguous_dropped, sorted rows)` with the writer's. Equal: `VaultCommit(sha256(blob), len(blob), entries, adopted=True)`, covering a leaf whose B-6 directory fsync failed or was killed, never rewritten. Different, or any `VaultError` from `load_vault` (wrong key, bad frame, unreadable): refuse `publication_vault_conflict`, vault untouched.
3. `vault_commit_failed` from `_publish`: refuse; nothing is bound. A linked vault is kept (B-6 rule 7); the next retry reaches step 2 and fsyncs it.
4. Only now `write_descriptor(txn, new, vault)` and `txn.publish()`. `write_descriptor` and the bind take `VaultCommit | NoVault`, and `NoVault` is accepted only when `request.vault is None`.

Republish (`already_committed` candidate, D3): `request.vault` and the descriptor's `vault` must both be set or both `null` (LOW-1), else `publication_vault_mismatch`. If set, `binding.out.adopt_file(limit=descriptor vault.size)` fsyncs the chain and reads the leaf; size and `sha256` must match the descriptor, and `same_rows(blob, request.vault.writer)` must hold (V1-3: equal masked output can carry a different reverse map, X for Alice in the bound run, X for Bob in this one); missing, larger, different digest, different rows or a blob this writer's key cannot open: `publication_vault_mismatch`; fsync failure: `commit_uncertain`. Then D2 and `already_committed`. Nothing is written, and `writer.publish` is never called, so a retry makes no second vault.

Ordering is by construction: the bind needs a `VaultCommit`, which exists only after the vault chain fsync returned in this call (step 1 or step 2). "Output bound, vault missing" needs an out-of-band delete, and E7 reports it.

Route scope: B-6 rule B0 stays. Every engine sink keeps refusing a vault writer; an engine vault job whose target is a local engine `OutputDir` publishes through `publish_tables(..., request.vault=...)` (resident output). Platform routes that publish targets themselves order the vault outside the engine and owe the same chain (Q4). Lifting B0 for F22 sinks is Q3.

### 5.6 Sink state table and restart recovery (slices c1a, c1b; class 3)

`ParquetTransactionalSink(out, *, request=None)`: with no request, `operation_id` is a fresh uuid4, so the sink is not idempotent across processes but is still judged. Commit calls `publish_unit`. States: `OPEN`, `WRITING`, `COMMITTING`, `RECOVERING`, `SEALED(R)`, `UNCERTAIN`, `REFUSED`, `FAILED`, `ABORTED`. One `RLock`; transitions under it; I/O outside it.

| state | write | commit | reconcile | abort |
|---|---|---|---|---|
| OPEN | WRITING | COMMITTING | no-op, returns `None` (seals nothing; V1-B) | ABORTED, txn aborted |
| WRITING | busy `publication_write_in_progress` | busy | busy | pending |
| COMMITTING | busy `publication_commit_in_progress` | busy | busy | pending |
| RECOVERING | busy `publication_recovery_in_progress` | busy | busy | pending |
| SEALED(R) | refuse `publication_attempt_sealed` | return R | return R | no-op |
| UNCERTAIN | refuse | refuse `publication_commit_uncertain` | RECOVERING | no-op |
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
| RECOVERING | found with this sink's measured `result_id`, D4 ok (unit tree and chain to the anchor) | SEALED |
| RECOVERING | found with this `result_id`, D4 fsync fails | UNCERTAIN |
| RECOVERING | absent, or a different `result_id` | REFUSED `publication_commit_not_visible` |

A pending abort applies on the exit from WRITING, COMMITTING or RECOVERING, except when the exit is SEALED or UNCERTAIN. Restart (V1-B): a fresh sink with the same `operation_id` runs the job and calls `commit()`; `publish_unit` measures the staged result and judges it: equal `result_id` gives `already_committed` after D2 and D3 (SEALED), a different one `operation_conflict` (REFUSED) with the bound unit untouched. Only a sink that measured its own result (UNCERTAIN) can be sealed by `reconcile()`. Routes do not call `reconcile()` before the first write. Evidence: `SinkState` maps onto `PublicationState`/`CleanupState` in the sink (SEALED to `published`, UNCERTAIN to `unknown`, REFUSED/FAILED/ABORTED to `not_published`), so `observed_commit` keeps working for foreign sinks unchanged.

### 5.7 Isolated route on the same bind (slice c2; classes 4, 6)

- The parent opens a `publication` txn for `output_dir` on the ambient policy (the route is ambient-only, `execution/_isolated_run.py:261-272`). It hands `txn.local_stage_path()` (`fileaccess/_local.py:255`) to the child as `staging_output_dir`, and on success calls `publish_unit`. The parent measures the artifacts and refuses any child-written descriptor (class 6).
- The child writes plain staged files only (V2-5): no publishing sink, lock, descriptor or rename. Its `ParquetTransactionalSink(Path(staging_output_dir))` (`execution/_isolated_worker.py:217`) would, after c1, bind a unit with a descriptor inside the parent's stage; c2 swaps it for `StagedParquetWriter` (the sink's write half split out in c1a). The parent is the only descriptor writer.
- The in-process fallback (`execution/_isolated_run.py:313-335`) does the same through `publish_tables`.
- `_isolated_commit.sibling_staging_dir`, `stage_tables` and `atomic_commit` (`execution/_isolated_commit.py:56-86`) are deleted (pre-GA).

## 6. Reuse inventory

- A-4 `fileaccess`: `OutputDir`, `OutputTxn`, both policy implementations, `validate_component`, issuer token, `_rmtree_fd`, `local_stage_path`, `arrow_adapter`. F22 adds the `publication` label, `lock`, `read_member`, `sync_chain`, `adopt_file` and the durable dir publish (about 170 lines), not a second mechanism.
- B-6 core (`52df1da8`): `DURABLE_LABELS`, `STAGE_MODE`, `serial.write_durable`, `vault._publish`, `vault_exists`, `vault_commit_failed`, `load_vault`, rule B0. F22 extends `_publish` to return a `VaultCommit`; it does not copy it.
- F29: `PublicationEvidence`, `observed_commit`, `observed_abort`, `capture_publication_failure`, `PublicationState`, `CleanupState`, the reason registry (`reasons/_registry.py:24`) and its tests and fixtures.
- Sentries: `tests/sentry/test_module_size.py`, `test_physical_seam_disconnection.py`, `test_public_import_boundary.py`, `file_access_registry.toml`.
- r5 (`2fa4f8a5`), rules only, not copied: canonical JSON and strict parse (`r5:publication/_codec.py`), no lock-free fallback (`r5:publication/_lock.py`), outcome vocabulary (`r5:publication/_types.py:19-27`).
- New, because nothing on main or B-6 has it: the descriptor, the judge, rule D adoption, the sink state table.

## 7. Hostile corpus (acceptance tests, red first)

### 7.1 Rules for every test

- Red on the slice's start commit, from `uv.lock`, same environment as the branch; ids in the slice evidence file. A test green at baseline is characterization, paired with its mutation.
- Crash tests run in a `spawn` child whose one monkeypatch wrapper sends itself SIGKILL after the named step; the parent asserts death by SIGKILL and the step's marker file (non-vacuity).
- Two-writer tests are ordered by the `_after_judge` hook (5.3), not by timing: winner W sets `w_in_hook` and waits on `l_probed`; loser L waits on `w_in_hook`, must get `EWOULDBLOCK` from a `flock(LOCK_EX|LOCK_NB)` probe (non-vacuity), sets `l_probed`, then calls `publish_unit` and judges W's binding. Under "judge before lock" L's judge sees no binding on every run.
- Every generated test asserts its non-vacuity in one line (marker present in the data, hook fired, probe saw `EWOULDBLOCK`). No oracle-twin harness (V2-LOG).

### 7.2 Tests per guarantee

| Test | Slice | Holds | Red before (commit, expected failure) |
|---|---|---|---|
| `test_durable_dir_publish_fsync_order` (both policies, by spy order: every staged file and directory, `rename`, the parent, then each directory to the anchor deepest first, pre-existing ones included (ambient boundary fixed at a `tmp_path` ancestor by patching the `st_dev` probe; `EACCES` cases are in `test_unreadable_ancestor_fail_closed`)) | a1b | G1 | `0173c054`: 0 fsync calls (probes P3, P5) |
| `test_ancestor_created_by_other_attempt` (V1-A; into `a/b/out`, `a` made by: (i) a `spawn` child SIGKILLed between `mkdir(a)` and its fsync; (ii) a live `begin()` held there by a hook while this one gets `EEXIST`; (iii) a foreign `os.mkdir`. Attempt B's `published` comes only after the spy saw the unit tree, `b`, `a` and each ancestor to the boundary; a model dropping every entry whose parent was not fsynced after its creation still reaches the output; non-vacuity: marker or hook fired, `a` pre-existed B) | a1b | G1 | rev 5 design: B's chain stops at `a` (L7), the model loses the output; mutation: stop at the first pre-existing ancestor; `0173c054`: absent |
| `test_unreadable_ancestor_fail_closed` (V1-1; output chain, and the vault chain in b1; `EACCES` injected by wrapping `os.open` for the seeded path, so it runs under any uid and is never skipped under root: (i) `p/a/out`, `p` and `a` seeded, then `p` a same-uid mode 0300 directory that Decoy did not make through 5.0 rule (1) (an operator `chmod`; separately, main's `mkdir(parents=True)` under umask `0o477`): attempt B never returns `published` (refused before bind with nothing bound, or `commit_uncertain` when injected after the rename; leaf absent or kept per 4.1; the operator fixes the mode, L1); (ii) `begin()` into `q/new/out`, `q` mode 0300: refused, `q` gains no entry; (iii) an unreadable ancestor above every created directory, owner probe patched to another uid (no root needed): the walk stops there and B returns `published`, so a path through a 0711 home still works; non-vacuity: the `open` spy recorded `EACCES` on the seeded directory in each case) | a1b, b1 | G1 | rev 6 design (5.0, `test_durable_dir_publish_fsync_order`): case (i) returns `published` with the chain stopped at `p`; mutation: end the walk at any `EACCES`, case (i) red; mutation: `mkdir(parents=True)` without opening the parent, case (ii) red; `0173c054`: absent |
| `test_mkdir_crash_window_retry_publishes` (V1-1; output chain, and the vault chain in b1; each case in a `spawn` child under umask `0o477` publishing into `p/a/out` with `p` and `a` absent, SIGKILLed at a hook marker: (k1) after the stage `mkdirat`, before `fchmodat`; (k2) after `fchmodat` and the stage fsync, before `renameat2`; (k3) after `renameat2`, before the parent fsync. A fresh-process retry under umask `0o477` returns `published`; the output is at `p/a/out`; every real-name directory on the chain has mode 0700; the only extra entries are empty `._decoy_mkdir_*` stages (k1, k2); the fsync spy saw each stage fsynced before its `renameat2` and (from a1b) the unit tree, `a`, `p` and each ancestor to the anchor. (iv) a hook makes an empty `a` and holds an fd on it between this attempt's stage `mkdirat` and its `renameat2`: `EEXIST`, the stage is removed, `a`'s `(st_dev, st_ino)` equals the hook's, and the publish continues through it. (v) `renameat2` patched to fail `EINVAL`, then `ENOSYS`: refused errno only, no entry named `a`, no stage left. Root: `os.open` and the child `mkdirat` are wrapped to raise `EACCES` on a directory whose `lstat` mode lacks the owner bit the call needs, so the test runs under any uid and is never skipped under root. Non-vacuity: the kill marker is present in k1 to k3, and in k1 the stage's `lstat` mode is `0o300`) | a1a, b1 | G1 | rev 7 design (`5ce8322b` plan:174, plan:322): with no stage, k1 leaves `p` at `0o300` under its real name and the retry raises by 5.0 rule (2); mutations: `mkdirat` at the real name, k1 red; plain `rename` for `RENAME_NOREPLACE`, case (iv) red (inode changes); plain `rename` fallback on `EINVAL`, case (v) red; drop the stage fsync, the spy order red; `0173c054`: absent |
| `test_durable_publish_faults` (fsync file EIO, fsync stage EIO, rename EIO, then EIO on each chain fsync; the chain faults give `CommitUncertain` with the leaf kept) | a1b | G1 | `0173c054`: no fsync, so no failure raised; red |
| `test_lock_contention_and_unsupported` (`EWOULDBLOCK` to deadline gives `LockTimeout`; patched `ENOLCK` gives `LockUnavailable`; the lock file is never unlinked) | a1b | G2 | absent API: red by `AttributeError`; mutation: return without `flock` |
| `test_strict_policy_confined` (a symlinked leaf parent is refused, lock and `read_member` stay under the root) | a1b | class 7 | characterization of A-4 plus the new methods; mutation: open the lock by path |
| `test_descriptor_canonical_and_strict`, `test_descriptor_schema_snapshot`, `test_descriptor_bounded_read` | a2 | class 10, 6 | absent: red; mutation: drop the duplicate-key hook |
| `test_retry_already_committed` (same and new process, same `operation_id` and tables; D2 fsync spy: every unit file and directory and the chain before the receipt; a unit with files 0400 and directories 0500 still gives `already_committed`, MEDIUM-4) | a3 | G3, G1a | `0173c054`: `OSError` 39 (P1); mutation: drop `out.sync_chain` |
| `test_adopt_syncs_ancestor_chain` (V1-1; publish into `a/b/out`, `a`, `b` absent, both policies; SIGKILL after the rename and before each chain fsync in turn; fresh-process retry: spy shows unit tree, `b`, `a`, each ancestor to the anchor, in order, before `already_committed`; EIO on each gives `commit_uncertain`, `durable=False`; non-vacuity: kill marker present) | a3 | G1, G1a | `c96d73e0` design: retry fsyncs `out` and `b` only; mutation: `sync_chain` stops at the parent; `0173c054`: absent |
| `test_two_writers_different_result` (judge-bind hook, 7.1; different tables; case 1 W and L share `operation_id`: `operation_conflict`; case 2 different ids: `publication_target_occupied`): exactly one binding, equal to the winner's | a3 | G2 | `0173c054`: loser gets raw `OSError` 39, no outcome (P2) |
| `test_two_writers_same_result` (judge-bind hook; same tables, same `operation_id`): exactly one `published`, one `already_committed`, one binding | a3 | G2, G3 | `0173c054`: loser `OSError` 39 |
| `test_refused_overwrite_survivor` (after each refusal above: the prior unit's tree, bytes, inode numbers and descriptor digest are unchanged; no stage left) | a3 | G2 | characterization at `0173c054` for non-empty units; red for the empty unit (probe P4 replaces it) |
| `test_exit_matrix_publish` (E1 to E14 and E4a injected; asserts outcome, binding untouched or bound per 4.1, stage aborted) | a3 | class 2 | absent: red |
| `test_marker_corpus_user_surfaces` (every `CommitReceipt`/`ArtifactSummary` field is a closed token, hex64, int, bool or a shape-checked id; `PATHMARK` in the destination path, `TBLMARK` as a table name, `OSMARK` as injected `OSError` text, across E1 to E14: absent from receipts, `str(exc)`, `ReasonRecord`, `IsolatedRunResult.error`; non-vacuity: `TBLMARK` is present in the descriptor's `artifacts[].path`) | a3, c2 | G5 | absent: red; mutation: put the leaf in the refusal text |
| `test_vault_order_is_structural` (spies on `os.link`, `os.fsync`, `os.rename`: the vault link and the vault directory fsync precede the output rename; with each vault step faulted (stage write, file fsync, link EIO, dir fsync EIO, `vault_exists` with different content) the output leaf is never bound) | b2 | G4 | absent: red; mutation: call `commit_vault` after `txn.publish()` turns it red |
| `test_output_bound_vault_missing_unreachable` (one SIGKILL after each hook point: vault stage write, vault file fsync, vault `link`, each vault chain fsync, descriptor write, stage fsync walk, output `rename`, each output chain fsync; after each, a bound output implies a vault leaf whose sha256 equals the descriptor's; after `commit_vault`, a fresh retry gives `published` with the vault inode unchanged) | b2 | G4, G1a | `0173c054`: platform-shaped order (output, then `w.write()`, SIGKILL between) leaves output without vault; `52df1da8`: B0 refuses, F22 API absent; mutation: swap the order |
| `test_republish_requires_vault_digest` (bound unit; vault deleted, or replaced by another valid vault: `publication_vault_mismatch`; untouched: `already_committed`; LOW-1 presence both ways: mismatch; D3 fsync EIO: `commit_uncertain`; nothing written in any case) | b2 | G4 | absent: red; mutation: skip the digest compare |
| `test_two_writers_same_output_different_vault` (V1-3; W binds tables T, vault rows X for Alice; L has T, rows X for Bob, same and different `operation_id`, sequential and under the hook: `publication_vault_mismatch`, vault bytes and inode unchanged; non-vacuity: equal `result_id`, and W's rows get `already_committed`) | b2 | G4, G1a | `c96d73e0` design: L gets `already_committed`; mutation: skip `same_rows` on republish |
| `test_vault_adopt_or_conflict` (equal rows adopted only after the spy saw leaf fsync then the chain, before `VaultCommit`; a 0400 vault leaf is adopted (MEDIUM-4); leaf or chain fsync EIO, or a leaf `open` error: `vault_commit_failed`, not bound; different rows, over the cap, or a vault under another key (LOW-1; no key, path or `display` text in `str(exc)`): `publication_vault_conflict`; vault bytes and inode unchanged) | b1 | G4, G1a | absent: red; mutation: issue `VaultCommit` before the fsyncs |
| `test_vault_ancestor_chain_durable` (V1-2; vault in a `vaults` directory just created, ambient and strict: spy shows leaf, `vaults`, each ancestor to the anchor before the `VaultCommit`, in `_publish` and in adopt; EIO on each: `vault_commit_failed`, leaf kept; after the fault clears, or after SIGKILL between `link` and each chain fsync, a retry spies the chain and gets an adopted `VaultCommit`) | b1 | G4, G1a | `c96d73e0` design: leaf and `vaults` only; mutation: chain stops at the vault directory |
| `test_sink_transition_cells` (every cell of both 5.6 tables, with hook-held in-flight calls; asserts the arriving result, final state and no I/O under the lock) | c1a | class 3 | `0173c054` (probe P6): after a successful commit a second `commit()` returns silently, a `write` is accepted into a new stage, and its commit fails `OSError` 39; no SEALED state; red |
| `test_restart_recovery` (child SIGKILLed after the bind rename; fresh sink, same `operation_id` and tables: `commit()` gives `already_committed` after the D2 spy, SEALED, `write` refused `publication_attempt_sealed`; an UNCERTAIN sink's `reconcile()` seals after D4, D4 EIO leaves UNCERTAIN; `reconcile()` on OPEN returns `None`) | c1b | G3, G1a | `0173c054`: the fresh sink fails `OSError` 39 |
| `test_restart_same_operation_different_result` (V1-B; unit bound for `op-1` with result A; fresh sink with `op-1` writes B: `reconcile()` seals nothing, `commit()` gives `operation_conflict`, REFUSED, A's tree, inodes and descriptor digest unchanged, no receipt for A returned; non-vacuity: B's measured `result_id` differs from A's) | c1b | G2, G3 | rev 5 design: `reconcile()` before the write seals and returns A's receipt; red by mutation: seal in `reconcile()` on `operation_id` |
| `test_route_outcome_matrix` (sequential, out-of-core, full-frame streamed, mixed, streamed generation, isolated parent, isolated fallback, `publish_tables`: each returns a receipt; a retry gives `already_committed`; a conflict gives `operation_conflict`) | c1b, c2 | class 4 | `0173c054`: the isolated route raises `CommitError`, the others raise `OSError` 39 |
| `test_fk_unit_all_or_none` (4.3 shapes by route, SIGKILL mid-stage or a refused commit: the destination holds all tables or none) | c1b | class 5 | characterization; mutation: one rename per table |
| `test_isolated_parent_measures` (child plants `_decoy_publication.json` or an extra file: refused; sizes and digests come from the parent; the unmodified child leaves no descriptor, lock file or nested unit in the stage, V2-5) | c2 | class 6 | absent: red |
| `test_sink_characterization` (P1 to P6 behaviours pinned before c1a changes the sink, then deliberately updated in c1a's red commit) | c1a | class 9 | characterization |

## 8. Frozen contracts touched

- `decoy-publication/v1` descriptor: new (r5 never merged); frozen in a2 by `test_descriptor_schema_snapshot`.
- F29 reason registry: additive codes (`publication_lock_unavailable`, `publication_lock_timeout`, `publication_target_occupied`, `publication_operation_conflict`, `publication_commit_uncertain`, `publication_commit_not_visible`, `publication_vault_mismatch`, `publication_vault_conflict`, `publication_write_in_progress`, `publication_commit_in_progress`, `publication_recovery_in_progress`, `publication_attempt_sealed`, `publication_attempt_aborted`). Registry minor bump to the next version after main's at slice start (`1.3.0` today, `1.4.0` if B-6 lands first), manifest and consumer-contract fixtures regenerated.
- A-4 `OutputDir` protocol: additive (`lock`, `read_member`, `sync_chain`, `adopt_file`); `StageLabel` gains one label (`publication`); `tests/sentry/file_access_registry.toml` updated (including the new `fileaccess/_mkdir.py`).
- `TransactionalSink` protocol (`execution/_transactional_sink.py:66-104`): methods unchanged; `ParquetTransactionalSink.commit()` returns a `CommitReceipt` (was `None`); new keyword `request`.
- On-disk output: every engine unit gains `_decoy_publication.json` (pre-GA; Q5).
- Vault file format (`VAULT_FORMAT_VERSION`): unchanged. `VaultWriter.write` keeps its signature; `VaultWriter.publish` is new; `vault.BytesHandle` is new (internal). Descriptor carries `vault.size` and no anchor field (new schema, frozen in a2).
- `IsolatedRunResult`, `ExecutionResult`, `GenerationResult`: additive `publication_receipt`. `CommitError` from `_isolated_commit` is deleted (pre-GA).

## 9. Slices

Repo `decoy-engine`, one integration branch from `main` per group. Each slice lands its red tests in its own first commit and updates `docs/publication.md` (new) for what it adds. Size = production plus test lines changed, estimated from r5's measured modules with the dropped features removed (r5: `_codec.py` 239, `_lock.py` 81, `_local_directory.py` 419, `_managed.py` 582, `_types.py` 209). Cap: 10 production files and 800 lines per slice. a4 (managed dataset) is deferred (5.4). Group (a) is four slices (a1 split, revision 8), group (b) two and group (c) three (c1 pre-split, V2-3). The platform half of vault ordering is not an engine slice; it is two platform changes, listed below so the order is visible (V1-C): `p1-platform-vault-before-output-order-fix` (branch `fix/vault-before-output-order`) targets the current engine pin (`mkstemp`, `link`, `_same_vault` adopt-or-fail, `502a8228`); the post-B-6 conversion is the separate consumer slice B-6 S6/S7 and lands with the B-6 repin (13 Q4).

| Slice | Content | Prod files | Size (prod + tests) | Depends on |
|---|---|---|---|---|
| a1a | 5.0 rule (1): stage `mkdirat`, `fchmodat` 0700, stage fsync, `renameat2(RENAME_NOREPLACE)` through `ctypes`, `EEXIST` adopt and `EINVAL`/`ENOSYS` refusal, both policies (replaces the strict create loop at `fileaccess/_output.py:316-320` and the ambient `mkdir(parents=True)`) | 3 (`fileaccess/_mkdir.py` new, `_output.py`, `_local.py`) | about 80 + 160 = 240 | main |
| a1b | durable dir publish with the chain to the anchor (mount boundary on ambient), `publication` label, `OutputDir.lock`, `read_member`, `sync_chain`, `adopt_file` (`O_RDONLY`) with chain, both policies; the spy clause of `test_mkdir_crash_window_retry_publishes` | 4 (`fileaccess/_output.py`, `_local.py`, `_types.py`, `__init__.py`) | about 260 + 530 = 790 | a1a; a1a and a1b rebase mechanically on B-6 `52df1da8` (`fchmod` restore and `DURABLE_LABELS` in the same two modules) whichever lands second |
| a2 | descriptor codec (vault `size`), `result_id`, request and receipt types, strict parse | 3 (`publication/__init__.py`, `_descriptor.py`, `_types.py`) | about 280 + 200 = 480 | a1b |
| a3 | `publish_unit` judge and bind under the lock, D2 chain on `already_committed`, `_after_judge` hook, outcomes, `publish_tables`, registry codes | 4 (`publication/_publish.py`, `_types.py`, `reasons/decoy/_core.py`, `reasons/_registry.py`) | about 285 + 420 = 705 (fixtures regenerated, not counted) | a2 |
| b1 | `VaultCommit`, `VaultWriter.publish` with the vault chain, `BytesHandle` and `same_rows` (in `_vault_order.py`), adopt durably (D1) or refuse | 2 (`vault.py`, `publication/_vault_order.py`) | about 115 + 265 = 380 | a3 and B-6 core merged |
| b2 | Rule V inside `publish_unit`, republish presence, digest and writer-row check (D3) | 1 (`publication/_publish.py`) | about 105 + 290 = 395 | b1 |
| c1a | sink state table and transition cells, `StagedParquetWriter` split out of the sink's write half, characterization | 2 (`execution/_sink_state.py` new, `_transactional_sink.py`) | about 245 + 290 = 535 | a3 |
| c1b | `reconcile` from UNCERTAIN with the D4 chain, restart through `commit()`, evidence projection, route wiring: receipts out | 10 (`execution/_transactional_sink.py`, `_publication_observer.py`, `_adapter.py`, `generation/_streamed.py`, `_sequential.py`, `out_of_core/_runner.py`, `_pipeline_full_frame.py`, `_pipeline_mixed_route.py`, `_pipeline_context.py`, `_chunked_output_sink.py`) | about 145 + 430 = 575 | c1a |
| c2 | isolated parent and fallback on `publish_unit`; child on `StagedParquetWriter`; delete `atomic_commit` | 4 (`execution/_isolated_run.py`, `_isolated_commit.py`, `_isolated_worker.py`, `_isolated_common.py`) | about 100 (net negative) + 260 = 360 | c1b |
| platform P1 (external) | `fix/vault-before-output-order` `502a8228`: vault durable before any target on both `_persist_job_vault` sites, on the current engine pin; vault failure fails the job | decoy-platform only | sized by that handoff | none |
| platform P2 (external, B-6 S6/S7) | `_persist_job_vault` on the B-6 shape (13 Q4), in the same change as the B-6 repin | decoy-platform only | sized by that handoff | P1; engine B-6 release |

Total engine about 4,460 lines (revision 7: about 4,220). Revision 8 adds a1a, about 240: production about 80 (the `renameat2` helper and the shared creation function in `fileaccess/_mkdir.py`, call sites in `_output.py` and `_local.py`), tests about 160 (`test_mkdir_crash_window_retry_publishes`). Old a1 (estimate 790) re-estimated with the helper and new test is about 1,030, over the 800 cap, so it split: a1a as above, a1b the rest at about 790 (the creation code moved out, the new test's spy clause moved in). a1b sits at the cap: if it measures over, its vault-chain and adopt cases move to b1. Plan text: to pay for the new 5.0 rule (1) text and 7.2 row, revision 8 cut revision 7's section 0 paragraph and rows (one row now), the `fchmodat` versus `fchmod` explanation in 5.0, case (i)'s killed-attempt wording in 7.2, revision 7's narrative here, and merged the four command blocks below into one. Next cut candidate: delete the isolated in-process fallback instead of rewiring it (c2, owner decision). c1b is at the 10-file cap; an eleventh module splits it by route (streamed: full-frame, mixed, context, chunked sink; batch: sequential, out-of-core, generation), not the cap.

c1b route edits (V2-3): `execution/_sequential.py:594`, `:638`, `execution/out_of_core/_runner.py:311`, `:321` (receipt from `commit()`); `execution/_pipeline_full_frame.py:245`, `execution/_pipeline_mixed_route.py:267` through `OutputPublish` (`execution/_chunked_output_sink.py:156-172`), request threaded from `execution/_pipeline_context.py`; `generation/_streamed.py:471-474` (receipt on `GenerationResult`).

Module census (main LOC; rule): `execution/_sequential.py` 699, call sites only, net <= 0 (census `tests/sentry/test_module_size.py:116`). `execution/out_of_core/_runner.py` 644 on main, 649 on B-6, net <= 0 against whichever census entry is current (`:120`). These two are c1b's net-zero modules: each added line is paid by a removed one in the same module (the commit-and-observe pair moves behind one helper in `_publication_observer.py`). `_pipeline_context.py` 508 (518 on B-6), `_chunked_output_sink.py` 565: at most 600. `quarantine.py` 638, untouched. `execution/_isolated_run.py` 651, net <= 0 (logic goes to `_isolated_commit.py`). `execution/_transactional_sink.py` 438, at most 600 after c1 (the state machine lives in `_sink_state.py`). `fileaccess/_output.py` 389 (443 on B-6), `fileaccess/_local.py` 378 (471 on B-6), each at most 600 after a1a and a1b (about 110 lines each lands near 555 and 580 on B-6; over 600 moves the walk to a new `fileaccess/_durable.py`). `vault.py` 562 on B-6: b1 adds only `VaultCommit` and `publish` (about 35, near 597), at most 600 with no dense census entry; `BytesHandle`, `same_rows` and the adopt helper live in `publication/_vault_order.py` (LOW-2). New modules at most 600 each.

Commands (from `uv.lock`, one environment; record `python -V`, PyArrow and DuckDB versions):

```bash
uv sync --frozen --extra dev
uv run pytest tests/unit/publication tests/fileaccess tests/sentry -q -p no:cacheprovider
uv run pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/decoy_engine
```

`scripts/dev/gates.sh` does not exist at `0173c054`; the baseline is the same commands on the slice's start commit, both failure sets in the evidence file.

## 10. Worker evidence rules

The handoff worker rules and the V1 bar apply as written. Plan-specific: apply each mutation named in sections 3 and 7.2 once and show its test red; never raise a census number; a repeat of a class this plan closes is stopped and reported.

## 11. V2-LOG (logged, not planned)

From revision 1 (`50ad3ba8`): the PEP 578 containment harness, oracle twins, the AST creation-site census, attack A6 and the safe-parent precondition, `_report.py` exact-type validation, and "every entry point" completeness with proof P7 (r1 lines 44, 164, 216, 244, 246, 274, 282).

Found while framing:

- `execution/_isolated_commit.py:83-86`: `CommitError` text holds the output path and the `OSError` text; it reaches only `__cause__` (users see `commit_error_text`, `execution/_isolated_common.py:253-259`). Deleted in c2 anyway.
- `fileaccess/_local.py:298-302`: `LocalOutputTxn.publish` logs `quarantine sidecar` for any label (admin log).
- `fileaccess/_output.py:71-77`, `fileaccess/_local.py:178-193`: swallowed parent fsync for non-F22 file outputs (quarantine, pack, subset). Durability of those outputs is outside F22.
- `execution/_isolated_commit.py:56-62`: a bare-path sibling stage, theoretical TOCTOU on an ambient-only route; deleted in c2.
- Deferred in revision 4: managed dataset, `CURRENT` CAS and D5 (old a4, about 520 lines, 5.4), with `test_managed_current_cas` and `test_kill_sweep_managed`.
- Engine slice estimates (360 to 790) exclude a3's regenerated fixtures, and both platform slices are "sized by that handoff", so the under-800 claim is not fully demonstrated (Codex, f22d V2-LOG).
- P2 (platform) does not fsync `vaults` after unlinking its comparison sibling, so a crash can leave an encrypted orphan `.tmp` file. Disk litter only (Codex, f22r7 V2-LOG).
- The P2 test text does not directly inject B-6's post-link directory-fsync failure; the adopt branch still covers it. Coverage completeness only (Codex, f22r7 V2-LOG).

## 12. Pre-mortem

- **R1: B-6 core does not merge, or merges changed.** b1 and b2 block; the other slices do not depend on it (Q3).
- **R2: `flock` on a network filesystem behaves as a no-op.** L1. The lock refuses only when `flock` errors. Docs say "local filesystems"; no detection is attempted (V2).
- **R3: fsync cost on large units.** One fsync per file plus directories; units are tens of files. If a bench regresses more than 5%, report it; do not drop the fsync.
- **R4: `same_rows` is slow for a large vault.** It decrypts once per crash retry and per `already_committed` with a vault; correctness of the reverse map outranks it.
- **R5: the descriptor file breaks a consumer that lists every output file.** Q5; the platform `target.file` listing and the CLI are grepped in c1b and a hit is reported, not patched.
- **R6: another round.** Most likely cause: a route missed in 4.2; `test_route_outcome_matrix` fails on it.

## 13. Open questions for the owner (recommended defaults)

- Q1: orphan `_decoy_publication_stage_*` and `._decoy_mkdir_*` stages after SIGKILL (L2) are reclaimed by F25 (supersedes the 2026-10-30 answer: there is no arena, and a stage carries no owner lock). **Default: F25.**
- Q2: protected backup. Nothing is ever replaced and prior generations are kept, so a backup adds no V1 protection. **Default: defer to V2; not built.**
- Q3: B-6 rule B0 stays for every sink; vault jobs publish through `publish_tables` with `request.vault`; lifting B0 for F22 sinks is a later item. `DURABLE_LABELS` comes from whichever of a1b and B-6 merges first. **Default: keep B0; B-6 merges first if ready.**
- Q4: the platform wrote output before the vault and swallowed a vault failure at two sites (1.3); `publish_tables(..., vault=...)` cannot serve the relationship routes (private `stage_dir`, possibly non-local targets, resident-only, Q3). **Default, both sites: the vault is durable before any target is materialised, and a vault failure fails the job; no F22 dependency. Two platform changes (9).** P1, `fix/vault-before-output-order` `502a8228`, on the current pin: the engine writes a `mkstemp` sibling, which is fsynced and `os.link`ed onto `job_<id>.dcyvault`; on `FileExistsError` the existing leaf is adopted only when `_same_vault` holds (both decrypted under the writer's key, compared in memory), else `VaultExistsError` with the leaf untouched; then `vaults`, `upload_dir` and the parent of each directory it created are fsynced (`pv:api/jobs/v2_vault.py:135-146`, `:157-170`, `:189-275`). P2 (B-6 consumer slice S6/S7, the seam `pv:api/jobs/v2_vault.py:211-215` names): B-6 `write(path)` publishes by exclusive `link` and raises `VaultError(code="vault_exists")` on an existing leaf (`b6:vault.py:134`, `:269-297`, `:357-385`). **Default:** after P1's `_ensure_vault_dir`, `writer.write(str(final_path))` directly (no `mkstemp`, no pre-created leaf). On `VaultError` with code `vault_exists` only: `writer.write` to a fresh sibling `.<leaf>.<uuid4 hex>.tmp` in `vaults` (never `mkstemp`, which pre-creates it), then P1's `_same_vault(final_path, tmp, writer)` unchanged. Equal: adopt (fsync the leaf, fsync every directory from `vaults` up to and including `upload_dir`, unlink the tmp; the job continues with the leaf's size and this writer's entry count). Different, or any decrypt or parse failure: `VaultExistsError`, tmp unlinked, job fails. Any other `VaultError` or `OSError`: `VaultPersistError`. The leaf is never unlinked or replaced on any branch (B-6 rule 7). The B-6 consumer slice replaces P1's temp file and link with the direct `writer.write(final_path)` and keeps `_same_vault` only on the `vault_exists` branch. Test `test_persist_job_vault_on_b6_engine` (against `52df1da8`): cell 1, first persist: leaf written, chain fsynced to `upload_dir`, job ok (red on P1, `7be71b68` and `502a8228`: `write` into the `mkstemp` leaf gives `vault_exists`); cell 2, after B-6 linked and fsynced the leaf, a `spawn` child SIGKILLed before the first target publishes, and separately EIO on the `upload_dir` fsync: a retry with the same job id and rows gets `status="ok"`, the leaf's `(st_dev, st_ino)` and bytes unchanged, no tmp left, the fsync spy saw the leaf and the chain to `upload_dir`; cell 3, same job id with different rows or key: `VaultExistsError`, leaf bytes and inode unchanged, no tmp left. Non-vacuity: in cell 2 the spy saw the engine's `vault_exists` before the adopt branch. Red on revision 6's P2 (cell 2 fails `vault_exists`); mutation: map `vault_exists` straight to job failure, cell 2 red. P2 lands with the B-6 repin, or behind an engine-version gate. The orchestrator relays P1 and P2; this plan edits no platform code.
- Q5: every engine output directory gains `_decoy_publication.json`. **Default: accept (pre-GA); document it in `docs/publication.md`.**
- Q6: a chain fsync failure after the rename gives `commit_uncertain` (bound, not durable), and `reconcile` re-fsyncs and seals. B-6 reports the same vault case as `vault_commit_failed` with the vault kept. **Default: keep the two vocabularies; the vault code is B-6's.**
- Q7: managed dataset (old a4). **Default: defer until a consumer is named (5.4).**
- Q8: F22 output becomes owner-only (files 0600, directories 0700, via B-6 `STAGE_MODE`; main gives `0666 & ~umask`), and the per-artifact `sha256` reads every output byte once more (R3). **Default: accept both; the platform handoff confirms its readers share the engine uid. Alternative: keep the umask and hash while writing (moves work into c1a).**

## 14. What this plan did not verify

- Revision 8 changed the summary, baseline, rules and heading lines and sections 0, 2 (G1, L1, L2), 3 (row 3), 5.0 rule (1) and proof, 5.1, 7.2 (one row added, case (i) rewritten, slice ids), 9, 11, 13 (Q1, Q3) and 14, and merged main `f5b46a2c` (no cited line moved). Read for it from `/home/cam/dev-rules`: `README.md` (routing), `00-universal`, `risk-and-exceptions`, `development-loop` (PLAN), `scope-discipline`. glibc `renameat2@@GLIBC_2.28` was read from the devbox libc symbol table (glibc 2.36), and the missing `os.renameat2` checked on CPython 3.11.2, 3.12.13 and 3.13.13.
- No build, test suite or mutation was run; only probes P1 to P6 (section 1.3).
- B-6 line numbers are read by `git show` at `52df1da8`, not run; they move if B-6 is revised. The failed-vault-fsync-keeps-leaf premise (`b6:fileaccess/_output.py:302-310`, `b6:fileaccess/_local.py:303-334`) was not probed.
- `fsync` on an `O_RDONLY` file or directory fd is accepted on Linux (ext4, xfs); behaviour on macOS (`F_FULLFSYNC`) was not examined. The `st_dev` boundary was not probed on overlay or bind mounts (a bind mount keeps `st_dev`, so the walk continues to the real mount, which is only more fsyncs). The 5.0 rules were not run. `RENAME_NOREPLACE` support on overlay and NFS is unprobed (the `rename(2)` man page lists ext4, btrfs, tmpfs, cifs, xfs and, from Linux 4.9, ext2 and others); where it is missing, creation refuses (L1). `flock` behaviour on NFS or overlay filesystems was not probed.
- Whether a platform relationship route passes `vault_writer` with a sink (`platform:api/jobs/route_dispatch_exec.py:116`, `:137`, `:154`), which B0 refuses, was not traced (platform handoff).
- The CLI was not grepped for a `CURRENT` or `managed_dataset/v1` consumer (5.4).
- Rule D's chain and anchor (5.0) are reasoned from `fileaccess/_output.py:45`, `:313-325` and `b6:fileaccess/_local.py:225-239`, and the platform shapes from `pv:api/jobs/v2_vault.py:113-275` and `b6:vault.py:134`, `:269-385`, `b6:fileaccess/_local.py:303-333` by `git show`; none was probed, and neither the V1-C nor the V1-2 failure was reproduced by running `_persist_job_vault` against B-6. Whether the platform worker has the Q4 requirement was not checked. The `same_rows` cost on a large vault (R4) was not measured.
- Slice sizes are estimates from r5 module sizes plus line counts of the modules each slice edits, not measured diffs.
- Platform and CLI consumers of output directory listings (R5) were not grepped beyond `v2_full_frame.py` and `v2_vault.py`.

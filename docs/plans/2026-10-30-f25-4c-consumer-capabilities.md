# F25 V1 scratch on A-4: one arena capability, permissions by default, report and sweep

Status: plan, revision 2 (handoff "F25 slice 4c plan revision: re-ground on A-4, drop the parallel capability tree"). Revision 1 (`affde556`) was the slice 4c amendment inside `docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md`, sections 2A and 3. The Dennis plan review under the V1 bar returned NO-GO: 2 V1 findings and 7 V2-LOG items. Revision 2 replaces the slice 4c amendment, slice 4b and slice 6 of that plan with the five slices in section 10. It cuts the plan to the handoff's smallest cut. No code is on this branch, and no build is authorized by this document.

Summary: F25 for V1 means three things. Engine scratch is created 0700 (directories) and 0600 (files the engine creates) under any umask, through one arena capability that is an A-4 `StageDir`. A job that runs out of scratch space stops with a value-free report and commits no output. A sweep removes abandoned arenas and leaves live ones alone. Kernel quotas and encryption are optional profiles, not prerequisites.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `development-loop.md`, `risk-and-exceptions.md`, `scope-discipline.md`, `reuse-first.md`, and the task rulebooks the README routing table selects for a security-relevant storage plan: `architecture.md`, `feature-dev.md`, `security.md`, `testing.md`, `verification.md`, `documentation.md`, `workspace-and-vcs.md`. **Not read during this revision:** `/home/cam/dev-rules/` does not exist in this container (`ls` fails), and neither does the defect catalog. The names come from the handoff and from revision 1's list. In their place this revision read decoy-platform `docs/guides/engineering-best-practices.md` (at decoy-platform `d8908f4`), cited by section below, the engine `CLAUDE.md`, `docs/security/file-access-policy.md` by reference from `fileaccess/__init__.py`, and the handoff's class list (1 to 10).

Risk: R2 (plan only). The change touches confidentiality of scratch files, cleanup of on-disk data, and the failure path of the out-of-core, subset, snapshot and spill routes. It is not R3: no step deletes data outside an arena the engine created and still owns, the sweep is a caller-invoked API that deletes only after the checks in section 4.4, and rollback is a revert on a pre-GA engine (`release.py`, best-practices 8.1).

Baselines. `origin/main` at `0173c054` (A-4 merged as `da1a522f`, #361). F25 implementation branch `origin/feat/f25-protected-scratch` at `4c901d38`, merge base with main `33ef629b`. Plan branch `origin/plan/f25-4c-consumer-capabilities` at `affde556`. Line anchors below are on `0173c054` unless marked `feat:` (the F25 branch).

## 0. What revision 2 changes, and the finding map

### 0.1 Merge of `origin/main` into this plan branch: blocked, reported

The handoff asked for `origin/main` to be merged into the plan branch first. The plan branch carries the F25 implementation commits (slices 0 to 4a). `git merge origin/main` on `affde556` stopped with 19 conflicting files. Seventeen are production files: `connectors/postgres_snapshot.py`, `data_discovery.py`, `execution/_chunked_output_sink.py`, `_isolated_run.py`, `_isolated_worker.py`, `_transactional_sink.py`, `native/_pool_quality.py`, `out_of_core/_batch_join.py`, `_relation.py`, `_runner.py`, `_scratch.py` (add/add), `_stage.py`, `_stream_driver.py`, `profile/_fixed_width_writer.py`, `quarantine.py`, `subset/_api.py`, `subset/_materialize.py`. The other two are `tests/sentry/test_module_size.py` and `tests/sentry/test_physical_seam_disconnection.py`. `git merge-tree --write-tree origin/main origin/feat/f25-protected-scratch` gives the same list.

Resolving those conflicts is production work, and V1 finding 1 is about how to resolve them. A plan-only task must not pick either side. The merge was aborted and nothing was committed from it. This revision is committed on the plan branch's existing history. Section 10 replaces "rebase the F25 branch" with "port onto a fresh branch from `origin/main`" (Q6).

### 0.2 Finding map

| Finding | Class | Where revision 1 had it | What revision 2 does |
|---|---|---|---|
| V1-1. 17 production conflicts with merged A-4. The plan builds a second capability system beside `file_access=`, `DirHandle`, `OutputDir`, `OutputTxn` and the DuckDB adapter. The "one commit ahead" claim is false, and the C00-C22 anchors are void. | 9 (refactor drift) and 4 (route parity: a builder keeps the F25 side and loses `require_ambient`) | 2A.2, 2A.5, umbrella plan line 19 | Section 3 re-censuses T01-T17 on `0173c054`. Section 4 makes the arena an A-4 `StageDir` issued from `fileaccess`. Publication rows T09, T10, T14, T15 and T16 already go through `OutputDir.begin` on main and need no F25 production change. Reuse table in section 7. Delete list in section 4.5. Port onto a fresh branch from main (S1). C00-C22 are dropped, not re-anchored. |
| V1-2. Activation makes fscrypt and ext4 project quotas mandatory, so a plain Docker/overlayfs host fails every spill job with `scratch_backend_unavailable`. | 1 (product function) and 4 (route parity) | umbrella 1.5 (line 142), slice 6, the 4b gate | The default profile needs only 0700 directories, 0600 engine-created files, and descriptor checks (owner, type, mode, no symlink). Quota and encryption become optional profiles (Q1, Q2). Named test `T-PLAIN` (section 5) runs out-of-core, subset, snapshot-shaped and forced-spill jobs on an ordinary container filesystem and requires success. `scratch_backend_unavailable` is removed from the default path. Nothing needs activating, so slice 6's activation flip is gone. |
| Owner Q3 answer: encryption is not needed for V1 | n/a | slice 4b | Slice 4b is cut to V2 (section 11, deferred features). |
| Smallest cut | 8 (six gate groups and 15 units for one threat class) | 2A.7, 2A.11, section 3 slice 4c | Dropped: 4b, 4c-pre, 4c-0a, 4c-0b, 4c-1 to 4c-8b, the C21 whole-repo taint sentry, `NativePathLease`, `ScratchDirectoryHandle`, `ScratchDirEntry`, the SQLite identity bracket, the `rmdir` link check, the round budget and the gate groups. Remaining: about four units, split into five slices by the size bound (section 10). |
| Tests the owner asked about (A02, A08, A07, A10) sat behind the cut in 4b, 5 and 6 | 8 | umbrella 1.9, slices 4b, 5 and 6 | A02 and A08 move to S4. A10 moves to S4. A07 moves to S5. Each has a named red-first form in section 5. |
| `ScratchManager.sweep` is decision-only | 2 (cleanup) | `feat:src/decoy_engine/scratch/_manager.py:163` | It stays decision-only until S5 lands with its tests. S1 to S4 add no deleting sweep. The `feat:` manager is not carried (section 4.5), so on the new base no sweep exists before S5. |
| 7 V2-LOG items | 1, 7, 8, 10 | various | Listed in section 11 with file and line. No work is planned for them. |

### 0.3 Found while writing revision 2

- The isolated payload holds source and target DSNs (credentials). It is written with the umask mode and then chmodded to 0600 (`execution/_isolated_run.py:429`, `:434`). When the caller supplies `staging_dir` (ambient policy only, `_isolated_run.py:215`, `:269`), the work root is the caller's directory with its existing mode (`:364-369`). For the window between write and chmod, a local non-admin user can open the payload. This is V1 rule 3 on main today. Class 7 at the check, class 1 for the exposed value. S3 fixes it (`T-PAYLOAD`).
- `out_of_core/_scratch.py:23-26` chmods a caller-named `temp_dir` to 0700. Changing the mode of a caller directory is a side effect on the caller's tree. It is ambient-policy only. S2a creates a fresh 0700 child inside the caller directory instead and leaves the caller directory's mode alone (Q8).
- `connectors/postgres_snapshot.py:217-218` creates the spill directory with `mkdir(parents=True)` (umask mode) and then chmods it. Parents created on the way keep the umask mode. Ambient policy only (`:447`). S3 creates the spill directory as an arena (0700 at creation).
- The discovery connection (`data_discovery.py:181-213`, `fileaccess/adapters/duckdb.py:44-61`) is in-memory with no `temp_directory`. Whether DuckDB spills such a session into a working-directory `.tmp`, and whether `enable_external_access = false` stops it, is **not verified**. S3 starts with probe `T-DISCOVERY-SPILL`. The fix is chosen from the probe result (section 4.3, T17).
- Slice 4a's gate record: no gate record for slice 4a was found under `docs/` on `feat/f25-protected-scratch` (`git grep -i "slice 4a"` over `docs/` finds no gate or review line). **Unverified** whether one exists elsewhere.

### 0.4 Stop-on-repeat report

No finding outside classes 1 to 10 was found. Two findings repeat earlier F25 review classes: 9/4 (a parallel system beside an existing one) and 8 (machinery out of proportion to the threat). Both are fixed at the class level. Class 9/4: section 4 has one capability family, A-4's, and section 7 lists what is reused instead of rebuilt. Class 8: section 1 states a ratchet and named limits, not completeness, and section 10 bounds every slice.

## 1. Guarantee (V1 rules 1 to 3 only: a ratchet plus named limits)

Each claim names the test that shows it and what that test does not show.

| # | Claim | V1 rule | Shown by | Not shown |
|---|---|---|---|---|
| G1 | On the producer rows of section 3 (T01-T08, T11-T13, T17), scratch directories the engine creates are mode 0700 at creation and owned by the engine UID. Files the engine's Python creates in them are 0600 at creation. This holds under umask 000, 002 and 022. | 3 | T-MODES (S2a, S2b, S3), T-ARENA-DESC (S1) | Files a native library creates inside the arena (DuckDB `COPY TO`, DuckDB spill) take the umask mode. The enclosing 0700 directory is what keeps a non-owner out (named limit L1). Producers added after the census are covered only through the ratchet (G5). |
| G2 | The arena opens on a host without project quotas or fscrypt (overlayfs, tmpfs, ext4 without `prjquota`). Out-of-core, subset, snapshot-shaped and forced-spill jobs succeed there with the same output as `0173c054`. | 1 | T-PLAIN (S1 for the arena, re-run in S2a, S2b, S3 and S5) | A host where the scratch root itself is unwritable still fails, as it does today. |
| G3 | When an out-of-core job exceeds its temp-disk budget, or a scratch write gets `ENOSPC` or `EDQUOT`, the job stops, the sink aborts, and no output is committed. The raised error carries a frozen `QuotaReport`. | 2, 1 | T-A02a, T-A02b (S4) | Native DuckDB I/O failures arrive without an errno. They fail the job, but their report says `dimension="unknown"` (L3). Without a kernel quota the byte bound is the existing point-in-time budget check (`out_of_core/_budget.py:525`), not a hard write-time cap (L2). |
| G4 | A process killed mid-spool leaves no committed output. A process killed at a publication step leaves either the old output or the complete new output. | 2 | T-A08 (S4), T-A10 (S4) | Stage directories a crash leaves in an output directory stay there for the operator. The sweep scans only the scratch root (L4). |
| G5 | Adding a new `tempfile.*` call, a new DuckDB `temp_directory`, or a new path-taking Arrow/Parquet writer outside `fileaccess/_scratch.py` and the adapters fails A-4's file-access boundary sentry until a reviewed registry entry is added. The registry count can only fall as producers migrate. | ratchet | A-4's `tests/sentry/test_file_access_boundary.py` with its count in `tests/sentry/file_access_registry.toml` (192 today), plus mutation M-RATCHET | A call the sentry's scanner does not model (dynamic dispatch, a C extension) is outside the ratchet (L5). |
| G6 | `str()` and `repr()` of `QuotaReport`, `CleanupReport`, `SweepReport` and the errors carrying them contain no path, table name, column name or cell value. | 3 | T-REPORT-TEXT (S1, S4) | Server-admin logs, tracebacks and `__cause__` chains are V2 under the V1 bar. |
| G7 | `sweep_scratch` never removes an arena whose owner still holds it (the owner process, or a child holding the inherited arena descriptor). It removes an arena only when the owner is gone, and it is safe to re-run after a crash mid-delete. | 2 | T-A07, T-A08-SWEEP (S5) | A same-UID process that deliberately takes and drops the lock is out of scope (L6). Cross-host shared filesystems are unsupported: `flock` is single-host (L7). |

Named limits (also in section 12): L1 native-created file modes; L2 no hard byte cap without the optional quota profile; L3 errno-less native I/O failures; L4 stale stages in output directories; L5 the sentry's scanner model; L6 same-UID interference; L7 single-host `flock`; L8 publication stages are output-policy files (A-4 `_output.py:222` creates file stages `0o666` minus umask, beside the final leaf). That is the destination's output mode, not scratch, and it is unchanged.

Out of scope for V1 (section 11): at-rest encryption, kernel project-quota enforcement, checkpoint and resume, same-UID race hardening, whole-repo completeness proofs.

## 2. Invariants

- I1. No producer in section 3 creates a scratch directory or a Python-written scratch file except through a `ScratchArena` (`StageDir` protocol, `fileaccess/_types.py:179`).
- I2. Internal producers take `arena: ScratchArena` as a required argument: no default, no `None`, no `getattr` fallback. Public entry points create the arena themselves. No unmanaged fallback route exists.
- I3. An arena root is created by the engine (`mkdtemp` under the platform temp root, or a fresh 0700 child of a caller directory under the ambient policy only), opened `O_DIRECTORY | O_NOFOLLOW`, and checked by `fstat`: directory, owner equals `geteuid()`, mode 0700. Then it is held by descriptor for its lifetime.
- I4. The arena holds `flock(LOCK_SH)` on its root descriptor from open to reclaim. An isolated child that writes into the parent's arena inherits that descriptor (`pass_fds`), so the lock outlives a parent crash while the child runs.
- I5. Reclaim is descriptor-relative (A-4 `_rmtree_fd`, `_output.py:80`). A failure is reported as `Disposition.RETAINED`, never as success.
- I6. Validation never mutates (best-practices 2.1): the descriptor checks raise and change nothing. Reports are frozen dataclasses (2.3).

## 3. Census on `0173c054` (replaces revision 1's C00-C22 and the 2A.2 anchors)

Grep run: `git grep -nE "mkdtemp|TemporaryDirectory|NamedTemporary|tempfile\.|\.begin\(|output_dir\(|connect_internal|duckdb\.connect|require_ambient|local_stage_path|temp_directory|spill_parent" origin/main -- src/decoy_engine`, followed by a per-file read of each hit. This is a reviewed census, not a proof of completeness. G5's ratchet covers what it misses.

| Row | Producer on main | Today | V1 change | Slice |
|---|---|---|---|---|
| T01 | OOC root, `out_of_core/_scratch.py:17-27`; subtrees `_runner.py:244`, `:268-270`; cleanup `:323-335` | `mkdtemp` (0700). Subtrees made by `Path.mkdir` take the umask mode. Caller `temp_dir` is chmodded. `shutil.rmtree` by path. | `scratch_root` returns a `ScratchArena`. Subtrees come from `arena.create_dir`. Cleanup is `arena.reclaim()`. A caller `temp_dir` gets a fresh child. | S2a |
| T02 | Staged keys, `_stage.py:66-70` | `mkdir(parents)` plus `pq.ParquetWriter(path)` (umask mode). Reads via `ScratchAccess` (`_scratch.py:30`). | `fileaccess.adapters.arrow.parquet_writer(arena_dir, component, schema)` (0600). Reads unchanged. | S2a |
| T03 | Relation files, `_relation.py:457`, `:466`, `:517` | DuckDB `COPY TO` a path, DuckDB temp dir | Directory from `arena.create_dir`. `COPY` targets stay native paths under the arena (L1). | S2a |
| T04 | Merged relation, `_multi_parent.py:135-161` | Same as T03 | Same as T03 | S2a |
| T05 | DuckDB connections, `_duckdb.py:42`, `:60`; `_batch_join.py:159`; `_join.py`; `_stream_join.py:530` | `temp_directory=str(path)` via `connect_internal` | `temp_directory` from `arena.native_dir(component)`. Only the DuckDB adapter receives it. | S2a (S2b for `_stream_join.py`) |
| T06 | IPC spools, `_payload_store.py:94-96`, `:146-150`, `:206`; `_stream_driver.py:257`, `:277`; `_stream_join.py:244` | `pa.ipc.new_stream(str(path))` (umask mode) | `adapters.arrow.ipc_new_stream(arena_dir, component, schema)` (0600). Reads through a new bounded `ipc_iter_stream`. | S2b |
| T07 | Sorter runs, `_external_sort.py:214`, `:381`, `:421`, `:511`; reorder `_stream_join.py:498` | `pa.OSFile(str(path), "wb")` (umask mode) | `adapters.arrow.ipc_new_file(...)` (0600). Readers open through the arena. | S2b |
| T08 | Quality pairs, `native/_pool_quality.py:245` | Parquet in a `connect_duckdb` 0700 dir | Arena-issued component and directory | S2b |
| T09 | Table sink stage, `_transactional_sink.py:200`, `:358` | A-4 `OutputDir.begin("dir", "exclusive", label="table_sink")` | None. A10 covers it (S4). | S4 (tests only) |
| T10 | Hold-back, `_chunked_output_sink.py:463-477` | A-4 `OutputDir.begin("dir", label="hold")` | None | S4 (tests only) |
| T11 | Isolated work root and payload, `_isolated_run.py:364-371`, `:422-434` | `mkdtemp`, or a caller `staging_dir` used as is. Payload written with the umask mode, then chmodded. | Work root is an arena, a fresh child under a caller `staging_dir`. Payload via `arena.create_file` (0600 at creation). The arena descriptor goes to the child through `pass_fds`. | S3 |
| T12 | Isolated staging output, `_isolated_commit.py:89`; worker `_isolated_worker.py` | Path under the work root | Staging directory from the work-root arena | S3 |
| T13 | Snapshot spill and stage, `postgres_snapshot.py:217-218`, `:455-459`, `:536-539` | Spill via `mkdir(parents)` then chmod. Stage via `mkdir(0o700)`. Ambient only (`:447`). | Spill directory is an arena under the caller's `temp_directory`. Stage unchanged (already 0700). | S3 |
| T14 | Subset, `subset/_materialize.py:51`, `:60` | A-4 `OutputDir.begin("dir", label="subset")` | None | S4 (tests only) |
| T15 | Quarantine, `quarantine.py:313`, `:336`, `:376` | A-4 `OutputDir.begin`, stage mode 0600 | None | S4 (tests only) |
| T16 | Fixed width, `profile/_fixed_width_writer.py:313-319` | A-4 `OutputDir.begin("file", "replace")` | None | S4 (tests only) |
| T17 | Discovery, `data_discovery.py:181-213`, `fileaccess/adapters/duckdb.py:44-61` | In-memory, no `temp_directory` | Decided by probe `T-DISCOVERY-SPILL` (section 4.3) | S3 |

Classified as not scratch, unchanged from revision 1 and re-checked on main: vault (`vault.py:341-348`, A-4 output), model pack (`profiling/model_pack/_packio.py:54-59`, A-4 output), cloud readers (network, no spool), and rotating logs (durable).

### 3.1 Enumerations

Anchors below are on main `0173c054`.

#### 3.1.1 Exit paths of an arena

Each gets a case in `T-ARENA-EXITS`, S1.

- Success then `reclaim()`.
- Exception inside the `with` block then `reclaim()` and re-raise with the primary error kept.
- `reclaim()` failure gives `CleanupReport(RETAINED)` without masking the primary error.
- Double `reclaim()` is a no-op.
- Garbage collection without `reclaim()` triggers a `weakref.finalize` reclaim (the A-4 precedent, `src/decoy_engine/fileaccess/_output.py:229`).
- Process kill leaves the arena with the lock released, for S5's sweep.
- Isolated child exit hands the work-root arena back to the parent's `reclaim()` (today the work root is created at `src/decoy_engine/execution/_isolated_run.py:364`).

#### 3.1.2 Routes

T-PLAIN and T-MODES cells. Each runs under the ambient policy and a strict `RootedFileAccess`. Routes that A-4 refuses under strict (`require_ambient`) assert the refusal, unchanged.

- OOC batch lane.
- OOC reorder lane.
- Native companion present and absent.
- Sink whole-table and streamed.
- Isolated subprocess.
- Isolated in-process fallback.
- Postgres snapshot (ambient only, `src/decoy_engine/connectors/postgres_snapshot.py:447`; the cell runs with a stub connection, see section 13).
- Discovery.
- Subset then mask.
- Quarantine (`src/decoy_engine/quarantine.py:313`).
- Fixed width.

#### 3.1.3 FK shapes

Through both OOC lanes. The existing parity suites (`tests/parity/test_out_of_core_fk_parity.py`, for example `tests/parity/test_out_of_core_fk_parity.py:448` and `tests/parity/test_out_of_core_fk_parity.py:763`) run unchanged before and after S2a and S2b. Class 5 is N/A because no row selection changes. The suites are the characterization (class 9).

- Single.
- Composite.
- Self-referencing.
- Multi-parent.
- Cross-batch.

#### 3.1.4 Fields of public result types touched

No path, name or identifier fields, so G6 holds by field set.

- New frozen `QuotaReport(route, dimension, effective_bytes_cap, observed_bytes, disposition)`.
- New frozen `CleanupReport(disposition, retained_bytes, retained_inodes)`.
- New frozen `SweepReport(scanned, reaped, retained_live, retained_unknown, failed)`.
- Existing error types gain one optional `report` attribute.
- `IsolatedRunResult` (`src/decoy_engine/execution/_isolated_common.py:321`), sink results and the public entry-point signatures are unchanged.

#### 3.1.5 Output surfaces

T-REPORT-TEXT seeds sentinels (a path, a table name, a cell value, a DSN) and scans `str` and `repr` of each report and carrying error.

- User-visible `Job.log` and API errors (via `str(exc)`).
- CLI stderr.
- Output files (unchanged).
- Scratch files (G1).
- Quarantine files (unchanged).

## 4. Design: the arena is an A-4 capability

### 4.1 Type and placement

New `src/decoy_engine/fileaccess/_scratch.py` (about 260 lines). Pattern: `cap-std` `Dir` and WASI preopens, as A-4 already cites (`_types.py:3-6`, `_output.py:3-8`), plus Python `tempfile.mkdtemp` for private creation. The docstring cites both.

```text
ScratchArena(StageDir)                # A-4 protocol, _types.py:179
  open(*, under: Path | None = None) -> ScratchArena    # under != None: require_ambient first
  create_file(component) -> int        # O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0o600, dir_fd
  open_file(component) -> int
  create_dir(component) -> ScratchArena (child, shares root lock and finished state)
  remove(component), rename(src, dst)  # one dir_fd-relative call each
  native_dir(component) -> str         # engine-created 0700 dir path for DuckDB temp_directory only
  local_stage_path() -> str            # raises, as A-4's _StageDir does
  reader() -> ScratchAccess            # existing read-only RootedFileAccess, _scratch.py:30
  reclaim() -> CleanupReport
```

Built over A-4 internals, not copied: `validate_component` (`_component.py`), `_call`, `_close`, `_rmtree_fd`, `_DIR_FLAGS` and `_FILE_CREATE` from `_output.py`. They are imported, and S1 moves them to a shared private helper only if the module-size sentry requires it. The A-4 writers in `fileaccess/adapters/arrow.py:72-126` take `OutputTxn` today and use only `create_file`, `open_file`, `display` and `strict`. S1 widens their annotation to `StageDir`, so the arena reuses them with no new writer code. `library_path`-style path release stays limited to `native_dir`, and A-4's boundary sentry rule (only adapter modules may call it) is extended to `native_dir`.

### 4.2 Descriptor checks (the whole fail-closed default)

At open: `fstat` on the root descriptor must show `S_ISDIR`, `st_uid == geteuid()` and `S_IMODE == 0o700`. Otherwise `ScratchPathUnsafe` is raised (value-free, `str` is the reason code only) and the directory is left untouched. At `create_dir` and `create_file`: `O_NOFOLLOW` plus `O_EXCL` or `mkdirat`, then `fstat` of the new descriptor for type, owner and mode. Nothing else is required: no project quota, no fscrypt, no qualification step. T-PLAIN is the regression test for that sentence.

### 4.3 Producers

T01-T08 and T11-T13 per section 3. T17: if `T-DISCOVERY-SPILL` shows DuckDB writes outside the arena for an in-memory untrusted session, `untrusted_session` sets `temp_directory` to `arena.native_dir("duckdb")` at connect, before `lock_configuration`. If it shows no spill is possible with external access off, T17 records that as a tested fact and makes no production change. The decision and the probe output go into the S3 evidence. Discovery keeps working in both cases (rule 1).

### 4.4 Sweep (S5)

`fileaccess.sweep_scratch(root: Path | None = None) -> SweepReport`. It is called by the CLI or worker, never on import and never in the background (Q7). For each entry of the scratch root, opened by descriptor:

1. Skip names without the engine prefix, entries that are not directories (`O_NOFOLLOW`), entries not owned by `geteuid()`, and entries without mode 0700. They are counted as `retained_unknown` and never deleted.
2. Try `flock(LOCK_EX | LOCK_NB)` on the entry's own descriptor. `EWOULDBLOCK` means a holder is alive: `retained_live`.
3. With the exclusive lock held, `_rmtree_fd` the contents, then `rmdir` the entry by name under the root descriptor. A failure leaves the entry for the next run and counts it as `failed`. A second sweep picks it up (idempotent).

Pattern: Linux `flock(2)` advisory locks on the open file description (held across `fork` and `exec` for inherited descriptors), as revision 1's 1.8 already cited for liveness. The etcd fencing and lease-generation machinery is not needed for an ephemeral-only V1 with no resume (Q3). The window between the lock and the `rmdir` of an emptied directory is the class 7 residual (L6). Its hardening is V2-LOG.

### 4.5 Not carried from `feat/f25-protected-scratch` (pre-GA hard delete, best-practices 8.1 and 8.2)

Not ported, because they duplicate A-4 or belong to the cut: `scratch/_capabilities.py` (`RegisteredFile`, `NativeDirectory`, `PublicationStage`, `unwrap_path`, `open_capability_fd`), `scratch/_seam.py` (`LocalArena`, `ensure_*`, `adopt_file`), the publication staging in `scratch/_arena.py`, `execution/_isolated_cap.py`, `tests/sentry/test_scratch_allocation_sentry.py` with `tests/sentry/scratch_producer_manifest.json` (A-4's registry is the ratchet), and `tests/scratch/test_fd_only_filesystem_sentry.py`. Deferred with their features (Q2, Q3): `scratch/_ledger.py`, `_authority.py`, `_manager.py`, `_policy.py`, `_provider.py`, `_local_provider.py`. Ported, with the cut fields removed: `scratch/_reports.py` becomes `fileaccess/_scratch_reports.py`, and `scratch/_errors.py` contributes `ScratchPathUnsafe` only. The F25 branch is not merged. It stays as reference until the owner retires it (Q6).

## 5. Acceptance tests, red first

Rule for each test: record it red before the change. For a new module, an `ImportError` is not a red. The red is the test run against the named neutered control, or against the main behavior at the fail-before location. A test that is green on main by design says so and proves it is not vacuous by its mutation. Baselines run from `uv.lock` in the same environment as the branch.

| Test (file::name) | Scenario and observable result | Fail-before location (red reason) | Mutation that must turn it red | Slice |
|---|---|---|---|---|
| `tests/fileaccess/test_scratch_arena.py::test_modes_under_umask[000,002,022]` (T-ARENA-DESC) | Create arena, nested dirs and files; each created dir is 0700, each created file 0600, owner is euid | Neutered control: `create_file` with mode `0o666` (A-4's `_output.py:222` default) shows 0644/0666 | `0o600` changed to `0o644` | S1 |
| `...::test_refuses_unsafe_root[symlink,wrong_mode,not_dir]` | Root replaced by symlink, made 0755, or made a file before open: `ScratchPathUnsafe`, nothing changed on disk | Control without the `fstat` checks opens it | Delete the mode comparison | S1 |
| `...::test_exit_paths` (T-ARENA-EXITS) | The section 3.1 exits; the primary error is kept; a failed reclaim reports `RETAINED` | Control whose reclaim swallows errors reports `REMOVED` | Return `REMOVED` on `OSError` | S1 |
| `tests/fileaccess/test_scratch_plain_host.py::test_jobs_succeed_without_quota_or_fscrypt` (T-PLAIN) | On the CI container filesystem (overlayfs or tmpfs, no `prjquota`, no fscrypt), run OOC both lanes with forced DuckDB spill, subset then mask, and the hold-back sink. All succeed and output bytes equal the `0173c054` run. | Revision 1's design (umbrella 1.5 line 142): neutered control where `ScratchArena.open` requires a project quota fails with `scratch_backend_unavailable` | Add a quota-qualification precondition to `open` | S1, re-run S2a, S2b, S3, S5 |
| `...::test_report_text_is_value_free` (T-REPORT-TEXT) | Sentinel path, table, value and DSN; `str` and `repr` of reports and carrying errors contain none | Control report with a `path` field leaks it | Add `detail=str(exc)` | S1, S4 |
| `tests/execution/out_of_core/test_scratch_modes.py::test_ooc_scratch_modes[batch,reorder]` (T-MODES, T01-T05) | Run OOC under umask 000 with a hook (`os.mkdir` and `os.open` wrappers, plus a post-run `fwalk` taken before reclaim) recording modes; each engine-created dir in the run is 0700, Python-created file 0600 | `_runner.py:244`, `:268-270` and `_stage.py:66` create subtrees `0777` under umask 000 on `0173c054` | Restore `Path.mkdir(parents=True)` in `_stage.py` | S2a |
| `...::test_caller_temp_dir_mode_untouched` | Caller `temp_dir` with mode 0755 keeps 0755, and the arena is a 0700 child | `_scratch.py:26` chmods it to 0700 on main | Restore the chmod | S2a |
| `...::test_spool_and_sorter_modes` (T-MODES, T06-T08) | Payload, raw-key, child-key spools, sorter runs and quality pairs are 0600 under umask 000; bounded RSS unchanged (existing bounded-memory tests) | `_payload_store.py:96`, `_external_sort.py:381` create umask-mode files on main | Restore `pa.OSFile(str(path), "wb")` | S2b |
| `tests/execution/test_isolated_payload_mode.py::test_payload_never_readable_by_others` (T-PAYLOAD) | Caller `staging_dir` 0755, umask 022: a wrapper on the payload write records the mode at first byte; it must be 0600, and the work root is a 0700 child | `_isolated_run.py:429` writes 0644 before `:434` chmods | Write then chmod again | S3 |
| `...::test_child_holds_arena_after_parent_death` | Parent killed while the child writes; the inherited arena descriptor keeps `LOCK_SH`; a `LOCK_EX|LOCK_NB` probe fails | Control without `pass_fds` for the arena descriptor: the probe succeeds | Drop the descriptor from `pass_fds` | S3 |
| `tests/connectors/test_snapshot_spill_mode.py::test_spill_dir_created_0700` | Spill helper under umask 022 creates 0700 at creation (stub connection, no Postgres) | `postgres_snapshot.py:217` creates 0755 then chmods | Restore `mkdir` then `chmod` | S3 |
| `tests/test_discovery_spill.py::test_discovery_spill_stays_in_arena` (T-DISCOVERY-SPILL) | Discovery query sized past `memory_limit`: no `.tmp` in the working directory; any spill lands under the arena | Outcome unknown before the probe (section 0.3). If green on main, recorded as a tested fact. | Remove `temp_directory` from the session config (only if the probe chose the arena fix) | S3 |
| `tests/execution/out_of_core/test_scratch_quota.py::test_budget_exceeded_commits_nothing` (T-A02a) | OOC with a sink and a tiny `temp_disk_budget_bytes`: raises `out_of_core_temp_disk_exceeded` with a `QuotaReport`; the destination has no final output | Green on main for "no output" by design (sink abort); red for `report` (attribute absent) | Move the sink commit before the budget check (output appears) | S4 |
| `...::test_enospc_commits_nothing[T02,T06,T07,T08,T11]` (T-A02b) | `ENOSPC` and `EDQUOT` injected at the arena's `os.write` boundary for each Python writer family: the job fails, nothing is committed, `QuotaReport(dimension="bytes")` | Main raises a bare `OSError` with no report; injected, not a real full disk (section 13) | Catch and continue the write | S4 |
| `tests/execution/test_crash_mid_spool.py::test_sigkill_mid_spool_commits_nothing` (T-A08) | Subprocess runs OOC with a sink; the test harness (not production code) pauses after the first spool batch; SIGKILL; the destination has no final output, and the arena remains with its lock released | Green on main by design (A-4 stage); mutation proves non-vacuity | Publish the stage before spooling ends | S4 |
| `tests/fileaccess/test_publication_crash_matrix.py::test_crash_points[T09,T15,T16]` (T-A10) | SIGKILL before stage fsync, after the publish rename, after the parent fsync; the final name holds the old output or the complete new output, never a partial | Green on main by design | Write directly to the final leaf | S4 |
| `tests/fileaccess/test_scratch_sweep.py::test_live_arena_survives` (T-A07) | Holder process paused mid-spool; sweep from a second process gives `retained_live`, bytes unchanged; two concurrent sweepers after the holder dies: one reaps, the other reports, no error; a non-engine entry and a 0755 entry are never touched | Neutered control: prefix-only `rmtree` sweep deletes the live arena | Drop the `flock` probe | S5 |
| `...::test_reap_after_kill_and_resume_after_sweeper_crash` (T-A08-SWEEP) | Kill the owner, sweep reaps and reports; kill the sweeper mid-delete, the second sweep finishes | Control without retry semantics leaves a partial | Count a failed delete as reaped | S5 |
| Ratchet M-RATCHET | Add a `tempfile.mkdtemp` in `_relation.py`: A-4's boundary sentry fails | Existing mechanism; the registry count must drop with each migrated site | Raise the registry count | S2a, S2b, S3 |

## 6. Class table (classes 1 to 10)

| # | Class | Applies | Closing structure | Test | Mutation |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: reports and errors reach `Job.log` and the API | Frozen reports with no path or name fields; errors raised `from None` with code-only text | T-REPORT-TEXT, T-PAYLOAD | add a `detail` field |
| 2 | Cleanup on an exit path | Yes | Arena `reclaim` on each enumerated exit (3.1), finalizer, `RETAINED` on failure; sweep for kills | T-ARENA-EXITS, T-A08, T-A08-SWEEP | swallow a reclaim error |
| 3 | State and concurrency | Yes: sweep against owner, two sweepers | `flock` on the open file description, inherited by the child | T-A07, child-holds test | drop `LOCK_SH` or `pass_fds` |
| 4 | Route parity and silent fallback | Yes | One required arena (I2), no unmanaged fallback, A-4 `require_ambient` kept on the caller-path routes; no profile gate (G2) | T-PLAIN, T-MODES both lanes, strict-policy cells | add a quota precondition; restore a `None` default |
| 5 | Referential integrity | N/A: no row selection changes | Existing parity suites as characterization | FK parity suites | n/a |
| 6 | Untrusted producer | Partly: DuckDB native files, discovery SQL | Native files confined to a 0700 arena dir; untrusted session config locked after `temp_directory` | T-DISCOVERY-SPILL | unlock or remove `temp_directory` |
| 7 | Check then use on a path | Yes, bounded | Descriptor checks at open; `dir_fd`-relative ops; native paths only via `native_dir`; residuals L1, L6 named | T-ARENA-DESC | delete a `fstat` comparison |
| 8 | Weak tests and overclaimed evidence | Yes | Section 1 table with "not shown" column; red forms named per test; injected failures labelled as injected | all | n/a |
| 9 | Refactor drift | Yes: producer migration | Byte-identical output parity before and after each slice; no behavior change beyond modes and the report | T-PLAIN, parity suites | reorder a write |
| 10 | Frozen contract drift | Yes | Public entry-point signatures unchanged; reason registry unchanged (Q4); A-4 `CONTRACT_VERSION` unchanged (additive export) | signature snapshot, `test_file_access_entry_points.py` | rename an export |

## 7. Reuse inventory

- A-4 `StageDir` and `OutputTxn` protocols, `_types.py:179-229`; `RootedOutputDir.begin` for T09, T10, T14, T15 and T16 (unchanged).
- A-4 `_output.py` helpers: `_call` (errno-only errors), `_close`, `_rmtree_fd`, `_DIR_FLAGS`, `_FILE_CREATE`; `_component.validate_component`.
- A-4 adapters: `arrow.parquet_writer`, `ipc_new_file`, `ipc_new_stream`, `write_table` (`adapters/arrow.py:72-126`), with the annotation widened; `duckdb.connect_internal` and `untrusted_session` (`adapters/duckdb.py:44-68`).
- `ScratchAccess` and `scratch_access_for` (`out_of_core/_scratch.py:30-61`) for reads.
- `fileaccess.require_ambient` (`_entry.py:115`) for the caller-named roots.
- A-4 boundary sentry and registry (`tests/sentry/test_file_access_boundary.py`, `file_access_registry.toml`) as the G5 ratchet; module-size sentry `tests/sentry/test_module_size.py`.
- From `feat/f25-protected-scratch`: `scratch/_reports.py` types, trimmed; the `tests/scratch/_fakes.py` patterns where they still fit.
- Existing reason `out_of_core_temp_disk_exceeded` (`reasons/runtime/_part2.py:150`) and `check_temp_disk_budget` (`out_of_core/_budget.py:525`).

## 8. Frozen contracts touched

- `fileaccess` public surface: additive exports `ScratchArena`, `sweep_scratch`, `QuotaReport`, `CleanupReport`, `SweepReport`, `ScratchPathUnsafe`. `CONTRACT_VERSION` stays 1 (additive). `ENTRY_POINTS` unchanged.
- Public entry-point signatures: unchanged. `run_fk_out_of_core(temp_dir=...)`, `run_pipeline_isolated(staging_dir=...)` and the snapshot `temp_directory` keep their meaning, except that the engine now creates a private child inside a caller directory instead of using or chmodding the directory itself. CHANGELOG entry in S2a and S3.
- Reason registry: unchanged (Q4).
- Output bytes: unchanged (T-PLAIN parity).

## 9. Owner questions (recommended defaults first)

- Q1. At-rest encryption: **not in V1 (owner's answer, given as Q3 in the earlier review: encryption (fscrypt) is cut to V2).** 0700 and 0600 stop non-admin local users. fscrypt protects against offline disk access and root, and root is the admin. Cut to V2.
- Q2. Kernel project quotas: **an optional profile deferred to V2 (recommended).** The V1 byte bound is the existing OOC temp-disk budget, plus `ENOSPC`/`EDQUOT` from whatever filesystem the host has, both surfaced as a `QuotaReport`. Alternative: port `feat:scratch/_local_provider.py` as an opt-in S6 if the platform's managed volume needs a hard cap at launch.
- Q3. SQLite `ResourceLedger` and `OwnershipAuthority`: **not carried (recommended).** For ephemeral-only V1, the sweep's authority is the `flock` liveness lock plus owner, mode and prefix checks. Revisit with checkpoint and resume (V2).
- Q4. Reason codes: **no new code (recommended).** OOC keeps `out_of_core_temp_disk_exceeded`, other routes keep their current errors, and each gains an optional `report`. This avoids a registry bump that collides with A-4's 1.2.0 and the B-6 sequence. Alternative: add `scratch_quota_exceeded` in its own slice with the registry partners.
- Q5. S2b throughput budget for fd-backed IPC writers (`os.fdopen` file objects instead of `pa.OSFile` paths): **median regression of at most 5% on the sorter-merge and payload-store benchmarks, same host, five repetitions, fixed before the change (recommended).** If it is missed, keep path writers for T06 and T07 and rely on the 0700 directory, recorded as limit L1 widened to those rows.
- Q6. Build base: **a fresh `feat/f25-v1-scratch` from `origin/main` (recommended).** `feat/f25-protected-scratch` is frozen as reference, not merged and not rebased. Its retirement is the owner's call after S5.
- Q7. Sweep invocation: **an exported `sweep_scratch` that the CLI or worker calls at startup or on a schedule; the engine never sweeps on import or in the background (recommended).**
- Q8. Caller-named roots (`temp_dir`, `staging_dir`, snapshot `temp_directory`): **stay ambient-only as on main; the engine creates a fresh 0700 child inside and never changes the caller directory's mode (recommended).**

## 10. Slices

Bound: each slice changes at most 10 production files and at most 800 lines, counting production, test and doc lines. Figures are estimates from the `feat:` diffstat of the same rows (`git diff --stat 33ef629b 4c901d38`) scaled to the smaller design. Each slice's evidence records the actual diffstat. If a slice exceeds the bound, it is split before review, not waived.

| Slice | Unit | Production files | Est. changed lines (prod + tests + docs) | Content | Gate |
|---|---|---|---|---|---|
| S1 | 1. Port onto A-4 | 4: new `fileaccess/_scratch.py`, new `fileaccess/_scratch_reports.py`, `fileaccess/__init__.py`, `fileaccess/adapters/arrow.py` (annotation and `ipc_iter_stream`) | about 780 (about 400 + 340 + 40 for the sentry rule and docs) | Arena, descriptor checks, exits, reports; T-ARENA-DESC, T-ARENA-EXITS, T-PLAIN (arena level), T-REPORT-TEXT, recorded red against neutered controls; boundary-sentry rule for `native_dir` | Dennis, then Codex |
| S2a | 2. Thread the arena (OOC part 1, T01-T05) | 8: `out_of_core/_scratch.py`, `_runner.py`, `_stage.py`, `_relation.py`, `_multi_parent.py`, `_duckdb.py`, `_batch_join.py`, `_join.py` | about 700 (about 230 + 470) | Required arena through the runner; caller `temp_dir` child; T-MODES (T01-T05), caller-mode test, T-PLAIN re-run, M-RATCHET; `_runner.py` is at 644 with a census entry (`test_module_size.py:120`), so net growth there is zero or the per-table loop moves out first in this slice | Codex |
| S2b | 2. Thread the arena (OOC part 2, T06-T08) | 6: `_payload_store.py`, `_external_sort.py`, `_stream_driver.py`, `_stream_join.py`, `_emit.py`, `native/_pool_quality.py` | about 650 (about 200 + 450 including the Q5 benchmark) | fd-backed spools and runs; bounded-RSS suites; Q5 budget recorded before the change | Codex |
| S3 | 2. Thread the arena (non-OOC, T11-T13, T17) | 6: `_isolated_run.py`, `_isolated_worker.py`, `_isolated_commit.py`, `connectors/postgres_snapshot.py`, `data_discovery.py`, `fileaccess/adapters/duckdb.py` | about 690 (about 230 + 460) | T-PAYLOAD, child-holds, snapshot spill mode, T-DISCOVERY-SPILL probe first; `_isolated_run.py` is at 651 with a census entry (`test_module_size.py:71`), so the payload writer moves to `_isolated_commit.py` and the net change is at most zero | Dennis (rule 3 fix), then Codex |
| S4 | 3. Quota report and crash behavior | 4: `out_of_core/_runner.py`, `out_of_core/_budget.py`, `fileaccess/_scratch.py`, `execution/_isolated_run.py` | about 720 (about 140 + 580) | T-A02a, T-A02b, T-A08, T-A10; report attached to existing errors; `from None` text | Codex |
| S5 | 4. Live-arena sweep | 3: new `fileaccess/_scratch_sweep.py`, `fileaccess/_scratch.py` (lock helper), `fileaccess/__init__.py` | about 700 (about 250 + 450) | T-A07, T-A08-SWEEP with real subprocesses; T-PLAIN re-run; docs: `docs/security/file-access-policy.md` scratch section, CHANGELOG | Dennis, then Codex |

Order: S1, S2a, S2b, S3, S4, S5. S3 depends only on S1 and may run beside S2a and S2b on separate worktrees, one writer each. S4 needs S2a, S2b and S3, because T-A02b runs the T02-T11 writers. S5 needs S1 and S3 (the inherited lock).

Units, as in the handoff: rebase onto A-4 (S1), one non-optional arena through the internal producers (S2a, S2b, S3: one unit split three ways by the size bound), the 0700/0600 default plus the quota report (S1 and S4), and the live-arena sweep (S5). That is four units in six slices, about 4,240 changed lines in all. Revision 1 planned 15 units for 4c alone, plus 4b and 6.

Each slice follows this loop: tests first and recorded red (or green-by-design with its mutation), then the change; baselines from `uv.lock` in the branch environment; each new mechanism mutated once with its test shown red, never committed while a mutation is applied; then lint, format, mypy, the touched suites, `tests/sentry`, and `tests/fileaccess`.

## 11. V2-LOG (listed, not planned) and deferred features

V2-LOG, with defect class:

- [7] SQLite `/proc` identity bracket and the detect-only residual inside `sqlite3_open`: umbrella plan 2A.3 and 4c-6a/4c-6b (`docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md:410`, `:685-686`; `feat:src/decoy_engine/scratch/_ledger.py:12-14`).
- [7] `NativePathLease` with `/proc/self/fd` anchoring and a per-DuckDB-version probe: umbrella unit 4c-4b (`docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md:683`).
- [7] `rmdir` `st_nlink == 0` post-check and bounded `ENOTEMPTY` rescans: umbrella unit 4c-7 (`:688`).
- [8] Whole-repo taint sentry C21: umbrella unit 4c-0b (`:678`); `feat:tests/scratch/test_fd_only_filesystem_sentry.py:26`.
- [1] Numeric `QuotaReport.errno`: `feat:src/decoy_engine/scratch/_reports.py:83`. The quarantine exception class name in output: file and line **not located** in this revision; the reviewer's anchor is needed.
- [8] Six gate groups and 15 units for one threat class: umbrella 2A.7 and 2A.11 (`:507`, `:591`).
- [10] Stale baseline and line anchors: umbrella header line 7 (`4c901d38`) and 2A.2 (`:378`; C00-C22 anchors void on `0173c054`).
- [7] Same-UID interference in the sweep window between `flock` and `rmdir` (L6): `fileaccess/_scratch_sweep.py` (new, S5).
- [7] Native-created file modes inside the arena (L1): `out_of_core/_relation.py:517`, `_multi_parent.py:147`.

Deferred features (not defects): fscrypt at-rest encryption and key envelopes (Q1; umbrella slice 4b, 1.6, 1.7), the kernel project-quota provider (Q2; `feat:scratch/_local_provider.py`), the ledger and ownership authority (Q3), checkpoint and resume (umbrella slice 6), and the platform lock-authority integration (umbrella section 4).

## 12. Pre-mortem: likely gate findings and the red-first test for each

1. "The arena duplicates `OutputTxn`." The arena implements A-4's `StageDir` protocol and reuses its helpers and adapters. Publication still uses `OutputTxn`. Test: the S1 type check that `isinstance(arena, StageDir)` holds and that `adapters.arrow.parquet_writer(arena, ...)` works unmodified.
2. "A strict-policy job now follows a caller path." The caller-path routes call `require_ambient` before creating the child arena. Test: the strict-policy cells in T-MODES assert `FileAccessUnavailable` before any I/O (strace helper `tests/fileaccess/strace_support.py`).
3. "A plain host breaks." T-PLAIN runs in each slice from S1 on.
4. "The sweep deletes a live child's files." The child-holds test (S3) plus T-A07 (S5).
5. "Throughput regresses from Python file objects." The Q5 budget is fixed before S2b, and its fallback is named.

## 13. What this revision did not run or verify

- `plan-lint plan <file>` was **not run**. The owner's linter is not installed in this container. The PyPI package of the same name (`plan-lint` 0.0.3) is an unrelated JSON agent-plan linter and was not used.
- `/home/cam/dev-rules/*` and `DEFECT-CATALOG.md` are absent here and were not read.
- The merge of `origin/main` was attempted and aborted (section 0.1). Nothing was rebased.
- No test was written or run, so no red or green is claimed. Line estimates are estimates.
- `T-DISCOVERY-SPILL`'s premise (whether a locked in-memory DuckDB session spills to `.tmp`) is unverified.
- The snapshot cell runs against a stub connection. No live Postgres run is planned in V1.
- `ENOSPC`/`EDQUOT` in T-A02b are injected at the write boundary, not produced by a real full filesystem. A real-quota run needs the optional profile (Q2) and a quota host.
- Whether slice 4a has a gate record is unverified (section 0.3).
- `flock` semantics on the deployment filesystems (overlayfs upper directory, NFS) are not verified. NFS is unsupported (L7).

## 14. Provenance

- Revision 1: `affde556` (Opus hostile-review remediation), on argon round 1 and the Opus review `review/f25-4c-opus`.
- Dennis plan review of revision 1 under the V1 bar: NO-GO, 2 V1, 7 V2-LOG (handoff text). Revision 2 answers it in section 0.2.
- Next gates: Dennis on this exact document, then Codex. No build before owner approval.

# F25 V1 scratch on A-4: one arena capability, permissions by default, report and sweep

Status: plan, revision 3 (handoff "F25 4c plan revision 3: three V1 fixes"), a targeted amendment answering the Dennis V1-bar review of `937e88fe` (GO-WITH-FIXES: 3 V1, 8 V2-LOG; revision 2's two V1s verified closed). Section 0 maps each finding to its change. No code is on this branch, and no build is authorized by this document.

Summary: F25 for V1 means three things. Engine scratch is created 0700 (directories) and 0600 (files the engine creates) under any umask, through one arena capability that is an A-4 `StageDir`. A job that runs out of scratch space stops with a value-free report and commits no output. A sweep removes abandoned arenas and leaves live ones alone. Kernel quotas and encryption are optional profiles, not prerequisites.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `development-loop.md`, `risk-and-exceptions.md`, `scope-discipline.md`, `reuse-first.md`, and the rulebooks the README routing table selects for a security-relevant storage plan: `architecture.md`, `feature-dev.md`, `security.md`, `testing.md`, `verification.md`, `documentation.md`, `workspace-and-vcs.md`. **Not read:** `/home/cam/dev-rules/` does not exist in this container; the names come from the handoff. Read in their place: decoy-platform `handoffs/queue` (`ac3a53f`) `docs/handoffs/V1-BAR.md` and `DEFECT-CATALOG.md`, the engine `CLAUDE.md`, and the code at each anchor below.

Risk: R2 (plan only). It touches scratch confidentiality, cleanup of on-disk data, and the failure path of the OOC, subset, snapshot and spill routes. It is not R3: the sweep deletes only entries whose name only `ScratchArena.open` produces (4.4), it is caller-invoked, and rollback is a revert on a pre-GA engine (`release.py`).

Baselines. `origin/main` at `0173c054` (A-4 merged as `da1a522f`, #361), carried on this branch by merge `3246830`. Plan branch at `937e88fe` before this revision. Platform anchors are on decoy-platform `ac3a53f`. Engine line anchors are on `0173c054`; `feat:` marks the frozen `feat/f25-protected-scratch` (`4c901d38`).

## 0. Revision 3: finding map

| Finding | Class | Change | Test (new or changed) |
|---|---|---|---|
| V1-A. The sweep's unnamed prefix plus euid, 0700 and no lock matches live platform and older-engine directories in the same temp root. | 2, 1 | 4.4 step 1 matches only `^decoy-arena-v1-[0-9a-f]{16}$`, issued only by `ScratchArena.open`, which creates under a pending name, locks, then renames (closes review V2 item 2). G7, I3, I4, L4. | `test_arena_prefix_used_only_by_open` (S1); T-A07 extended with look-alikes; `test_sweep_in_open_window` (S5) |
| V1-B. T12 moved `output_dir` staging into the arena: `os.replace` gets `EXDEV` across devices. | 1, 4 | T12 only for `output_dir is None`; otherwise `sibling_staging_dir` (`_isolated_commit.py:56-62`, `:89-100`) stays, under L8. | `test_output_dir_commit_keeps_sibling_staging[other_dir,other_device]` (S3) |
| V1-C. The only path release (`native_dir`, adapters only) cannot serve the isolated child or the OOC `COPY`/`read_parquet` modules. | 1, 7 | One release, `local_path`, with a policy rule and a caller allowlist in the boundary sentry; released paths are L1. One deviation, below. | `test_local_path_policy`, `test_local_path_callers_are_allowlisted` (S1) |
| Eight V2-LOG plan edits | 8, 1, 2 | T-A02b seam (5); `display`/`strict` (4.1); slice transition (10); S1/S4 size notes (10); `sources/` in T11 and T-PAYLOAD; caller-directory arenas unswept (L4); missing caller `temp_dir` (T01); stale anchor. | in place |

Deviation (for Dennis). The review asks for `local_path` refused under any strict policy. On main the OOC route runs under a strict policy with engine-made scratch and must match its golden (`tests/fileaccess/test_policy_routes.py:69`, job `out_of_core_fk`), and its `COPY TO` and `read_parquet` need paths, so that would fail the route (rule 1). The rule is by where the arena lives: an `under=` arena (a caller directory) releases only under the ambient policy, mirroring `_local.py:174` against `_output.py:185`; an engine-rooted arena, whose path names no caller location, releases under any policy, as main's `decoy-ooc-` root does. The isolated subprocess route is ambient-only already (`_isolated_run.py:215`, `test_policy_routes.py:122`).

Cut so the plan does not grow: revision 2's section 0 (merge report, finding map, found-while-writing, stop-on-repeat; it stays in `937e88fe`, and its live items are rows T01, T11, T13 and T17), `native_dir`, and pre-mortem item 5. `ipc_iter_stream` moves from S1 to S2b, where its reader is.

Stop-on-repeat: no finding outside classes 1 to 10. V1-A repeats class 2 (F25 scratch) and V1-C repeats class 7 (F25 consumers using bare paths). Both are fixed at the class: a name only one constructor issues, with a sentry; one path release with a policy rule and a caller-allowlist sentry.

## 1. Guarantee (V1 rules 1 to 3 only: a ratchet plus named limits)

Each claim names the test that shows it and what that test does not show.

| # | Claim | V1 rule | Shown by | Not shown |
|---|---|---|---|---|
| G1 | On the producer rows of section 3 (T01-T08, T11-T13, T17), scratch directories the engine creates are mode 0700 at creation and owned by the engine UID. Files the engine's Python creates in them are 0600 at creation. This holds under umask 000, 002 and 022. | 3 | T-MODES (S2a, S2b, S3, S4), T-ARENA-DESC (S1) | Files DuckDB creates inside the arena (`COPY TO`, spill) and files the isolated child writes by path take the umask mode; the enclosing 0700 arena keeps a non-owner out (L1). Producers added after the census are covered only through the ratchet (G5). |
| G2 | The arena opens on a host without project quotas or fscrypt (overlayfs, tmpfs, ext4 without `prjquota`). Out-of-core, subset, snapshot-shaped and forced-spill jobs succeed there with the same output as `0173c054`. | 1 | T-PLAIN (S1 for the arena, re-run in S2a, S2b, S3 and S5) | A host where the scratch root itself is unwritable still fails, as it does today. |
| G3 | When an out-of-core job exceeds its temp-disk budget, or a scratch write gets `ENOSPC` or `EDQUOT`, the job stops, the sink aborts, and no output is committed. The raised error carries a frozen `QuotaReport`. | 2, 1 | T-A02a, T-A02b (S4) | Errno-less DuckDB I/O failures fail the job with `dimension="unknown"` (L3). Without a kernel quota the bound is the point-in-time budget check (`out_of_core/_budget.py:525`), not a write-time cap (L2). |
| G4 | A process killed mid-spool leaves no committed output. A process killed at a publication step leaves either the old output or the complete new output. | 2 | T-A08 (S4), T-A10 (S4) | Stage directories a crash leaves in an output directory stay there for the operator. The sweep scans only the scratch root (L4). |
| G5 | A new `tempfile.*` call, DuckDB `temp_directory`, or path-taking Arrow/Parquet writer outside `fileaccess/_scratch.py` and the adapters fails A-4's boundary sentry until a reviewed registry entry is added; the count only falls as producers migrate. | ratchet | `tests/sentry/test_file_access_boundary.py`, count in `file_access_registry.toml` (192 today), M-RATCHET | Calls the scanner does not model (dynamic dispatch, C extensions) (L5). |
| G6 | `str()` and `repr()` of `QuotaReport`, `CleanupReport`, `SweepReport` and the errors carrying them contain no path, table name, column name or cell value. | 3 | T-REPORT-TEXT (S1, S4, S5) | Server-admin logs, tracebacks and `__cause__` chains are V2 under the V1 bar. |
| G7 | `sweep_scratch` acts only on names `^decoy-arena-v1-[0-9a-f]{16}$`, which only `ScratchArena.open` issues; platform, older-engine and pending directories are never removed. It never removes an arena still held (owner, child with the inherited descriptor, an arena inside `open`), and is safe to re-run after a crash mid-delete. | 2, 1 | `test_arena_prefix_used_only_by_open` (S1); T-A07, `test_sweep_in_open_window`, T-A08-SWEEP (S5) | Deliberate same-UID lock games (L6); cross-host filesystems (L7). |

Named limits: L1 native and released-path files: DuckDB `COPY TO`, spill and `read_parquet` paths inside the arena, and the isolated child's path writes (result envelope, staging when `output_dir` is None), take the umask mode; L2 no hard byte cap without the optional quota profile; L3 errno-less native I/O failures; L4 never swept: stale stages in output directories, arenas `under=` a caller directory, and an empty pending directory left by a crash inside `open` (the sweep scans only the platform temp root); L5 the sentry's scanner model; L6 same-UID interference; L7 single-host `flock`; L8 publication stages are output-policy files (A-4 `_output.py:222`, `0o666` minus umask, and isolated `sibling_staging_dir`): the destination's output mode, unchanged.

Out of scope for V1 (section 11): at-rest encryption, kernel project-quota enforcement, checkpoint and resume, same-UID race hardening, whole-repo completeness proofs.

## 2. Invariants

- I1. No producer in section 3 creates a scratch directory or a Python-written scratch file except through a `ScratchArena` (`StageDir` protocol, `fileaccess/_types.py:179`).
- I2. Internal producers take `arena: ScratchArena` as a required argument: no default, no `None`, no `getattr` fallback. Public entry points create the arena themselves. No unmanaged fallback route exists.
- I3. An arena root is created by the engine (`mkdtemp` under the platform temp root, or a fresh 0700 child of a caller directory under the ambient policy only) under a pending name, opened `O_DIRECTORY | O_NOFOLLOW`, checked by `fstat` (directory, owner `geteuid()`, mode 0700), locked, and only then renamed to its `decoy-arena-v1-` name (4.1). It is held by descriptor for its lifetime.
- I4. The arena holds `flock(LOCK_SH)` on its root descriptor from before its rename to reclaim (the lock is on the open file description, so it survives the rename). An isolated child that writes into the parent's arena inherits that descriptor (`pass_fds`), so the lock outlives a parent crash while the child runs.
- I5. Reclaim is descriptor-relative (A-4 `_rmtree_fd`, `_output.py:80`). A failure is reported as `Disposition.RETAINED`, never as success.
- I6. Validation never mutates (best-practices 2.1): the descriptor checks raise and change nothing. Reports are frozen dataclasses (2.3).

## 3. Census on `0173c054` (replaces revision 1's C00-C22 and the 2A.2 anchors)

Grep run: `git grep -nE "mkdtemp|TemporaryDirectory|NamedTemporary|tempfile\.|\.begin\(|output_dir\(|connect_internal|duckdb\.connect|require_ambient|local_stage_path|temp_directory|spill_parent" origin/main -- src/decoy_engine`, then a read of each hit; revision 3 re-read the path consumers (`read_parquet`, `COPY`, `OSFile`, `prefix=`). A reviewed census, not a completeness proof; G5's ratchet covers misses.

| Row | Producer on main | Today | V1 change | Slice |
|---|---|---|---|---|
| T01 | OOC root, `out_of_core/_scratch.py:17-27`; subtrees `_runner.py:244`, `:268-270`; cleanup `:323-335` | `mkdtemp` (0700). Subtrees made by `Path.mkdir` take the umask mode. Caller `temp_dir` is chmodded. `shutil.rmtree` by path. | `scratch_root` returns a `ScratchArena`. Subtrees come from `arena.create_dir`. Cleanup is `arena.reclaim()`. A caller `temp_dir` gets a fresh child; a missing one is created with `mkdir(parents=True)` first, as main does (`:25`, ambient only). | S2a |
| T02 | Staged keys, `_stage.py:66-70` | `mkdir(parents)` plus `pq.ParquetWriter(path)` (umask mode). Reads via `ScratchAccess` (`_scratch.py:30`). | `fileaccess.adapters.arrow.parquet_writer(arena_dir, component, schema)` (0600). Reads unchanged. | S2a |
| T03 | Relation files, `_relation.py:457`, `:466`, `:517-529`; stream-lane builds `_emit.py:137`, `:150` | DuckDB `COPY TO` and `read_parquet` by path | Directory from `arena.create_dir`. `COPY` and `read_parquet` paths come from `local_path` (4.1, L1). `ParentKeyRelation.path` (`_relation.py:53`) carries one to `_join.py:120`, `_batch_join.py:162`, `_stream_join.py:553`. | S2a (`_emit.py` S2b) |
| T04 | Merged relation, `_multi_parent.py:135-164` | Same as T03 | Same as T03 | S2a |
| T05 | DuckDB connections, `_duckdb.py:42`, `:60`; `_batch_join.py:159`; `_join.py`; `_stream_join.py:530` | `temp_directory=str(path)` via `connect_internal` | `connect_duckdb` takes an arena and passes `local_path()` of a child as `temp_directory` (transition: section 10) | S2a (S2b for `_stream_join.py`) |
| T06 | IPC spools, `_payload_store.py:94-96`, `:146-150`, `:206`; `_stream_driver.py:257`, `:277`; `_stream_join.py:244` | `pa.ipc.new_stream(str(path))` (umask mode) | `adapters.arrow.ipc_new_stream(arena_dir, component, schema)` (0600). Reads through a new bounded `ipc_iter_stream`. | S2b |
| T07 | Sorter runs, `_external_sort.py:214`, `:381`, `:421`, `:511`; reader `_external_sort_run.py:37`; reorder `_stream_join.py:498` | `pa.OSFile(str(path), "wb")` (umask mode) | `adapters.arrow.ipc_new_file(...)` (0600). Readers open through the arena. | S2b |
| T08 | Quality pairs, `execution/native/_pool_quality.py:245-246` (no production caller of `measure_pool_quality`; tests only) | Parquet in a `connect_duckdb` 0700 dir | Arena-issued component and directory | S4 |
| T09, T10, T14, T15, T16 | Publication: table sink `_transactional_sink.py:200`, `:358`; hold-back `_chunked_output_sink.py:463-477`; subset `subset/_materialize.py:51`, `:60`; quarantine `quarantine.py:313`, `:336`, `:376`; fixed width `profile/_fixed_width_writer.py:313-319` | A-4 `OutputDir.begin` (dir or file stages) | None; T-A10 covers them | S4 (tests only) |
| T11 | Isolated work root, raw sources and payload, `_isolated_run.py:364-371`, `:406-411`, `:422-434` | `mkdtemp`, or a caller `staging_dir` used as is. `sources/` made by umask `mkdir`, parquet by path. Payload written with the umask mode, then chmodded. | Work root is an arena, a fresh child under a caller `staging_dir`. `sources/` via `arena.create_dir`, files via `adapters.arrow.write_table` (0600). Payload via `adapters.serial.write_text` over `arena.create_file` (0600 at creation). The child gets `local_path` strings (`:446`) and the arena descriptor through `pass_fds`. | S3 |
| T12 | Isolated staging output, `_isolated_commit.py:89-100`; worker `_isolated_worker.py:129-133` | `output_dir is None`: `work_root / "staging_output"`. With `output_dir`: `sibling_staging_dir` beside it, so the commit is one same-filesystem rename. | `output_dir is None` only: `arena.create_dir("staging_output")`. With `output_dir`: unchanged (L8; moving it into the arena makes `os.replace` fail with `EXDEV` across devices). | S3 |
| T13 | Snapshot spill and stage, `postgres_snapshot.py:217-218`, `:455-459`, `:536-539` | Spill via `mkdir(parents)` then chmod. Stage via `mkdir(0o700)`. Ambient only (`:447`). | Spill directory is an arena under the caller's `temp_directory`. Stage unchanged (already 0700). | S3 |
| T17 | Discovery, `data_discovery.py:181-213`, `fileaccess/adapters/duckdb.py:44-61` | In-memory, no `temp_directory` | Decided by probe `T-DISCOVERY-SPILL` (section 4.3) | S3 |

Not scratch: vault (`vault.py:341-348`, A-4 output), model pack (`profiling/model_pack/_packio.py:54-59`, A-4 output), cloud readers (network, no spool), and rotating logs (durable).

### 3.1 Enumerations

Anchors below are on main `0173c054`.

#### 3.1.1 Exit paths of an arena

Each gets a case in `T-ARENA-EXITS`, S1.

- Success then `reclaim()`.
- Exception inside the `with` block then `reclaim()` and re-raise with the primary error kept.
- `reclaim()` failure gives `CleanupReport(RETAINED)` without masking the primary error.
- Double `reclaim()` is a no-op.
- Garbage collection without `reclaim()` runs a `weakref.finalize` reclaim (A-4 precedent, `src/decoy_engine/fileaccess/_output.py:229`).
- Process kill leaves the arena with the lock released, for S5's sweep; a kill inside `open` leaves an empty pending directory (L4).
- Isolated child exit leaves the work-root arena to the parent's `reclaim()` (`src/decoy_engine/execution/_isolated_run.py:364`).

#### 3.1.2 Routes

T-PLAIN and T-MODES cells. Each runs under the ambient policy and a strict `RootedFileAccess`. Routes that A-4 refuses under strict (`require_ambient`) assert the refusal, unchanged.

- OOC batch lane.
- OOC reorder lane.
- Native companion present and absent.
- Sink whole-table and streamed.
- Isolated subprocess.
- Isolated subprocess with `output_dir` in another directory and on another device (T12, V1-B).
- Isolated in-process fallback.
- Postgres snapshot (ambient only, `src/decoy_engine/connectors/postgres_snapshot.py:447`; the cell runs with a stub connection, see section 13).
- Discovery.
- Subset then mask.
- Quarantine (`src/decoy_engine/quarantine.py:313`).
- Fixed width.

#### 3.1.3 FK shapes

Through both OOC lanes. The parity suites (for example `tests/parity/test_out_of_core_fk_parity.py:448`, `:763`) run unchanged before and after S2a and S2b as the class 9 characterization. Class 5 is N/A because no row selection changes.

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
  display = "engine.scratch"; strict = True   # constants, as ScratchAccess handles (_rooted.py:86-90)
  open(*, under: Path | None = None) -> ScratchArena    # under != None: require_ambient first
  create_file(component) -> int        # O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0o600, dir_fd
  open_file(component) -> int
  create_dir(component) -> ScratchArena (child, shares root lock and finished state)
  remove(component), rename(src, dst)  # one dir_fd-relative call each
  local_path(component: str | None = None) -> str   # the only path release (rule below)
  local_stage_path() -> str            # raises, as A-4's _StageDir does (not a sink stage)
  reader() -> ScratchAccess            # existing read-only RootedFileAccess, _scratch.py:30
  reclaim() -> CleanupReport
```

With `display` constant, a scrubbed library failure reads `FileAccessReadError("engine.scratch")` (`guarded`, `_errors.py:142-163`), never a path; with `strict` always `True`, `ENOSPC` and `EDQUOT` keep their class for S4.

Built over A-4 internals, not copied: `validate_component` (`_component.py`), `_call`, `_close`, `_rmtree_fd`, `_DIR_FLAGS` and `_FILE_CREATE` from `_output.py`. The A-4 writers in `fileaccess/adapters/arrow.py:72-126` take `OutputTxn` and use only `create_file`, `open_file`, `display` and `strict`; S1 widens their annotation, and `adapters/serial.py`'s `write_text`, to `StageDir`, so the arena needs no new writer code.

Naming (V1-A). `ARENA_PREFIX = "decoy-arena-v1-"` is defined once, in `fileaccess/_scratch.py`. `open` makes the root with `mkdtemp(prefix="decoy-arena-pending-")`, opens and checks it (4.2), takes `LOCK_SH`, then renames it by `dir_fd` to `ARENA_PREFIX + secrets.token_hex(8)`; a failed rename retries with a new token. No sweep matches the pending name, so none can reap an unlocked arena. `under=` arenas are never swept (L4). Sentry `test_arena_prefix_used_only_by_open`: no module but `fileaccess/_scratch.py` holds a string literal starting `decoy-arena-`, and no `mkdtemp`/`mkstemp` `prefix=` elsewhere starts with it.

Path release (V1-C). `local_path` is the only method that returns a path. Policy rule: an `under=` arena releases only under the ambient policy and otherwise raises `FileAccessUnavailable` (mirroring `_local.py:174` against `_output.py:185`); an engine-rooted arena releases under any policy (section 0, deviation). Callers, exactly: `execution/_isolated_run.py` (payload, `sources/` manifest, `staging_output` when `output_dir` is None, given to the child at `:446`), `execution/_isolated_commit.py` (only if S3 moves the payload writer there), `execution/out_of_core/_relation.py` and `_multi_parent.py` (`COPY` targets, `read_parquet`), `out_of_core/_duckdb.py` (`temp_directory`), and `fileaccess/adapters/duckdb.py` (only if T17 picks the arena fix). The list is a new `scratch_path_release` key in `tests/sentry/file_access_modules.toml`; the boundary sentry fails on any other caller and on a listed module with no call. Join modules read `ParentKeyRelation.path`, released in `_relation.py`, and need no entry. Released paths are L1. A transitional `_runner.py` entry lives from S2a to S2b (section 10).

### 4.2 Descriptor checks (the whole fail-closed default)

At open: `fstat` on the root descriptor must show `S_ISDIR`, `st_uid == geteuid()` and `S_IMODE == 0o700`. Otherwise `ScratchPathUnsafe` (code-only text) is raised and nothing is changed. At `create_dir` and `create_file`: `O_NOFOLLOW` plus `O_EXCL` or `mkdirat`, then `fstat` of the new descriptor for type, owner and mode. Nothing else is required (no quota, no fscrypt); T-PLAIN guards that.

### 4.3 Producers

T01-T08 and T11-T13 per section 3. T17: if `T-DISCOVERY-SPILL` shows DuckDB writes outside the arena for an in-memory untrusted session, `untrusted_session` sets `temp_directory` to `arena.create_dir("duckdb").local_path()` at connect, before `lock_configuration`. If it shows no spill is possible with external access off, T17 records that as a tested fact and makes no production change. The decision and the probe output go into the S3 evidence. Discovery keeps working in both cases (rule 1).

### 4.4 Sweep (S5)

`fileaccess.sweep_scratch(root: Path | None = None) -> SweepReport`. It is called by the CLI or worker, never on import and never in the background (Q7). For each entry of the scratch root, opened by descriptor:

1. Consider only names matching `^decoy-arena-v1-[0-9a-f]{16}$` (4.1). No other name is opened for deletion: pending `decoy-arena-pending-*`; the platform's `decoy-job-*`, `decoy-preflight-*`, `decoy-src-fd-*` (decoy-platform `api/jobs/v2_sequential.py:394`, `preflight_dispatch.py:249`, `dispatch_worker_main.py:268`); the engine's `decoy-ooc-*`, `decoy-isolated-run-*`; pre-S5 directories. A matching entry that is not a directory (`O_NOFOLLOW`), not owned by `geteuid()`, or not mode 0700 is `retained_unknown` and never deleted.
2. Try `flock(LOCK_EX | LOCK_NB)` on the entry's own descriptor. `EWOULDBLOCK` means a holder is alive: `retained_live`.
3. With the exclusive lock held, `_rmtree_fd` the contents, then `rmdir` the entry by name under the root descriptor. A failure leaves the entry for the next run and counts it as `failed`. A second sweep picks it up (idempotent).

Pattern: Linux `flock(2)` on the open file description (held across `fork` and `exec` for inherited descriptors). No fencing or lease generations: V1 is ephemeral with no resume (Q3). The pending-then-rename open (4.1) closes the `mkdtemp`-to-`LOCK_SH` window; the window between the sweep's lock and `rmdir` is the class 7 residual (L6, V2-LOG).

### 4.5 Not carried from `feat/f25-protected-scratch` (pre-GA hard delete, best-practices 8.1 and 8.2)

Not ported (they duplicate A-4 or belong to the cut): `scratch/_capabilities.py`, `scratch/_seam.py`, the publication staging in `scratch/_arena.py`, `execution/_isolated_cap.py`, `tests/sentry/test_scratch_allocation_sentry.py` with its manifest, and `tests/scratch/test_fd_only_filesystem_sentry.py`. Deferred with their features (Q2, Q3): `scratch/_ledger.py`, `_authority.py`, `_manager.py`, `_policy.py`, `_provider.py`, `_local_provider.py`. Ported, trimmed: `scratch/_reports.py` as `fileaccess/_scratch_reports.py`, and `ScratchPathUnsafe` from `scratch/_errors.py`. The F25 branch stays as reference until the owner retires it (Q6).

## 5. Acceptance tests, red first

Each test is recorded red before the change, against its neutered control or main's behavior at the fail-before location; an `ImportError` is not a red. A test green on main by design says so and proves non-vacuity by its mutation. Baselines run from `uv.lock` in the branch environment.

| Test (file::name) | Scenario and observable result | Fail-before location (red reason) | Mutation that must turn it red | Slice |
|---|---|---|---|---|
| `tests/fileaccess/test_scratch_arena.py::test_modes_under_umask[000,002,022]` (T-ARENA-DESC) | Create arena, nested dirs and files; each created dir is 0700, each created file 0600, owner is euid | Neutered control: `create_file` with mode `0o666` (A-4's `_output.py:222` default) shows 0644/0666 | `0o600` changed to `0o644` | S1 |
| `...::test_refuses_unsafe_root[symlink,wrong_mode,not_dir]` | Root replaced by symlink, made 0755, or made a file before open: `ScratchPathUnsafe`, nothing changed on disk | Control without the `fstat` checks opens it | Delete the mode comparison | S1 |
| `...::test_exit_paths` (T-ARENA-EXITS) | The section 3.1 exits; the primary error is kept; a failed reclaim reports `RETAINED` | Control whose reclaim swallows errors reports `REMOVED` | Return `REMOVED` on `OSError` | S1 |
| `tests/fileaccess/test_scratch_plain_host.py::test_jobs_succeed_without_quota_or_fscrypt` (T-PLAIN) | On the CI container filesystem (overlayfs or tmpfs, no `prjquota`, no fscrypt), run OOC both lanes with forced DuckDB spill, subset then mask, and the hold-back sink. All succeed and output bytes equal the `0173c054` run. | Revision 1's design (umbrella 1.5 line 142): neutered control where `ScratchArena.open` requires a project quota fails with `scratch_backend_unavailable` | Add a quota-qualification precondition to `open` | S1, re-run S2a, S2b, S3, S5 |
| `...::test_report_text_is_value_free` (T-REPORT-TEXT) | Sentinel path, table, value and DSN; `str` and `repr` of reports and carrying errors contain none | Control report with a `path` field leaks it | Add `detail=str(exc)` | S1, S4, S5 |
| `tests/fileaccess/test_scratch_arena.py::test_local_path_policy` | `under=` arena: `local_path` raises `FileAccessUnavailable` under strict, returns under ambient. Engine-rooted arena: returns under both | Control refusing every strict release fails the strict `out_of_core_fk` golden (`test_policy_routes.py:69`) from S2a on | Drop the `under=` ambient check | S1 |
| `tests/sentry/test_file_access_boundary.py::test_local_path_callers_are_allowlisted` | A `local_path` call outside `scratch_path_release` fails; a listed module with no call fails; seeded `fa_fixtures` evasions (alias, `getattr`, bound method) are caught | Fixture caller outside the list passes without the rule | Add a `local_path` call to `_stage.py` | S1 |
| `tests/sentry/test_scratch_arena_prefix.py::test_arena_prefix_used_only_by_open` | No `decoy-arena-` literal or `mkdtemp`/`mkstemp` `prefix=` with it outside `fileaccess/_scratch.py`; `open`'s name matches the sweep pattern | Seeded fixture using the prefix passes without the sentry | Add `prefix="decoy-arena-v1-x"` to `_isolated_run.py:367` | S1 |
| `tests/execution/out_of_core/test_scratch_modes.py::test_ooc_scratch_modes[batch,reorder]` (T-MODES, T01-T05) | Run OOC under umask 000 with a hook (`os.mkdir` and `os.open` wrappers, plus a post-run `fwalk` taken before reclaim) recording modes; each engine-created dir in the run is 0700, Python-created file 0600 | `_runner.py:244`, `:268-270` and `_stage.py:66` create subtrees `0777` under umask 000 on `0173c054` | Restore `Path.mkdir(parents=True)` in `_stage.py` | S2a |
| `...::test_caller_temp_dir[mode_untouched,missing_created]` | Caller `temp_dir` with mode 0755 keeps 0755 and the arena is a 0700 child; a missing `temp_dir` is created (ambient) and the job succeeds | `_scratch.py:26` chmods it to 0700 on main; `missing_created` is green on main by design | Restore the chmod; drop the `mkdir(parents=True)` | S2a |
| `...::test_ooc_internals_take_only_an_arena[relation,connect_duckdb]` | The relation builders (S2b) and `connect_duckdb` (S4) reject a `Path`; the `_runner.py` allowlist entry is gone | Red at the S2a head by design (transitional arms, section 10) | Restore a `Path` arm | S2b, S4 |
| `...::test_spool_and_sorter_modes` (T-MODES, T06-T08) | Payload, raw-key, child-key spools and sorter runs (S2b), and quality pairs (S4, with T08), are 0600 under umask 000; bounded RSS unchanged (existing bounded-memory tests) | `_payload_store.py:96`, `_external_sort.py:381` create umask-mode files on main | Restore `pa.OSFile(str(path), "wb")` | S2b, S4 |
| `tests/execution/test_isolated_payload_mode.py::test_payload_never_readable_by_others` (T-PAYLOAD) | Caller `staging_dir` 0755, umask 022: a wrapper on the payload write records the mode at first byte; it must be 0600; the work root and `sources/` are 0700 and each `sources/s*.parquet` is 0600 | `_isolated_run.py:429` writes 0644 before `:434` chmods | Write then chmod again | S3 |
| `...::test_child_holds_arena_after_parent_death` | Parent killed while the child writes; the inherited arena descriptor keeps `LOCK_SH`; a `LOCK_EX|LOCK_NB` probe fails | Control without `pass_fds` for the arena descriptor: the probe succeeds | Drop the descriptor from `pass_fds` | S3 |
| `tests/execution/test_isolated_payload_mode.py::test_output_dir_commit_keeps_sibling_staging[other_dir,other_device]` | Isolated subprocess with `output_dir` in another directory, and on `/dev/shm` (tmpfs) against the pytest tmp: completes, output committed, staging was beside `output_dir`. `other_device` first asserts the `st_dev` differ; never skipped; evidence names the pair | Green on main by design | Stage `output_dir` jobs in the arena (revision 2's T12): `CommitError` from `EXDEV` | S3 |
| `tests/connectors/test_snapshot_spill_mode.py::test_spill_dir_created_0700` | Spill helper under umask 022 creates 0700 at creation (stub connection, no Postgres) | `postgres_snapshot.py:217` creates 0755 then chmods | Restore `mkdir` then `chmod` | S3 |
| `tests/test_discovery_spill.py::test_discovery_spill_stays_in_arena` (T-DISCOVERY-SPILL) | Discovery query sized past `memory_limit`: no `.tmp` in the working directory; any spill lands under the arena | Outcome unknown before the probe (section 13). If green on main, recorded as a tested fact. | Remove `temp_directory` from the session config (only if the probe chose the arena fix) | S3 |
| `tests/execution/out_of_core/test_scratch_quota.py::test_budget_exceeded_commits_nothing` (T-A02a) | OOC with a sink and a tiny `temp_disk_budget_bytes`: raises `out_of_core_temp_disk_exceeded` with a `QuotaReport`; the destination has no final output | Green on main for "no output" by design (sink abort); red for `report` (attribute absent) | Move the sink commit before the budget check (output appears) | S4 |
| `...::test_enospc_commits_nothing[T02,T06,T07,T08,T11]` (T-A02b) | `ENOSPC` and `EDQUOT` injected by patching `fileaccess.adapters.arrow._writable` to return a faulting file object over the real descriptor (its `write` raises after N bytes; pyarrow writes through it as a Python file), for each Python writer family; the payload cell patches `os.fdopen` in `fileaccess.adapters.serial` the same way: the job fails, nothing is committed, `QuotaReport(dimension="bytes")` | Main raises a bare `OSError` with no report; injected, not a real full disk (section 13) | Catch and continue the write | S4 |
| `tests/execution/test_crash_mid_spool.py::test_sigkill_mid_spool_commits_nothing` (T-A08) | Subprocess runs OOC with a sink; the test harness (not production code) pauses after the first spool batch; SIGKILL; the destination has no final output, and the arena remains with its lock released | Green on main by design (A-4 stage); mutation proves non-vacuity | Publish the stage before spooling ends | S4 |
| `tests/fileaccess/test_publication_crash_matrix.py::test_crash_points[T09,T15,T16]` (T-A10) | SIGKILL before stage fsync, after the publish rename, after the parent fsync; the final name holds the old output or the complete new output, never a partial | Green on main by design | Write directly to the final leaf | S4 |
| `tests/fileaccess/test_scratch_sweep.py::test_live_arena_survives` (T-A07) | Holder process paused mid-spool; sweep from a second process gives `retained_live`, bytes unchanged; two concurrent sweepers after the holder dies: one reaps, the other reports, no error; unlocked 0700 same-UID entries named `decoy-job-x`, `decoy-preflight-x`, `decoy-src-fd-x`, `decoy-ooc-x`, `decoy-isolated-run-x`, `decoy-arena-pending-x` and `decoy-arena-v1-nothex`, a foreign-UID entry where the host allows, and a 0755 match are never touched | Neutered control: prefix-only `rmtree` sweep deletes the live arena | Drop the `flock` probe | S5 |
| `...::test_sweep_in_open_window` | Barrier pauses `open` after `mkdtemp` and after `LOCK_SH`; a sweep at each pause leaves the arena, which then works | Control creating the final name first, locking after, is reaped at the first pause | Create the root directly under the final name | S5 |
| `...::test_reap_after_kill_and_resume_after_sweeper_crash` (T-A08-SWEEP) | Kill the owner, sweep reaps and reports; kill the sweeper mid-delete, the second sweep finishes | Control without retry semantics leaves a partial | Count a failed delete as reaped | S5 |
| Ratchet M-RATCHET | Add a `tempfile.mkdtemp` in `_relation.py`: A-4's boundary sentry fails | Existing mechanism; the registry count must drop with each migrated site | Raise the registry count | S2a, S2b, S3 |

## 6. Class table (classes 1 to 10)

| # | Class | Applies | Closing structure | Test | Mutation |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: reports and errors reach `Job.log` and the API | Frozen reports with no path or name fields; errors raised `from None` with code-only text | T-REPORT-TEXT, T-PAYLOAD | add a `detail` field |
| 2 | Cleanup on an exit path | Yes | Arena `reclaim` on each enumerated exit (3.1), finalizer, `RETAINED` on failure; sweep for kills, scoped to a name only `open` issues | T-ARENA-EXITS, T-A08, T-A08-SWEEP, T-A07 look-alikes, prefix sentry | swallow a reclaim error; widen the sweep pattern to `decoy-` |
| 3 | State and concurrency | Yes: sweep against owner, two sweepers | `flock` on the open file description, inherited by the child, taken before the rename into the swept name | T-A07, child-holds test, `test_sweep_in_open_window` | drop `LOCK_SH` or `pass_fds`; create under the final name |
| 4 | Route parity and silent fallback | Yes | One required arena (I2), no unmanaged fallback, A-4 `require_ambient` kept on the caller-path routes; no profile gate (G2) | T-PLAIN, T-MODES both lanes, strict-policy cells, `test_local_path_policy`, `output_dir` commit cells | add a quota precondition; restore a `None` default; stage `output_dir` jobs in the arena |
| 5 | Referential integrity | N/A: no row selection changes | Existing parity suites as characterization | FK parity suites | n/a |
| 6 | Untrusted producer | Partly: DuckDB native files, discovery SQL | Native files confined to a 0700 arena dir; untrusted session config locked after `temp_directory` | T-DISCOVERY-SPILL | unlock or remove `temp_directory` |
| 7 | Check then use on a path | Yes, bounded | Descriptor checks at open; `dir_fd`-relative ops; paths only via `local_path` (policy rule, caller allowlist); residuals L1, L6 named | T-ARENA-DESC, `test_local_path_callers_are_allowlisted` | delete a `fstat` comparison; add an unlisted caller |
| 8 | Weak tests and overclaimed evidence | Yes | Section 1 table with "not shown" column; red forms named per test; injected failures labelled as injected | all | n/a |
| 9 | Refactor drift | Yes: producer migration | Byte-identical output parity before and after each slice; no behavior change beyond modes and the report | T-PLAIN, parity suites | reorder a write |
| 10 | Frozen contract drift | Yes | Public entry-point signatures unchanged; reason registry unchanged (Q4); A-4 `CONTRACT_VERSION` unchanged (additive export) | signature snapshot, `test_file_access_entry_points.py` | rename an export |

## 7. Reuse inventory

- A-4 `StageDir` and `OutputTxn` protocols, `_types.py:179-229`; `RootedOutputDir.begin` for T09, T10, T14, T15 and T16 (unchanged).
- A-4 `_output.py` helpers: `_call` (errno-only errors), `_close`, `_rmtree_fd`, `_DIR_FLAGS`, `_FILE_CREATE`; `_component.validate_component`.
- A-4 adapters: `arrow.parquet_writer`, `ipc_new_file`, `ipc_new_stream`, `write_table` (`adapters/arrow.py:72-126`), with the annotation widened; `duckdb.connect_internal` and `untrusted_session` (`adapters/duckdb.py:44-68`).
- `ScratchAccess` and `scratch_access_for` (`out_of_core/_scratch.py:30-61`) for reads.
- `fileaccess.require_ambient` (`_entry.py:115`) for the caller-named roots.
- A-4 boundary sentry, registry and module roles (`tests/sentry/test_file_access_boundary.py`, `file_access_registry.toml`, `file_access_modules.toml`) as the G5 ratchet and the `local_path` allowlist; module-size sentry `tests/sentry/test_module_size.py`.
- From `feat/f25-protected-scratch`: `scratch/_reports.py`, trimmed.
- Existing reason `out_of_core_temp_disk_exceeded` (`reasons/runtime/_part2.py:150`) and `check_temp_disk_budget` (`out_of_core/_budget.py:525`).

## 8. Frozen contracts touched

- `fileaccess` public surface: additive exports `ScratchArena`, `sweep_scratch`, `QuotaReport`, `CleanupReport`, `SweepReport`, `ScratchPathUnsafe`. `CONTRACT_VERSION` stays 1 (additive). `ENTRY_POINTS` unchanged.
- Public entry-point signatures: unchanged. `run_fk_out_of_core(temp_dir=...)`, `run_pipeline_isolated(staging_dir=...)` and the snapshot `temp_directory` keep their meaning, except that the engine now creates a private child inside a caller directory instead of using or chmodding the directory itself. An isolated job with `output_dir` still stages beside it and commits by one rename (T12). CHANGELOG entry in S2a and S3.
- Reason registry: unchanged (Q4).
- Output bytes: unchanged (T-PLAIN parity).

## 9. Owner questions (recommended defaults first)

- Q1. At-rest encryption: **not in V1 (owner's answer).** 0700 and 0600 stop non-admin local users; fscrypt guards against root and offline disks, and root is the admin.
- Q2. Kernel project quotas: **an optional profile deferred to V2 (recommended).** The V1 bound is the OOC temp-disk budget plus the host's `ENOSPC`/`EDQUOT`, both as a `QuotaReport`. Alternative: port `feat:scratch/_local_provider.py` as an opt-in S6.
- Q3. SQLite `ResourceLedger` and `OwnershipAuthority`: **not carried (recommended).** For ephemeral-only V1 the sweep's authority is the `flock` lock plus the name, owner and mode checks.
- Q4. Reason codes: **no new code (recommended).** OOC keeps `out_of_core_temp_disk_exceeded`, other routes keep their current errors, and each gains an optional `report`. This avoids a registry bump that collides with A-4's 1.2.0 and B-6.
- Q5. S2b throughput budget for fd-backed IPC writers (`os.fdopen` file objects instead of `pa.OSFile` paths): **median regression at most 5% on the sorter-merge and payload-store benchmarks, same host, five runs, fixed first (recommended).** If missed, T06 and T07 keep path writers under the 0700 directory (L1 widened to them).
- Q6. Build base: **a fresh `feat/f25-v1-scratch` from `origin/main` (recommended).** `feat/f25-protected-scratch` stays frozen as reference; retiring it is the owner's call.
- Q7. Sweep invocation: **an exported `sweep_scratch` that the CLI or worker calls at startup or on a schedule, never on import or in the background (recommended).** It touches only `decoy-arena-v1-` names (4.4), so it is safe beside live platform job directories in the same temp root.
- Q8. Caller-named roots (`temp_dir`, `staging_dir`, snapshot `temp_directory`): **ambient-only as on main; the engine makes a fresh 0700 child inside and never changes the caller directory's mode (recommended).**

## 10. Slices

Bound: each slice changes at most 10 production files and 800 lines (production, test and doc). Figures are estimates scaled from the `feat:` diffstat (`git diff --stat 33ef629b 4c901d38`); each slice's evidence records the actual diffstat, and a slice over the bound is split before review, not waived.

| Slice | Unit | Production files | Est. changed lines (prod + tests + docs) | Content | Gate |
|---|---|---|---|---|---|
| S1 | 1. Port onto A-4 | 5: new `fileaccess/_scratch.py`, new `fileaccess/_scratch_reports.py` (`CleanupReport` only), `fileaccess/__init__.py`, `fileaccess/adapters/arrow.py` and `serial.py` (annotations) | about 790 (about 390 + 360 + 40 for the sentry rules and docs) | Arena, naming, `local_path` rule, descriptor checks, exits, transitional `_adopt`; T-ARENA-DESC, T-ARENA-EXITS, T-PLAIN, T-REPORT-TEXT (`CleanupReport`), `test_local_path_policy`, both sentries. At the bound: over 800 in the evidence, the sentries split to S1b | Dennis, then Codex |
| S2a | 2. Thread the arena (OOC batch lane and shared core, T01-T05) | 8: `out_of_core/_scratch.py`, `_runner.py`, `_stage.py`, `_relation.py`, `_multi_parent.py`, `_duckdb.py`, `_batch_join.py`, `_join.py` | about 740 (about 250 + 490) | Required arena through the batch lane; transition below; caller `temp_dir` cells, T-MODES (batch), T-PLAIN, M-RATCHET. `_runner.py` capped at 644 (`test_module_size.py:120`): net zero; `_relation.py` at 595: at most +5 | Codex |
| S2b | 2. Thread the arena (reorder lane, T06-T07) | 9: `_payload_store.py`, `_external_sort.py`, `_external_sort_run.py`, `_stream_driver.py`, `_stream_join.py`, `_emit.py`, `fileaccess/adapters/arrow.py` (`ipc_iter_stream`, moved from S1), and, to end the transition, `_runner.py` and `_relation.py` | about 720 (about 230 + 490 including the Q5 benchmark) | fd-backed spools and runs; bounded-RSS suites; Q5 budget first; `test_ooc_internals_take_only_an_arena[relation]`. `_stream_join.py` capped at 645 (`test_module_size.py:124`): net zero | Codex |
| S3 | 2. Thread the arena (non-OOC, T11-T13, T17) | 6: `_isolated_run.py`, `_isolated_worker.py`, `_isolated_commit.py`, `connectors/postgres_snapshot.py`, `data_discovery.py`, `fileaccess/adapters/duckdb.py` | about 730 (about 230 + 500) | T-PAYLOAD with `sources/`, child-holds, `output_dir` commit cells, snapshot spill mode, T-DISCOVERY-SPILL probe first. `_isolated_run.py` is capped at 651 (`test_module_size.py:71`): the payload writer moves to `_isolated_commit.py`, net change at most zero | Dennis (rule 3 fix), then Codex |
| S4 | 3. Quota report and crash behavior; T08 | 7: `out_of_core/_runner.py`, `out_of_core/_budget.py`, `fileaccess/_scratch.py` (deletes `_adopt`), `fileaccess/_scratch_reports.py` (`QuotaReport`), `execution/_isolated_run.py`, `execution/native/_pool_quality.py` (T08; tests only, no production caller), `out_of_core/_duckdb.py` (drops the last `Path` arm) | about 780 (about 180 + 600) | T-A02a, T-A02b, T-A08, T-A10, T-REPORT-TEXT (`QuotaReport`), `test_ooc_internals_take_only_an_arena[connect_duckdb]`; report on existing errors, `from None`. `_isolated_run.py` (651) and `_runner.py` (644) net zero: offsets in-file or moves to `_isolated_commit.py` / `_budget.py` | Codex |
| S5 | 4. Live-arena sweep | 4: new `fileaccess/_scratch_sweep.py`, `fileaccess/_scratch.py` (lock helper), `fileaccess/_scratch_reports.py` (`SweepReport`), `fileaccess/__init__.py` | about 760 (about 260 + 500) | T-A07 with look-alikes, `test_sweep_in_open_window`, T-A08-SWEEP with real subprocesses; T-PLAIN re-run; docs: `docs/security/file-access-policy.md` scratch section, CHANGELOG | Dennis, then Codex |

Transition (a transitional adapter; I2 is suspended only here, each end named). `stream_table` (`_stream_driver.py:124`) takes `Path` directories until S2b, as do the stream-lane callers `_emit.py:137`, `:150` and `_stream_join.py:530`, and T08 (`_pool_quality.py:246`) until S4. So (a) in S2a `_runner.py` passes `stream_table` `child.local_path()` under a transitional allowlist entry, removed in S2b; (b) `build_parent_key_relation_aligned` (until S2b) and `connect_duckdb` (until S4) keep a `Path` arm through `ScratchArena._adopt(path)`, which only opens and checks (4.2) an existing engine-made 0700 directory. `test_ooc_internals_take_only_an_arena` is red at the S2a head; the sentry's no-call rule forces the entry out; S4 deletes `_adopt`.

Order: S1, S2a, S2b, S3, S4, S5. S3 depends only on S1 and may run beside S2a and S2b on separate worktrees, one writer each. S4 needs S2a, S2b and S3, because T-A02b runs the T02-T11 writers. S5 needs S1 and S3 (the inherited lock).

Units, as in the handoff: rebase onto A-4 (S1), one non-optional arena through the internal producers (S2a, S2b, S3: one unit split three ways by the size bound), the 0700/0600 default plus the quota report (S1 and S4), and the live-arena sweep (S5). Four units in six slices, about 4,520 changed lines (revision 2: 4,240; the rise is the two sentries, the transition and the new cells).

Loop per slice: tests recorded red first (or green by design with their mutation); baselines from `uv.lock` in the branch environment; each new mechanism mutated once with its test shown red, never committed mutated; then lint, format, mypy, touched suites, `tests/sentry`, `tests/fileaccess`.

## 11. V2-LOG (listed, not planned) and deferred features

V2-LOG, with defect class. Revision 2's items stand, with anchors in `937e88fe` section 11: [7] SQLite `/proc` identity bracket; [7] `NativePathLease`; [7] `rmdir` `st_nlink` post-check; [8] whole-repo taint sentry C21; [1] numeric `QuotaReport.errno` (`feat:src/decoy_engine/scratch/_reports.py:83`) and the quarantine exception class name (anchor still not located); [8] six gate groups for one threat class; [10] stale umbrella anchors. Live in this plan:

- [7] Same-UID interference in the sweep window between `flock` and `rmdir` (L6): `fileaccess/_scratch_sweep.py` (new, S5).
- [7] Native and released-path file modes inside the arena (L1): `out_of_core/_relation.py:517`, `_multi_parent.py:147`, `execution/_isolated_worker.py:133`.
- [2] An empty `decoy-arena-pending-*` directory left by a crash inside `open` is never reaped (L4): `fileaccess/_scratch.py` (new, S1).

All eight V2-LOG items of the revision 2 review were plan edits and are folded in (section 0).

Deferred features (not defects): fscrypt at-rest encryption and key envelopes (Q1; umbrella slice 4b, 1.6, 1.7), the kernel project-quota provider (Q2; `feat:scratch/_local_provider.py`), the ledger and ownership authority (Q3), checkpoint and resume (umbrella slice 6), and the platform lock-authority integration (umbrella section 4).

## 12. Pre-mortem: likely gate findings and the red-first test for each

1. "The sweep deletes a platform job's stage root." The pattern matches only names `open` issues. Test: T-A07 look-alikes, the prefix sentry.
2. "A strict-policy OOC job breaks on `local_path`." Engine-rooted arenas release under any policy. Test: `test_local_path_policy` and the strict route-corpus golden.
3. "`output_dir` on another device fails the commit." T12 leaves sibling staging. Test: the `output_dir` commit cells.
4. "A strict-policy job now follows a caller path." `require_ambient` runs before an `under=` arena is made. Test: the strict-policy cells in T-MODES (strace helper `tests/fileaccess/strace_support.py`).
5. "A plain host breaks." T-PLAIN runs in each slice from S1 on.

## 13. What this revision did not run or verify

- `plan-lint plan` (decoy-platform `handoffs/queue` `docs/handoffs/tools/plan-lint`) was run and printed OK. Nothing else was run: no test exists, so no red or green is claimed, and line estimates are estimates.
- `/home/cam/dev-rules/*` is absent here and was not read.
- Platform anchors were read on `handoffs/queue` (`ac3a53f`), not on platform `main`.
- Whether `/dev/shm` and the pytest tmp are on different devices in CI (the `other_device` cell) is unverified.
- `T-DISCOVERY-SPILL`'s premise is unverified. The snapshot cell uses a stub connection. `ENOSPC`/`EDQUOT` in T-A02b are injected, not a real full filesystem.
- `flock` on overlayfs upper directories is not verified; NFS is unsupported (L7). That a `flock` survives a directory rename is from `flock(2)` (the lock is on the open file description), not tested yet.

## 14. Provenance

- Revision 1 `affde556`; revision 2 `937e88fe` (Dennis under the V1 bar: GO-WITH-FIXES, 3 V1, 8 V2-LOG). Revision 3 answers it in section 0.
- Next gates: Dennis on this exact document, then Codex. No build before owner approval.

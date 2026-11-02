# F25 V1 scratch on A-4: one arena capability, permissions by default, report and sweep

Status: plan, revision 4 (handoff "F25 4c consumer capabilities plan revision 4: three V1 fixes"), a targeted amendment answering Codex on `98a72bb6` (GO-WITH-FIXES, 1 V1) and Dennis on `98a72bb6` (GO-WITH-FIXES, 2 V1). Section 0 maps each finding to its change. No code is on this branch, and no build is authorized by this document.

Summary: engine scratch is created 0700 (directories) and 0600 (files the engine creates) under any umask, through one arena capability that is an A-4 `StageDir`. A job that runs out of scratch space stops with a value-free report and commits no output. A sweep removes abandoned arenas and leaves live ones alone. Quotas and encryption are optional profiles.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `development-loop.md`, `risk-and-exceptions.md`, `scope-discipline.md`, `reuse-first.md`, and the rulebooks the README routing table selects for a security-relevant storage plan: `architecture.md`, `feature-dev.md`, `security.md`, `testing.md`, `verification.md`, `documentation.md`, `workspace-and-vcs.md`. That path is on the owner's machine and not mounted in the container this revision was written in; names from the handoff. Read here: decoy-platform `handoffs/queue` `docs/handoffs/V1-BAR.md` and `DEFECT-CATALOG.md`, the engine `CLAUDE.md`, and the code at each anchor below.

Risk: R2 (plan only): scratch confidentiality, on-disk cleanup, and the OOC, subset, snapshot and spill failure paths. Not R3: the sweep deletes only names only `ScratchArena.open` produces (4.4), is caller-invoked, and rollback is a revert on a pre-GA engine (`release.py`).

Baselines. `origin/main` `0173c054` (A-4 as `da1a522f`), merged here as `3246830`; plan branch `98a72bb6` before this revision. Anchors are on `0173c054` (re-read); `feat:` is the frozen `feat/f25-protected-scratch` (`4c901d38`).

## 0. Revision 4: finding map

Both gates accepted revision 3's path-release deviation (an engine-rooted arena releases a path under any policy; an `under=` arena only under the ambient policy, section 4.1) and found revision 3's V1-A (sweep prefix) and V1-B (isolated staging) closed; those parts are unchanged.

| Finding | Class | Change | Test (new or changed) |
|---|---|---|---|
| V1-A (Codex, rule 3). DuckDB failures on released paths are re-raised unchanged (`out_of_core/_relation.py:517`, `_multi_parent.py:147`, `_batch_join.py:160`, `_stream_join.py:551`): arena paths, identifiers or SQL reach `Job.log` and the API. | 1 | One value-free boundary, `scratch_native`, in the arena module around every native call that consumes a released path; its callers are a `scratch_native` key in the allowlist sentry (4.1). | `test_native_failure_text_is_value_free` (S1a helper; cells in S2a-1, S2a-2, S2b, S4b) |
| V1-B (Dennis H1, rule 1). `MaskedKeyStager` is built only in `_emit.py:109`, reached by both lanes (`_runner.py:505`, `_stream_driver.py:454`); S2a gave `_stage.py` a required arena without it. | 9, 4 | `_emit.py` in S2a-1; transitional arm (d) (section 10); T03 corrected. | T-MODES at the S2a-1 head runs an outgoing FK edge in both lanes; `test_ooc_internals_take_only_an_arena[emit]` (S2b) |
| V1-C (Dennis H2, rule 1). `_adopt` as written raises on the missing leaf `_stream_join.py:530` passes, and an arena finalizer deletes the adopted `relation_dir` that `ParentKeyRelation.path` still names. | 2, 4 | `_adopt` specified (10): creates missing components 0700, then 4.2; non-owning (3.1.1). | `test_adopted_arena_is_non_owning` (S2a-1) |
| M1-M3, L1-L5, Codex V2-LOG plan items | 7, 8, 1, 9 | Release callers, `ScratchAccess` move (4.1); migration, splits, delivery unit (10); error class (8); L1-L5 in place. | `test_arena_is_stagedir`, T-PLAIN `[error_path]` |

Cut so the plan does not grow (at most revision 3's 319 lines and 49.5 KB): revision 3's section 0 and 4.5 list (in `98a72bb6`), the revision 2 V2-LOG list (`937e88fe`), G1-G7 and test-table prose, the out-of-scope line (now section 11), and pre-mortem items 4 and 5 merged.

Stop-on-repeat: V1-A repeats class 1, V1-B and V1-C class 9 (a transition drifting from the code it bridges); none outside classes 1 to 10. Fixed at the class: one boundary with a caller sentry; one non-owning `_adopt` whose four named arms a test forces out.

## 1. Guarantee (V1 rules 1 to 3 only: a ratchet plus named limits)

| # | Claim | V1 rule | Shown by | Not shown |
|---|---|---|---|---|
| G1 | On section 3's producer rows (T01-T08, T11-T13, T17), engine-created scratch directories are 0700 and owned by the engine UID, and files its Python creates are 0600, at creation, under umask 000, 002 and 022. | 3 | T-MODES (S2a-1, S2a-2, S2b, S3, S4b), T-ARENA-DESC (S1a) | Native and released-path files take the umask mode inside the 0700 arena (L1); later producers only via G5. |
| G2 | On a host without project quotas or fscrypt (overlayfs, tmpfs, ext4 without `prjquota`), OOC, subset, snapshot-shaped and forced-spill jobs succeed with `0173c054`'s output. | 1 | T-PLAIN (S1a for the arena, re-run in S2a-1, S2a-2, S2b, S3 and S5) | An unwritable scratch root still fails, as today. |
| G3 | Over the OOC temp-disk budget, or on `ENOSPC`/`EDQUOT` in a scratch write, the job stops, the sink aborts, nothing is committed, and the error carries a frozen `QuotaReport`. | 2, 1 | T-A02a, T-A02b (S4a; T08 cell S4b) | Errno-less DuckDB failures report `dimension="unknown"` (L3); without a kernel quota the bound is the budget check (`out_of_core/_budget.py:525`), not a write cap (L2). |
| G4 | A kill mid-spool commits nothing; a kill at a publication step leaves the old or the complete new output. | 2 | T-A08 (S4a), T-A10 (S4a) | Crash-left stages in output directories stay for the operator (L4). |
| G5 | A new `tempfile.*` call, DuckDB `temp_directory` or path-taking Arrow/Parquet writer outside `fileaccess/_scratch.py` and the adapters fails A-4's boundary sentry until a reviewed entry is added. | ratchet | `tests/sentry/test_file_access_boundary.py`, count in `file_access_registry.toml` (192 today), M-RATCHET | Calls the scanner does not model (L5). |
| G6 | `str()` and `repr()` of `QuotaReport`, `CleanupReport`, `SweepReport`, the errors carrying them, and any error leaving a native call on a released path (`scratch_native`, 4.1) contain no path, table name, column name or cell value. | 3 | T-REPORT-TEXT (S1a, S4a, S5); `test_native_failure_text_is_value_free` (S1a to S4b) | Admin logs, tracebacks, `__cause__` (V2). |
| G7 | `sweep_scratch` acts only on `^decoy-arena-v1-[0-9a-f]{16}$`, issued only by `ScratchArena.open`; it never removes a held arena (owner, child with the inherited descriptor, one inside `open`) and is safe to re-run after a crash mid-delete. | 2, 1 | `test_arena_prefix_used_only_by_open` (S1b); T-A07, `test_sweep_in_open_window`, T-A08-SWEEP (S5) | Deliberate same-UID lock games (L6); cross-host filesystems (L7); the foreign-UID cell runs only as root, so not in CI. |

Named limits: L1 native and released-path files (DuckDB `COPY TO`, spill, the isolated child's path writes) take the umask mode inside the arena; L2 no hard byte cap without the optional quota profile; L3 errno-less native I/O failures; L4 never swept: stale output-directory stages, `under=` arenas, an empty pending directory from a crash inside `open`; L5 the sentry's scanner model; L6 same-UID interference (including a same-UID rename onto an empty arena-named directory, 4.1); L7 single-host `flock`; L8 publication stages keep the output mode (A-4 `_output.py:222`, isolated `sibling_staging_dir`).

## 2. Invariants

- I1. No producer in section 3 creates a scratch directory or a Python-written scratch file except through a `ScratchArena` (`StageDir` protocol, `fileaccess/_types.py:179`).
- I2. Internal producers take `arena: ScratchArena` as a required argument: no default, no `None`, no `getattr` fallback. Public entry points create the arena themselves. No unmanaged fallback route exists.
- I3. An arena root is created by the engine (`mkdtemp` under the platform temp root, or a fresh 0700 child of a caller directory under the ambient policy only) under a pending name, opened `O_DIRECTORY | O_NOFOLLOW`, checked by `fstat` (directory, owner `geteuid()`, mode 0700), locked, and only then renamed to its `decoy-arena-v1-` name (4.1). It is held by descriptor for its lifetime.
- I4. The arena holds `flock(LOCK_SH)` on its root descriptor from before its rename to reclaim (on the open file description, so it survives the rename); an isolated child inherits it (`pass_fds`), so the lock outlives a parent crash.
- I5. Reclaim is descriptor-relative (A-4 `_rmtree_fd`, `_output.py:80`). A failure is reported as `Disposition.RETAINED`, never as success.
- I6. Validation never mutates (best-practices 2.1): the descriptor checks raise and change nothing. Reports are frozen dataclasses (2.3).

## 3. Census on `0173c054` (replaces revision 1's C00-C22 and the 2A.2 anchors)

From a `git grep` of temp, stage and DuckDB producers under `src/decoy_engine` (command in `937e88fe` section 3) and a read of each hit and path consumer. A reviewed census, not a completeness proof; G5's ratchet covers misses.

| Row | Producer on main | Today | V1 change | Slice |
|---|---|---|---|---|
| T01 | OOC root, `out_of_core/_scratch.py:17-27`; subtrees `_runner.py:244`, `:268-270`; cleanup `:323-335` | `mkdtemp` (0700); subtrees umask `Path.mkdir`; caller `temp_dir` chmodded; `rmtree` by path | `scratch_root` returns a `ScratchArena`; subtrees `arena.create_dir`; cleanup `arena.reclaim()`. A caller `temp_dir` gets a fresh child, created first if missing as main does (`:25`, ambient only). | S2a-1 |
| T02 | Staged keys, `_stage.py:66-70` | `mkdir(parents)` plus `pq.ParquetWriter(path)` (umask mode). Reads via `ScratchAccess` (`_scratch.py:30`). | `fileaccess.adapters.arrow.parquet_writer(arena_dir, component, schema)` (0600). `source()` builds `LazySource(component, access=arena.reader())`, a relative locator (4.1); no path release. | S2a-1 |
| T03 | Relation files, `_relation.py:457`, `:466`, `:517-529`; stream-lane builds `_emit.py:137`, `:150` | DuckDB `COPY TO` and `read_parquet` by path | Directory from `arena.create_dir`. `COPY` and `read_parquet` paths come from `local_path` (4.1, L1). `ParentKeyRelation.path` (`_relation.py:53`) carries one to `_join.py:120`, `_batch_join.py:162`, `_stream_join.py:553`. Native calls on them go through `scratch_native`. `_emit.py` serves both lanes (`_runner.py:505`, `_stream_driver.py:454`). | S2a-1 (joins S2a-2) |
| T04 | Merged relation, `_multi_parent.py:135-164` | Same as T03 | Same as T03 | S2a-1 |
| T05 | DuckDB connections, `_duckdb.py:42`, `:60`; `_batch_join.py:159`; `_join.py`; `_stream_join.py:530` | `temp_directory=str(path)` via `connect_internal` | `connect_duckdb` takes an arena and passes `local_path()` of a child as `temp_directory` (transition: section 10) | S2a-1 (`_batch_join.py`, `_join.py` S2a-2; `_stream_join.py` S2b) |
| T06 | IPC spools, `_payload_store.py:94-96`, `:146-150`, `:206`; `_stream_driver.py:257`, `:277`; `_stream_join.py:244` | `pa.ipc.new_stream(str(path))` (umask) | `adapters.arrow.ipc_new_stream` (0600); reads via a new bounded `ipc_iter_stream` | S2b |
| T07 | Sorter runs, `_external_sort.py:214`, `:381`, `:421`, `:511`; reader `_external_sort_run.py:37`; reorder `_stream_join.py:498` | `pa.OSFile(str(path), "wb")` (umask) | `adapters.arrow.ipc_new_file` (0600); readers via the arena | S2b |
| T08 | Quality pairs, `execution/native/_pool_quality.py:245-246` (no production caller of `measure_pool_quality`; tests only) | Parquet in a `connect_duckdb` 0700 dir; `read_parquet(str(pairs_path))` (`:245-249`) | Arena-issued component and directory; the pairs path is a listed release, its query through `scratch_native` | S4b |
| T09, T10, T14, T15, T16 | Publication: `_transactional_sink.py:200`, `:358`; `_chunked_output_sink.py:463-477`; `subset/_materialize.py:51`, `:60`; `quarantine.py:313`, `:336`, `:376`; `profile/_fixed_width_writer.py:313-319` | A-4 `OutputDir.begin` (dir or file stages) | None; T-A10 covers them | S4a (tests only) |
| T11 | Isolated work root, raw sources and payload, `_isolated_run.py:364-371`, `:406-411`, `:422-434` | `mkdtemp` or a caller `staging_dir` as is; umask `sources/` and parquet; payload umask mode, then chmodded | Work root is an arena (a fresh child under a caller `staging_dir`); `sources/` via `create_dir`, files via `adapters.arrow.write_table`, payload via `adapters.serial.write_text` (0600 at creation). The child gets `local_path` strings (`:446`) and the arena descriptor via `pass_fds`. | S3 |
| T12 | Isolated staging output, `_isolated_commit.py:89-100`; worker `_isolated_worker.py:129-133` | No `output_dir`: `work_root / "staging_output"`; else `sibling_staging_dir` beside it (one same-filesystem rename) | No `output_dir` only: `arena.create_dir("staging_output")`; else unchanged (L8; the arena would give `EXDEV`). | S3 |
| T13 | Snapshot spill and stage, `postgres_snapshot.py:217-218`, `:455-459`, `:536-539` | Spill `mkdir` then chmod; stage `mkdir(0o700)`; ambient only (`:447`) | Spill is an arena under the caller's `temp_directory`; stage unchanged | S3 |
| T17 | Discovery, `data_discovery.py:181-213`, `fileaccess/adapters/duckdb.py:44-61` | In-memory, no `temp_directory` | Probe `T-DISCOVERY-SPILL` decides (4.3) | S3 |

Not scratch: vault (`vault.py:341-348`) and model pack (`profiling/model_pack/_packio.py:54-59`), both A-4 output; cloud readers; rotating logs.

### 3.1 Enumerations

Anchors below are on main `0173c054`.

#### 3.1.1 Exit paths of an arena

Each gets a case in `T-ARENA-EXITS` (S1a; the adopted case in `test_adopted_arena_is_non_owning`, S2a-1).

- Success then `reclaim()`.
- Exception in the `with` block: `reclaim()`, primary error re-raised; a failed `reclaim()` is `CleanupReport(RETAINED)` without masking it.
- Double `reclaim()` is a no-op.
- Garbage collection without `reclaim()` runs a `weakref.finalize` reclaim (A-4 precedent, `src/decoy_engine/fileaccess/_output.py:229`), for an owning arena only.
- An adopted arena (`_adopt`, transitional to S4b, section 10) owns nothing: garbage collection and scope exit close its descriptor and delete nothing; `reclaim()` on it raises.
- Kill: the arena stays, lock released, for S5; a kill inside `open` leaves an empty pending directory (L4).
- Isolated child exit leaves the work root to the parent's `reclaim()` (`src/decoy_engine/execution/_isolated_run.py:364`).

#### 3.1.2 Routes

T-PLAIN and T-MODES cells, each under ambient and a strict `RootedFileAccess`; routes A-4 refuses under strict assert the refusal.

- OOC batch and reorder lanes; native companion present and absent; sink whole-table and streamed.
- Isolated subprocess (also with `output_dir` in another directory and on another device, T12) and in-process fallback.
- Postgres snapshot (ambient only, `src/decoy_engine/connectors/postgres_snapshot.py:447`; stub connection), discovery, subset then mask, quarantine (`src/decoy_engine/quarantine.py:313`), fixed width.

#### 3.1.3 FK shapes

Through both OOC lanes. The parity suites (for example `tests/parity/test_out_of_core_fk_parity.py:448`, `:763`) run unchanged before and after S2a-1 to S2b as the class 9 characterization. Class 5 is N/A because no row selection changes.

- Single, composite, self-referencing.
- Multi-parent, cross-batch.

#### 3.1.4 Fields of public result types touched

No path, name or identifier fields, so G6 holds by field set.

- New frozen `QuotaReport(route, dimension, effective_bytes_cap, observed_bytes, disposition)` and `CleanupReport(disposition, retained_bytes, retained_inodes)`.
- New frozen `SweepReport(scanned, reaped, retained_live, retained_unknown, failed)`.
- Existing error types gain one optional `report` attribute.
- `IsolatedRunResult` (`src/decoy_engine/execution/_isolated_common.py:321`), sink results and entry-point signatures unchanged.

#### 3.1.5 Output surfaces

T-REPORT-TEXT seeds sentinels (a path, a table name, a cell value, a DSN) and scans `str` and `repr` of each report and carrying error; `test_native_failure_text_is_value_free` does the same for native failures on released paths.

- User-visible `Job.log` and API errors (via `str(exc)`); CLI stderr.
- Output and quarantine files (unchanged); scratch files (G1).

## 4. Design: the arena is an A-4 capability

### 4.1 Type and placement

New `src/decoy_engine/fileaccess/_scratch.py` (about 330 lines, with `ScratchAccess` moved in). Pattern: `cap-std` `Dir` and WASI preopens (as A-4 cites, `_types.py:3-6`) plus `tempfile.mkdtemp`.

```text
ScratchArena(StageDir)                # A-4 protocol, _types.py:179
  display = "engine.scratch"; strict = True   # constants, as ScratchAccess handles (_rooted.py:86-90)
  open(*, under: Path | None = None) -> ScratchArena    # under != None: require_ambient first;
                                       # records here whether the arena may release a path
  create_file(component) -> int        # O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0o600, dir_fd
  open_file(component) -> int
  create_dir(component) -> ScratchArena (child, shares root lock and finished state)
  remove(component), rename(src, dst)  # one dir_fd-relative call each
  local_path(component: str | None = None) -> str   # the only path release (rule below)
  local_stage_path() -> str            # raises, as A-4's _StageDir does (not a sink stage)
  reader() -> ScratchAccess            # read-only RootedFileAccess, moved here from out_of_core/_scratch.py:30
  reclaim() -> CleanupReport
  _adopt(path) -> ScratchArena         # transitional, non-owning (section 10)
scratch_native(fn, *args, **kwargs)    # the value-free native boundary (below)
```

With `display` constant, a scrubbed library failure reads `FileAccessReadError("engine.scratch")` (`guarded`, `_errors.py:142-163`); `ENOSPC` and `EDQUOT` keep their class for S4a. `strict` is `True` under the ambient policy too, which changes the CLI error class (section 8).

`ScratchAccess` and its issuer token (`out_of_core/_scratch.py:13`, `:30-61`) move into `fileaccess/_scratch.py` in S1a (no `execution` import from `fileaccess`); `arena.reader()` is its only issuer, and `scratch_access_for` goes in S2a-1 with its one caller (`_stage.py:70`). Its `select` resolves a relative locator against its descriptor (`out_of_core/_scratch.py:48-56`).

Built over A-4 internals (section 7), not copied. The A-4 writers (`adapters/arrow.py:72-126`, `serial.write_text`) use only `create_file`, `open_file`, `display` and `strict`; S1a widens their annotation to `StageDir`, so no new writer code.

Naming. `ARENA_PREFIX = "decoy-arena-v1-"` is defined once, in `fileaccess/_scratch.py`. `open` makes the root with `mkdtemp(prefix="decoy-arena-pending-")`, opens and checks it (4.2), takes `LOCK_SH`, then renames it by `dir_fd` to `ARENA_PREFIX + secrets.token_hex(8)`; a failed rename retries with a new token. No sweep matches the pending name; `under=` arenas are never swept (L4). Sentry: `test_arena_prefix_used_only_by_open`. A directory rename replaces an existing empty directory of the target name on Linux; with a 64-bit token a collision is a deliberate same-UID act (L6), and the sticky temp root refuses it across UIDs.

Path release. `local_path` is the only method that returns a path. Policy rule, fixed at `open` (an `under=` arena records "ambient only", an engine-rooted one "any"), not re-derived per call: an `under=` arena releases only under the ambient policy and otherwise raises `FileAccessUnavailable` (as `_local.py:174`); an engine-rooted arena releases under any policy (section 0). Callers, exactly: `execution/_isolated_run.py` (child arguments at `:446`), `execution/_isolated_commit.py` (only if S3 moves the payload writer there), `execution/out_of_core/_relation.py` and `_multi_parent.py` (`COPY` targets, `read_parquet`), `out_of_core/_duckdb.py` (`temp_directory`), `execution/native/_pool_quality.py` (pairs file, `:245-249`, S4b), `connectors/postgres_snapshot.py` (spill arena as `temp_directory`, `:216-224`, S3), `fileaccess/adapters/duckdb.py` (only if T17 picks the arena fix), and `_payload_store.py` and `_external_sort.py` only if Q5's fallback keeps path writers. `_stage.py` needs none (relative locator above). The list is a new `scratch_path_release` key in `tests/sentry/file_access_modules.toml`, enforced both ways by the boundary sentry. Join modules read `ParentKeyRelation.path` (released in `_relation.py`). Released paths are L1. A transitional `_runner.py` entry lives from S2a-1 to S2b (section 10).

Native boundary (V1-A). `scratch_native(fn, *args, **kwargs)`, built on A-4 `guarded("engine.scratch", True, ...)` (`_errors.py:142`), runs one native call under every policy: a resource `OSError` keeps its class, rebuilt errno-only (`_resurface`, `_errors.py:128`); `MemoryError` and `duckdb.OutOfMemoryException` are rebuilt as the same type with fixed text, so `is_memory_failure` (`execution/_isolated_common.py:186`) still matches by type; any other `Exception` becomes `FileAccessReadError("engine.scratch")` raised `from None`. Reader batch pulls use `guarded_iter` (`_errors.py:172`) the same way. It wraps every connection call and batch pull on a connection that uses a released path. Its callers are a second key, `scratch_native`; a module joins either key in the slice that converts it: `_relation.py`, `_multi_parent.py`, `_batch_join.py`, `_join.py`, `_stream_join.py`, `native/_pool_quality.py`; the sentry fails on a listed module that calls a connection method outside `scratch_native`. `postgres_snapshot.py` already raises code-only errors `from None` (`:38-39`, `:239-242`) and needs no entry.

### 4.2 Descriptor checks (the whole fail-closed default)

At open: `fstat` on the root descriptor must show `S_ISDIR`, `st_uid == geteuid()` and `S_IMODE == 0o700`. Otherwise `ScratchPathUnsafe` (code-only), nothing changed. At `create_dir` and `create_file`: `O_NOFOLLOW` plus `O_EXCL` or `mkdirat`, then `fstat` of the new descriptor for type, owner and mode. No quota or fscrypt is required (T-PLAIN).

### 4.3 Producers

T01-T08 and T11-T13 per section 3. T17: if `T-DISCOVERY-SPILL` shows DuckDB writes outside the arena for an in-memory untrusted session, `untrusted_session` sets `temp_directory` to `arena.create_dir("duckdb").local_path()` at connect, before `lock_configuration`, and its queries go through `scratch_native`. If no spill is possible with external access off, T17 records that as a tested fact, with no production change. The probe output goes into the S3 evidence.

### 4.4 Sweep (S5)

`fileaccess.sweep_scratch(root: Path | None = None) -> SweepReport`. Called by the CLI or worker only (Q7). For each entry of the scratch root, opened by descriptor:

1. Only names matching `^decoy-arena-v1-[0-9a-f]{16}$` (4.1); never `decoy-arena-pending-*`; the platform's `decoy-job-*`, `decoy-preflight-*`, `decoy-src-fd-*` (decoy-platform `api/jobs/v2_sequential.py:394`, `preflight_dispatch.py:249`, `dispatch_worker_main.py:268`); the engine's `decoy-ooc-*`, `decoy-isolated-run-*`; pre-S5 directories. A matching entry that is not a directory (`O_NOFOLLOW`), not owned by `geteuid()`, or not mode 0700 is `retained_unknown` and never deleted.
2. Try `flock(LOCK_EX | LOCK_NB)` on the entry's own descriptor. `EWOULDBLOCK` means a holder is alive: `retained_live`.
3. With the exclusive lock held, `_rmtree_fd` the contents, then `rmdir` the entry by name under the root descriptor. A failure counts as `failed` and is left for the next run (idempotent).

Pattern: Linux `flock(2)` on the open file description, inherited across `fork` and `exec`; no fencing (no resume, Q3). Pending-then-rename (4.1) closes the `mkdtemp`-to-`LOCK_SH` window; the lock-to-`rmdir` window is L6 (V2-LOG).

### 4.5 Not carried from `feat/f25-protected-scratch` (pre-GA hard delete, best-practices 8.1 and 8.2)

Unchanged from revision 3 (`98a72bb6` 4.5): only `scratch/_reports.py` (as `fileaccess/_scratch_reports.py`, trimmed) and `ScratchPathUnsafe` are ported; the capability, seam, ledger, authority, provider and sentry files are not. The F25 branch stays as reference (Q6).

## 5. Acceptance tests, red first

Each test is recorded red before the change, against its neutered control or main at the fail-before location (an `ImportError` is not a red); a test green by design proves non-vacuity by its mutation. Baselines from `uv.lock` in the branch environment.

| Test (file::name) | Scenario and observable result | Fail-before location (red reason) | Mutation that must turn it red | Slice |
|---|---|---|---|---|
| `tests/fileaccess/test_scratch_arena.py::test_modes_under_umask[000,002,022]` (T-ARENA-DESC) | Nested dirs 0700, files 0600, owner euid | Control with A-4's `0o666` (`_output.py:222`) shows 0644/0666 | `0o600` changed to `0o644` | S1a |
| `...::test_refuses_unsafe_root[symlink,wrong_mode,not_dir]` | Symlink, 0755 or file root: `ScratchPathUnsafe`, disk unchanged | Control without the `fstat` checks opens it | Delete the mode comparison | S1a |
| `...::test_exit_paths` (T-ARENA-EXITS) | The 3.1.1 exits; primary error kept; failed reclaim is `RETAINED` | Control swallowing errors reports `REMOVED` | Return `REMOVED` on `OSError` | S1a |
| `...::test_arena_is_stagedir` | `isinstance(arena, StageDir)`; unmodified `parquet_writer(arena, ...)` and `write_text` write 0600 files | Red (mypy) before the annotations widen | Drop the `strict` attribute | S1a |
| `tests/fileaccess/test_scratch_plain_host.py::test_jobs_succeed_without_quota_or_fscrypt` (T-PLAIN) | On the CI filesystem (no `prjquota`, no fscrypt): OOC both lanes with forced spill, subset then mask, hold-back sink succeed with `0173c054`'s bytes. `[error_path]`: an injected non-resource pyarrow failure in an arena writer under ambient raises `FileAccessReadError("engine.scratch")` (8) | Control whose `open` requires a project quota (revision 1) fails | Add a quota-qualification precondition to `open`; for `[error_path]`, set `strict` from the policy | S1a, re-run S2a-1, S2a-2, S2b, S3, S5 |
| `...::test_report_text_is_value_free` (T-REPORT-TEXT) | Sentinels absent from `str`/`repr` of reports and errors | A `path` field leaks | Add `detail=str(exc)` | S1a, S4a, S5 |
| `tests/fileaccess/test_scratch_native.py::test_native_failure_text_is_value_free[relation_copy,merged_copy,batch_join_read,join_read,stream_join_read,spill,pool_quality]` | Seeded DuckDB failures, sentinel table and column names: `COPY` into a removed arena child, `read_parquet` of a truncated relation, spill into a read-only `temp_directory`. Each cell asserts the library message holds the arena path and a sentinel (non-vacuity), then outward `str` and `repr` hold neither, ambient and strict | Main re-raises the `duckdb` error with the path (`_relation.py:517`) | Call `conn.execute` bare at one site (sentry also red) | S1a helper; cells S2a-1, S2a-2, S2b, S4b |
| `tests/fileaccess/test_scratch_arena.py::test_local_path_policy` | `under=`: strict raises `FileAccessUnavailable`, ambient returns; engine-rooted returns under both | Refusing every strict release fails the `out_of_core_fk` golden (`test_policy_routes.py:69`) | Drop the `under=` ambient check; re-derive the rule per call from a mutable field | S1a |
| `tests/sentry/test_file_access_boundary.py::test_local_path_callers_are_allowlisted` | Unlisted `local_path` caller, listed module with no call, bare connection call in a `scratch_native` module: each fails; `fa_fixtures` evasions (alias, `getattr`, bound method) caught | Fixture caller passes without the rule | Add a `local_path` call to `_stage.py` | S1b |
| `tests/sentry/test_scratch_arena_prefix.py::test_arena_prefix_used_only_by_open` | No `decoy-arena-` literal or `prefix=` outside `fileaccess/_scratch.py`; `open`'s name matches the sweep pattern | Seeded fixture passes without it | Add `prefix="decoy-arena-v1-x"` to `_isolated_run.py:367` | S1b |
| `tests/execution/out_of_core/test_scratch_modes.py::test_ooc_scratch_modes[batch,reorder]` (T-MODES, T01-T05) | OOC under umask 000, `os.mkdir`/`os.open` wrappers plus a pre-reclaim `fwalk`: dirs 0700, Python files 0600. At the S2a-1 head both lanes complete a job with an outgoing FK edge (`_emit.py`) | `_runner.py:244`, `:268-270`, `_stage.py:66` make `0777` subtrees | Restore `Path.mkdir(parents=True)` in `_stage.py` | S2a-1, S2b |
| `...::test_caller_temp_dir[mode_untouched,missing_created]` | Caller `temp_dir` 0755 stays 0755 with a 0700 arena child; a missing one is created (ambient) | `_scratch.py:26` chmods it; `missing_created` green by design | Restore the chmod; drop the `mkdir(parents=True)` | S2a-1 |
| `...::test_adopted_arena_is_non_owning[reorder_spill,relation_survives_gc,reclaim_refused]` | At the S2a-1 head the reorder lane runs with forced spill into the missing leaf `_stream_join.py:530` names (made 0700, 4.2); a relation built through the `Path` arm survives `del` plus `gc.collect()` of its adopted arena and the child join reads it; adopted `reclaim()` raises | Revision 3's open-only `_adopt` raises; a finalizer-carrying one deletes `*_key_relation.parquet` | Register the reclaim finalizer in `_adopt`; drop its `mkdir` | S2a-1 |
| `...::test_ooc_internals_take_only_an_arena[emit,relation,stream_table,connect_duckdb]` | `emit_to_sink`, `MaskedKeyStager`, relation builders, `stream_table` (S2b) and `connect_duckdb` (S4b) reject a `Path`; `_adopt` and arm (a)'s entry gone | Red at S2a-1 by design (arms, section 10) | Restore a `Path` arm | S2b, S4b |
| `...::test_spool_and_sorter_modes` (T-MODES, T06-T08) | Spools and sorter runs (S2b), quality pairs (S4b) are 0600 under umask 000; bounded RSS unchanged | `_payload_store.py:96`, `_external_sort.py:381` use the umask | Restore `pa.OSFile(str(path), "wb")` | S2b, S4b |
| `tests/execution/test_isolated_payload_mode.py::test_payload_never_readable_by_others` (T-PAYLOAD) | Caller `staging_dir` 0755, umask 022: payload mode at first byte is 0600; work root and `sources/` 0700, each `sources/s*.parquet` 0600 | `_isolated_run.py:429` writes 0644 before `:434` chmods | Write then chmod again | S3 |
| `...::test_child_holds_arena_after_parent_death` | Parent killed while the child writes: a `LOCK_EX|LOCK_NB` probe fails | Without `pass_fds` the probe succeeds | Drop the descriptor from `pass_fds` | S3 |
| `tests/execution/test_isolated_payload_mode.py::test_output_dir_commit_keeps_sibling_staging[other_dir,other_device]` | `output_dir` in another directory, and under `/dev/shm`: committed, staged beside `output_dir`. `other_device` first asserts `st_dev(output_dir.parent) != st_dev(tempfile.gettempdir())`; never skipped. Supported host: a tmpfs `/dev/shm` apart from the temp dir, as on `ubuntu-latest` (`.github/workflows/ci.yml:29`); elsewhere the precondition fails by name | Green on main by design | Stage `output_dir` jobs in the arena (`EXDEV`) | S3 |
| `tests/connectors/test_snapshot_spill_mode.py::test_spill_dir_created_0700` | Spill dir 0700 at creation, umask 022 (stub connection) | `postgres_snapshot.py:217` creates 0755 then chmods | Restore `mkdir` then `chmod` | S3 |
| `tests/test_discovery_spill.py::test_discovery_spill_stays_in_arena` (T-DISCOVERY-SPILL) | Discovery query past `memory_limit`: no `.tmp` in the working directory; spill only under the arena | Unknown before the probe; green on main is recorded as a fact | Remove `temp_directory` (arena fix only) | S3 |
| `tests/execution/out_of_core/test_scratch_quota.py::test_budget_exceeded_commits_nothing` (T-A02a) | Tiny `temp_disk_budget_bytes`: `out_of_core_temp_disk_exceeded` with a `QuotaReport`; no final output | Red for `report` (absent); "no output" green by design | Commit the sink before the budget check | S4a |
| `...::test_enospc_commits_nothing[T02,T06,T07,T08,T11]` (T-A02b) | `ENOSPC` and `EDQUOT` injected per writer family through a faulting file object over the real descriptor (patched `adapters.arrow._writable`; `os.fdopen` in `adapters.serial` for the payload): the job fails, nothing is committed, `QuotaReport(dimension="bytes")` | Bare `OSError`, no report (injected, section 13) | Catch and continue the write | S4a (T08 cell S4b) |
| `tests/execution/test_crash_mid_spool.py::test_sigkill_mid_spool_commits_nothing` (T-A08) | A harness pause after the first spool batch, then SIGKILL: no final output; the arena remains, lock released | Green by design (A-4 stage) | Publish the stage before spooling ends | S4a |
| `tests/fileaccess/test_publication_crash_matrix.py::test_crash_points[T09,T15,T16]` (T-A10) | SIGKILL before stage fsync, after the publish rename, after the parent fsync: old or complete new output, never partial | Green on main by design | Write directly to the final leaf | S4a |
| `tests/fileaccess/test_scratch_sweep.py::test_live_arena_survives` (T-A07) | Holder paused mid-spool: a second process's sweep gives `retained_live`, bytes unchanged; two sweepers after the holder dies: one reaps, one reports; the 4.4 look-alikes (unlocked 0700 same-UID), `decoy-arena-v1-nothex`, a 0755 match, and a foreign-UID entry (root only) are never touched | A prefix-only `rmtree` sweep deletes it | Drop the `flock` probe | S5 |
| `...::test_sweep_in_open_window` | A sweep at barriers after `mkdtemp` and after `LOCK_SH` in `open` leaves the arena, which then works | Final-name-first control is reaped | Create the root directly under the final name | S5 |
| `...::test_reap_after_kill_and_resume_after_sweeper_crash` (T-A08-SWEEP) | Owner killed: sweep reaps; sweeper killed mid-delete: a second sweep finishes | No-retry control leaves a partial | Count a failed delete as reaped | S5 |
| Ratchet M-RATCHET | Add a `tempfile.mkdtemp` in `_relation.py`: A-4's boundary sentry fails | Existing; count drops per migrated site | Raise the registry count | S2a-1, S2a-2, S2b, S3 |

## 6. Class table (classes 1 to 10)

| # | Class | Applies | Closing structure | Test | Mutation |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: reports, errors and native failure text reach `Job.log` and the API | Field-free reports; code-only errors `from None`; `scratch_native` with a caller sentry | T-REPORT-TEXT, T-PAYLOAD, `test_native_failure_text_is_value_free` | add a `detail` field; call `conn.execute` bare |
| 2 | Cleanup on an exit path | Yes | `reclaim` on each exit (3.1.1), finalizer on owning arenas only, `RETAINED` on failure; sweep scoped to a name only `open` issues | T-ARENA-EXITS, T-A08, T-A08-SWEEP, T-A07, prefix sentry, `_adopt` cells | swallow a reclaim error; widen the sweep pattern to `decoy-`; give `_adopt` a finalizer |
| 3 | State and concurrency | Yes: sweep vs owner, two sweepers | `flock` on the open file description, inherited, taken before the rename | T-A07, child-holds test, `test_sweep_in_open_window` | drop `LOCK_SH` or `pass_fds`; create under the final name |
| 4 | Route parity and silent fallback | Yes | One required arena (I2), `require_ambient` on caller paths, no profile gate (G2) | T-PLAIN, T-MODES both lanes, strict-policy cells, `test_local_path_policy`, `output_dir` commit cells | add a quota precondition; restore a `None` default; stage `output_dir` jobs in the arena |
| 5 | Referential integrity | N/A: no row selection changes | Existing parity suites as characterization | FK parity suites | n/a |
| 6 | Untrusted producer | Partly: DuckDB files, discovery SQL | Native files in a 0700 arena; session config locked after `temp_directory` | T-DISCOVERY-SPILL | unlock or remove `temp_directory` |
| 7 | Check then use on a path | Yes, bounded | Descriptor checks; `dir_fd`-relative ops; paths only via `local_path` (rule, allowlist); L1, L6 named | T-ARENA-DESC, `test_local_path_callers_are_allowlisted` | delete a `fstat` comparison; add an unlisted caller |
| 8 | Weak tests and overclaimed evidence | Yes | "Not shown" column; red forms per test; injected failures labelled | all | n/a |
| 9 | Refactor drift | Yes: producer migration | Output parity per slice; changes limited to modes, the report and the error class (8); four named arms ended by a sentry; assertions kept | T-PLAIN, parity suites, `test_ooc_internals_take_only_an_arena` | reorder a write; restore a `Path` arm |
| 10 | Frozen contract drift | Yes | Signatures, reason registry (Q4) and A-4 `CONTRACT_VERSION` unchanged (additive) | signature snapshot, `test_file_access_entry_points.py` | rename an export |

## 7. Reuse inventory

- A-4 `StageDir` and `OutputTxn` (`_types.py:179-229`); `RootedOutputDir.begin` for T09-T16 publication (unchanged).
- A-4 `_output.py` helpers: `_call` (errno-only errors), `_close`, `_rmtree_fd`, `_DIR_FLAGS`, `_FILE_CREATE`; `_component.validate_component`.
- A-4 adapters `arrow.*` writers (`adapters/arrow.py:72-126`), `duckdb.connect_internal`, `untrusted_session` (`adapters/duckdb.py:44-68`).
- `ScratchAccess` (`out_of_core/_scratch.py:30-61`, moved into `fileaccess/_scratch.py`) for reads; A-4 `guarded` and `guarded_iter` (`_errors.py:142`, `:172`) under `scratch_native`.
- `fileaccess.require_ambient` (`_entry.py:115`) for caller-named roots.
- A-4 boundary sentry, registry and module roles (`tests/sentry/test_file_access_boundary.py`) as the G5 ratchet and both allowlists; `tests/sentry/test_module_size.py`.
- From `feat/f25-protected-scratch`: `scratch/_reports.py`, trimmed.
- Existing reason `out_of_core_temp_disk_exceeded` (`reasons/runtime/_part2.py:150`) and `check_temp_disk_budget` (`out_of_core/_budget.py:525`).

## 8. Frozen contracts touched

- `fileaccess` public surface: additive exports `ScratchArena`, `sweep_scratch`, `QuotaReport`, `CleanupReport`, `SweepReport`, `ScratchPathUnsafe`. `CONTRACT_VERSION` 1, `ENTRY_POINTS` unchanged.
- Public entry-point signatures: unchanged. `run_fk_out_of_core(temp_dir=...)`, `run_pipeline_isolated(staging_dir=...)` and the snapshot `temp_directory` keep their meaning, except that the engine makes a private child inside a caller directory instead of using or chmodding it; `output_dir` jobs still stage beside it (T12). CHANGELOG entry in S2a-1 and S3.
- Error class (pre-GA, stated): because the arena's `strict` is constant, a non-resource library failure on scratch now raises `FileAccessReadError("engine.scratch")` under the ambient (CLI) policy too, where main raised the raw pyarrow or DuckDB error with its text (`_errors.py:142-163`). `ENOSPC`, `EDQUOT` and memory failures keep their class. CHANGELOG entry in S1a; T-PLAIN `[error_path]`.
- Reason registry (Q4) and output bytes (T-PLAIN) unchanged.

## 9. Owner questions (recommended defaults first)

- Q1. At-rest encryption: **not in V1 (owner's answer).**
- Q2. Kernel project quotas: **an optional V2 profile (recommended);** V1's bound is the OOC budget plus `ENOSPC`/`EDQUOT`, as a `QuotaReport`.
- Q3. SQLite `ResourceLedger` and `OwnershipAuthority`: **not carried (recommended);** the sweep's authority is the `flock` plus the name, owner and mode checks.
- Q4. Reason codes: **no new code (recommended).** Existing errors gain an optional `report`; no registry bump to collide with A-4's 1.2.0 and B-6.
- Q5. fd-backed IPC writer budget: **median regression at most 5% on the sorter-merge and payload-store benchmarks, same host, five runs, fixed in S2b-0 (recommended).** If missed, T06 and T07 keep path writers (L1 widened).
- Q6. Build base: **a fresh `feat/f25-v1-scratch` from `origin/main` (recommended).**
- Q7. Sweep invocation: **an exported `sweep_scratch` the CLI or worker calls, never on import or in the background (recommended).**
- Q8. Caller-named roots: **ambient-only as on main; a fresh 0700 child inside, the caller directory's mode untouched (recommended).**

## 10. Slices

Bound: each slice changes at most 10 production files and 800 lines (production, test and doc). Estimates scale the `feat:` diffstat (`git diff --stat 33ef629b 4c901d38`) plus the migration below; evidence records the actual diffstat, and a slice over the bound is split before review, not waived.

| Slice | Unit | Production files | Est. changed lines (prod + tests + docs) | Content | Gate |
|---|---|---|---|---|---|
| S1a | 1. Port onto A-4 | 6: new `fileaccess/_scratch.py`, new `_scratch_reports.py`, `fileaccess/__init__.py`, `adapters/arrow.py` and `serial.py` (annotations), `out_of_core/_scratch.py` (`ScratchAccess` moved out) | about 760 (about 420 + 300 + 40 docs and CHANGELOG) | Arena, naming, release rule, `scratch_native`, `_adopt`; their section 5 tests | Dennis, then Codex |
| S1b | 1. Sentries | 0 (`tests/sentry` and `file_access_modules.toml` keys `scratch_path_release`, `scratch_native`) | about 180 | The two sentries, seeded evasions | Codex |
| S2a-1 | 2. Thread the arena (batch lane, stager, relations, T01-T05) | 7: `out_of_core/_scratch.py`, `_runner.py`, `_stage.py`, `_emit.py`, `_relation.py`, `_multi_parent.py`, `_duckdb.py` | about 750 (about 250 + 350 new tests + 150 migration) | Arms (a) to (d); T-MODES (FK edge in both lanes), caller `temp_dir`, `_adopt` and native cells, M-RATCHET. `_runner.py` capped at 644 (`test_module_size.py:120`): net zero; `_relation.py` (595): at most +5 | Codex |
| S2a-2 | 2. Thread the arena (batch joins) | 3: `_batch_join.py`, `_join.py`, `_runner.py` (joins take the arena; arm (a) narrows to `stream_table`) | about 260 (about 60 + 150 + 50 migration) | T-MODES and native cells for joins | Codex |
| S2b-0 | 2. Q5 budget first | 0 (benchmark only) | about 90 | Q5 medians on `main` | Codex |
| S2b | 2. Thread the arena (reorder lane, T06-T07) | 9: `_payload_store.py`, `_external_sort.py`, `_external_sort_run.py`, `_stream_driver.py`, `_stream_join.py`, `_emit.py` (ends arm (d)), `fileaccess/adapters/arrow.py` (`ipc_iter_stream`), `_runner.py` (ends arm (a)), `_relation.py` (ends arm (b)) | about 790 (about 230 + 450 + 110 migration) | fd-backed spools and runs; bounded RSS; Q5 check; native cell; arms ended. `_stream_join.py` (645, `test_module_size.py:124`): net zero | Codex |
| S3 | 2. Thread the arena (non-OOC, T11-T13, T17) | 6: `_isolated_run.py`, `_isolated_worker.py`, `_isolated_commit.py`, `connectors/postgres_snapshot.py`, `data_discovery.py`, `fileaccess/adapters/duckdb.py` | about 730 (about 230 + 500) | T-DISCOVERY-SPILL probe first; T-PAYLOAD, child-holds, `output_dir` and snapshot cells. `_isolated_run.py` (651, `test_module_size.py:71`): payload writer moves to `_isolated_commit.py`, net at most zero | Dennis (rule 3 fix), then Codex |
| S4a | 3. Quota report and crash behavior | 5: `out_of_core/_runner.py`, `out_of_core/_budget.py`, `fileaccess/_scratch_reports.py` (`QuotaReport`), `fileaccess/_scratch.py` (report on `ENOSPC`), `execution/_isolated_run.py` | about 640 (about 140 + 500) | T-A02a, T-A02b, T-A08, T-A10, T-REPORT-TEXT; `_isolated_run.py`, `_runner.py` net zero | Codex |
| S4b | 3. End of transition; T08 | 3: `out_of_core/_duckdb.py` (drops arm (c)), `fileaccess/_scratch.py` (deletes `_adopt`), `execution/native/_pool_quality.py` (T08) | about 300 (about 50 + 170 + 80 migration) | Last arm ended; T08 cells | Codex |
| S5 | 4. Live-arena sweep | 4: new `fileaccess/_scratch_sweep.py`, `fileaccess/_scratch.py` (lock helper), `fileaccess/_scratch_reports.py` (`SweepReport`), `fileaccess/__init__.py` | about 760 (about 260 + 500) | Sweep tests with real subprocesses; T-PLAIN; `docs/security/file-access-policy.md`, CHANGELOG | Dennis, then Codex |

Transition (I2 is suspended only here; four named arms, each with its end). (a) `_runner.py` passes `child.local_path()` to `stream_table` (`_stream_driver.py:124`, until S2b) and to the joiners (until S2a-2) under a transitional `scratch_path_release` entry. (b) `build_parent_key_relation_aligned` keeps a `Path` arm until S2b. (c) `connect_duckdb` keeps a `Path` arm until S4b (joiners until S2a-2, `_stream_join.py:530` until S2b, `_pool_quality.py:246` until S4b). (d) `emit_to_sink` and `MaskedKeyStager` accept a `Path` until S2b, because `_stream_driver.py:454` still passes `relation_dir` and `staging_path` until then. Every `Path` arm goes through `ScratchArena._adopt(path)`, specified exactly: create each missing component with `os.mkdir(..., 0o700)` (main's `connect_duckdb` makes the leaf with `mkdir(parents=True)` then `chmod 0o700`, `_duckdb.py:38-39`), open the leaf `O_DIRECTORY | O_NOFOLLOW`, `fchmod` it to 0700 only if this call created it, then run the 4.2 checks. An adopted arena is non-owning: no lock, no reclaim finalizer, its descriptor closes on scope exit and nothing is deleted; `reclaim()` raises; cleanup stays with the code that owns the directory today (`_runner.py:323-335`, `_stream_driver.py:553`).

Delivery unit (Codex V2-LOG). Every slice lands on `feat/f25-v1-scratch` (Q6). It merges to `main` once, after S4b with `test_ooc_internals_take_only_an_arena` green, so no arm reaches `main`; S5 may follow.

Existing-test migration (M2). `git grep -n "temp_dir=" -- tests` gives 214 lines in 35 files, including public `run_fk_out_of_core(temp_dir=...)` calls that stay; the internal-callee sites are about 83 in 18 files (Dennis; `tests/unit/execution/test_out_of_core_relation.py` 15, `test_out_of_core_join_chunked.py` 10, `_ooc_fixtures.py` 5). Each moves in the slice that removes its callee's `Path` form: relation, merged-relation and fixture sites in S2a-1, join sites in S2a-2, stream-driver and reorder sites in S2b, `connect_duckdb` and pool-quality sites in S4b. Edits are mechanical (a fixture builds the arena), assertions unchanged and shown in evidence, except `tests/native/test_pool_quality.py:314`, whose `temp_directory == str(tmp_path)` becomes equality with the arena child's `local_path()` (S4b evidence).

Order: S1a, S1b, S2a-1, S2a-2, S2b-0, S2b, S3, S4a, S4b, S5. S3 needs only S1a and S1b and may run beside S2a-1 to S2b, one writer per worktree. S4a needs S2b and S3 (T-A02b runs the T02-T11 writers). S5 needs S1a and S3 (the inherited lock).

Units: rebase onto A-4 (S1a, S1b), one required arena (S2a-1 to S3), 0700/0600 plus the quota report (S1a, S4a, S4b), the sweep (S5). Four units in ten slices, about 5,260 changed lines (revision 3: 4,520; the rise is `scratch_native`, `_emit.py`, `_adopt` cells and about 390 lines of test migration).

Loop per slice: red first; baselines from `uv.lock`; each new mechanism mutated once, never committed mutated; lint, format, mypy, touched suites, `tests/sentry`, `tests/fileaccess`.

## 11. V2-LOG (listed, not planned) and deferred features

V2-LOG, with defect class. Revision 2's items stand as listed in `937e88fe` section 11. Live in this plan:

- [7] Same-UID interference in the sweep window between `flock` and `rmdir` (L6): `fileaccess/_scratch_sweep.py` (new, S5).
- [7] Native and released-path file modes inside the arena (L1): `out_of_core/_relation.py:517`, `_multi_parent.py:147`, `execution/_isolated_worker.py:133`.
- [2] An empty `decoy-arena-pending-*` directory left by a crash inside `open` is never reaped (L4): `fileaccess/_scratch.py` (new, S1a).
- [7] `sweep_scratch(root=...)` can be aimed at a caller directory; an admin-only call: `fileaccess/_scratch_sweep.py` (new, S5).

Deferred features (not defects): fscrypt (Q1), the kernel project-quota provider (Q2), the ledger and ownership authority (Q3), checkpoint and resume (umbrella slice 6), platform lock-authority integration (umbrella section 4).

## 12. Pre-mortem: likely gate findings and the red-first test for each

1. "The sweep deletes a platform job's stage root." Test: T-A07 look-alikes, the prefix sentry.
2. "A strict-policy OOC job breaks on `local_path`." Test: `test_local_path_policy`, the strict route golden.
3. "`output_dir` on another device fails the commit." Test: the `output_dir` commit cells.
4. "A strict job follows a caller path, or a plain host breaks." Test: strict-policy cells in T-MODES (strace helper `tests/fileaccess/strace_support.py`), T-PLAIN in each slice.
5. "A DuckDB failure prints the arena path into `Job.log`", or "a slice head breaks a lane it did not touch." `scratch_native`; named arms and a non-owning `_adopt`. Tests: `test_native_failure_text_is_value_free`, T-MODES both lanes at S2a-1, `test_adopted_arena_is_non_owning`.

## 13. What this revision did not run or verify

- `plan-lint plan` (decoy-platform `handoffs/queue`) printed OK. Nothing else was run: no test exists, no red or green is claimed, line counts are estimates.
- `/home/cam/dev-rules/*` is not mounted in this container and was not read.
- Platform anchors (4.4) are from revision 3, not re-read. The 83-site count is Dennis's; only the broad grep (214 lines, 35 files) was reproduced.
- `/dev/shm` and `/tmp` differ in `st_dev` in this container; not checked on the `ubuntu-latest` runner.
- The isolated child's own failure text over released paths was raised by neither gate and was not re-checked.
- `T-DISCOVERY-SPILL`'s premise is unverified; the snapshot cell uses a stub; T-A02b's `ENOSPC`/`EDQUOT` are injected.
- `flock` on overlayfs and across a directory rename is from `flock(2)`, not tested; NFS is unsupported (L7).

## 14. Provenance

- Revisions: 1 `affde556`, 2 `937e88fe`, 3 `98a72bb6`; revision 4 answers the rev-3 gates in section 0.
- Next gates: Dennis on this document, then Codex. No build before owner approval.

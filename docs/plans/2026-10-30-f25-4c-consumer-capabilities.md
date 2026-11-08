# F25 V1 scratch on A-4: one arena capability, permissions by default, report and sweep

Status: plan, revision 8: a re-base on engine `main` `a5eb66e2` (F06 merged as #338, LIB L0.1 as #403) that fixes Dennis r5's V1 at its root; section 0 maps it. No code is on this branch; no build is authorized.

Summary: engine scratch is created 0700 (directories) and 0600 (engine files) under any umask, 0777 included, through one arena capability, an A-4 `StageDir` whose every directory comes from L0.1's allocator (`fileaccess/_protected_dir.py`) and every file from A-4's one descriptor-relative private creator (`_output.create_private_at`). A job out of scratch space stops with a value-free report and commits nothing. A sweep removes abandoned arenas it can prove it created, never live ones.

Rules consulted: `/home/cam/dev-rules/README.md`, `00-universal.md`, `development-loop.md` (PLAN: choke point, Placement, Design notes, Established solutions), `architecture.md` (layout), `reuse-first.md`, `destructive-operations.md` (the sweep), `risk-and-exceptions.md`, re-read for this revision; the V1 bar (`v1-bar.md`: rules 1 to 3; a second copy of a central mechanism always blocks); the engine `CLAUDE.md`; the code at each anchor on `main`.

Risk: R2 (plan only): scratch confidentiality, cleanup, and the OOC, declared-staging, subset, snapshot and spill failure paths. Not R3: the sweep deletes only marker-proven arenas under the one temp root (4.4), is caller-invoked, and rollback is a revert on a pre-GA engine (`release.py`).

Dependencies. Planned against engine `main` `a5eb66e2`. F06 is merged (`05e1c0c6`, #338), so revision 7's `f06:` anchors now read `main`: `git diff --stat 05e1c0c6 a5eb66e2` shows no change since F06 merged in `execution/out_of_core/`, `declared_casting/_stream.py`, `connectors/postgres_snapshot.py` or `native/_pool_quality.py`, and the anchors spot-checked there hold (`out_of_core/_scratch.py:35`, `:36`, `:56`, `:79`, `:105-118`; `declared_casting/_stream.py:200`, `:206`; `_runner.py:181`, `:294`, `:323`; `_budget.py:539`; `_duckdb.py:64`). Files that moved since F06 merged are re-anchored in section 0.2. F25 builds on two LIB parts: L0.1 (merged, `45553d7e`: the allocator, its live issued-root registry, `ProtectedDirUnsafe`/`scratch_unsafe`, G-ONE-PROTECTED-DIR) and L0.2 (planned, `feat/lib-l0-2-route-row-dirs`, about 360 lines, not started: routes `scratch_root`, `engine_scratch_root`, the isolated work root and the Postgres spill root through the allocator and deletes `_ENGINE_ROOTS`). Slices that touch those three modules start after L0.2 merges (section 10).

## 0. Revision 8: finding map

| Finding | Class | Change | Test |
|---|---|---|---|
| Dennis r5 V1 (rule 1). Arena `open`, `create_dir`, `create_file`, `_adopt` and the `under=` missing parent took umask-masked modes, then `fstat`-required 0700/0600: under umask 0777 every scratch job fails. Revisions 6 and 7 answered it site by site (a chmod at five sites, then the marker Codex found missed), which is the shape that let the marker slip. | 4 | Root fix: the arena creates nothing itself. Path-named directories (the root, each missing `under=` level) come from L0.1's allocator (`_protected_dir._create_level`); descriptor-relative directories and every private file come from A-4's capability module `_output.py`, whose two existing creators become the only ones (`make_private_dir_at`, `create_private_at`, 4.2) and gain the explicit mode and the `fstat` check. `fileaccess/_scratch.py` holds no `mkdir`, `mkdtemp`, `chmod` or `O_CREAT` (4.2). Revision 6 and 7's per-site chmods are deleted. | `test_modes_under_umask[...,777]`; `[create_choke_point]` now also bans `mkdir`/`mkdtemp`/`chmod`; G-ONE-PROTECTED-DIR (existing) |
| Dennis r1 (rev 8) V1-1: revision 8's new `_create_private` and `_create_level_at` copied A-4's descriptor-relative private creators (`_output.py:52`, `:160-161`, `:171`, `:228`, `:240-253`). V1-2: the win32 arm's `O_CREAT` broke the plan's own sentry. | 9 | One creator per kind, in the capability module A-4 already uses (4.2); the win32 file form sits beside L0.1's win32 directory form (4.5); the sentry text is exact on both platforms. | `test_private_creators.py`; `[create_choke_point]` |
| Re-base: L0.1 already owns directory allocation, the issued-root registry and the `scratch_unsafe` reason. Revision 7's own `mkdtemp`-plus-chmod root, its `ScratchPathUnsafe` and its release rule would be second copies (always blocking under the V1 bar). | 9 | Reuse, section 4.1: the allocator's allocation loop and parent rule (extracted, behaviour unchanged), its registry (an arena root registers there), `ProtectedDirUnsafe`. `ScratchPathUnsafe` is dropped. | `tests/fileaccess/protected_dir/test_protected_dir.py` green unchanged; `test_arena_root_is_an_issued_root` |
| Re-base: LIB L0.2 routes T01, T11 and T13's roots through the allocator. | 9 | F25 does not redo it: it starts after L0.2 merges (L0.2 also holds the D9 registry lane F25's first slices write), and S2a-1, S3a and S3b replace the routed root with an arena. | slice order (10) |
| Main moved: the isolated payload is already 0600 through `fileaccess.write_private` (`_isolated_run.py:432-436`). | 8 | T-PAYLOAD's fail-before moves to the sources files (`pq.write_table` at `:414`, umask mode). | T-PAYLOAD |
| Windows is in the CLI V1 matrix subject to WIN-FEAS (roadmap 5.5); L0.1 already has a Windows arm. | 4 | A small win32 arm (4.5): the allocator's DACL root, path operations beneath it, no `flock`, no sweep; verified on L0.1w's lane. | `test_win32_arena` (Windows lane) |
| F22 revision 9 Q1 defaults reclaim of its SIGKILL-left stages to F25, but F25 L4 never sweeps output-side stages. | 2 | Section 4.6: F25 does not take them; recommended owner answer Q10. | none (owner) |

### 0.1 What carries from revision 7

Everything not named above: the guarantee table, invariants, census rows, `scratch_native`, the marker, `flock`, the sweep, reports, budget hand-over and the test list. Revision 7's marker fix stands and is now a case of the root rule (the marker is a `create_private_at` file).

### 0.2 Anchors that moved since F06 merged

| Revision 7 anchor | On `main` `a5eb66e2` |
|---|---|
| `fileaccess/_output.py:80` `_rmtree_fd`; `:222` `0o666`; `:229` finalizer | `:82`; `:225`; `:232` |
| `fileaccess/_errors.py:172` `guarded_iter`; `:178` `is_memory_failure` | `guarded_iter` `:191`; `is_memory_failure` is `execution/_isolated_common.py:178`; `guarded` `:142`, `passes_by_type` `:100`, `resurface_scrubbed` `:113`, `_resurface` `:128`, `_RESOURCE_ERRNOS` `:27` unchanged |
| `fileaccess/_local.py:174` (`FileAccessUnavailable` on a path release) | `local_stage_path` `:268-271` |
| `_isolated_run.py:363-369`, `:406-411`, `:429-434`, `:420`, `:453` | work root `:366-372`; sources `:409-415`; payload `:425-436` (`write_private`); child command `:456`; `Popen` `:465` |
| `data_discovery.py:181-213`; `adapters/duckdb.py:44-61` | `data_discovery.py:205-249` (`untrusted_session` `:249`); `adapters/duckdb.py:45-58` |

## 1. Guarantee (V1 rules 1 to 3 only: a ratchet plus named limits)

| # | Claim | V1 rule | Shown by | Not shown |
|---|---|---|---|---|
| G1 | On section 3's rows (T01-T08, T11-T13, T17, T18), engine-created scratch directories are 0700 and engine-owned, and Python-created files 0600, at creation, under umask 000, 002, 022, 077, 777 (non-root). | 3, 1 | T-MODES, T-ARENA-DESC | Native writes (L1); later producers only via G5. |
| G2 | Without project quotas or fscrypt (overlayfs, tmpfs, plain ext4), OOC (declared and plain), subset, snapshot-shaped and forced-spill jobs succeed with F06's output. | 1 | T-PLAIN | An unwritable root still fails. |
| G3 | Over the OOC budget (copy and spill in one arena), or on a resource-errno `OSError` or errno-less native I/O failure in a scratch write, the job stops, the sink aborts, nothing commits, and the error carries a frozen `QuotaReport` (`"bytes"` or `"unknown"`). | 2, 1 | T-A02a, T-A02b, one-arena-one-budget | `"unknown"` names no limit (L3); no write cap without a kernel quota (L2); a DuckDB disk-full raised as `OutOfMemoryException` is classed memory, no report (V2-LOG). |
| G4 | A kill mid-spool commits nothing; a kill at a publication step leaves old or complete new output. | 2 | T-A08, T-A10 | Crash-left output stages stay (L4). |
| G5 | A new `tempfile.*`, DuckDB `temp_directory` or path-taking Arrow/Parquet writer outside `fileaccess/_scratch.py` and the adapters fails A-4's boundary sentry until reviewed. | ratchet | `test_file_access_boundary.py`, registry (205 entries on F06), M-RATCHET | Unmodelled calls (L5). |
| G6 | `str()`/`repr()` of the three reports, errors carrying them (the budget error included) and errors leaving `scratch_native` hold no path, table, column or cell value. | 3 | T-REPORT-TEXT, native-text test | Admin logs, tracebacks, `__cause__` (V2). |
| G7 | `sweep_scratch()` acts only on temp-root entries `open` created (exact name, marker token matching the name), never a held one; safe to re-run after a crash mid-delete. | 2, 1 | prefix sentry, T-A07, open-window test, T-A08-SWEEP, `test_sweep_takes_no_root` | Same-UID forgery (L6); cross-host (L7); foreign-UID cell root-only. |

Named limits: L1 DuckDB spill files and the isolated child's path writes take the umask mode inside the 0700 arena (`COPY` targets are pre-created 0600); L2 no byte cap without the quota profile; L3 errno-less failures report `"unknown"`; L4 never swept: output-directory stages, `under=` arenas, an empty pending directory from a crash in `open`; L5 the scanner model; L6 same-UID interference (forged marker, rename onto an empty arena name); L7 single-host `flock`; L8 publication stages keep the output mode (`_output.py:225`, `sibling_staging_dir`), and the ambient output stage (`_local.py:221` `mkdtemp`) is still born at the umask mode, so a directory output fails under umask 0777 with or without F25; T-MODES observes the scratch tree, not the sink; L9 on win32 (4.5) a killed run's arena is never reclaimed and check-then-use is path-based.

## 2. Invariants

- I1. No producer in section 3 creates a scratch directory or a Python-written scratch file except through a `ScratchArena` (`StageDir` protocol, `fileaccess/_types.py:179`).
- I2. Internal producers take `arena: ScratchArena` as a required argument: no default, no `None`, no `getattr` fallback. Public entry points create the arena themselves; an internal caller that already holds one hands it on (`_arena=`, 4.1).
- I3. An arena root is made by L0.1's allocator (in the temp root, or a fresh child of a caller directory, ambient only) under a pending name, checked as 4.2, marked, locked, renamed to its `decoy-arena-v1-` name (4.1), registered in the allocator's live issued-root set, and held by descriptor. The arena module creates no directory and no file except through `_protected_dir` and `_output`'s two creators (4.2).
- I4. `flock(LOCK_SH)` on the root descriptor from before the rename to reclaim; an isolated child inherits it (`pass_fds`).
- I5. Reclaim is descriptor-relative (A-4 `_rmtree_fd`, `_output.py:80`). A failure is reported as `Disposition.RETAINED`, never as success.
- I6. Validation never mutates: descriptor checks raise and change nothing. Reports are frozen dataclasses.

## 3. Census (F06 `19e73714`, re-checked on `main` `a5eb66e2`)

The `937e88fe` section 3 `git grep` re-run on F06, each hit read; a reviewed census, not a proof (G5 covers misses). The rows' modules are unchanged on `main` since F06 merged except `_isolated_run.py` and `data_discovery.py` (0.2). "F06" under Today means `main` already makes the site 0700/0600 (`out_of_core/_scratch.py:68-119`, `_duckdb.py:21`); the V1 change there is the arena, not the mode. "L0.2" means LIB L0.2 routes the root through the allocator before F25 touches it; F25 then replaces the routed root with an arena.

| Row | Producer on F06 | Today | V1 change | Slice |
|---|---|---|---|---|
| T01 | OOC root `scratch_root` (`out_of_core/_scratch.py:26-37`); subtrees `_runner.py:234`, `:258-259`, `_rewrite_pass.py:158`, `:265`; cleanup `_runner.py:323-327` | `mkdtemp`; caller `temp_dir` chmodded (`:36`); `_ENGINE_ROOTS` skips `require_ambient` (`:32`); subtrees F06; `rmtree` by path. After L0.2: allocator root, `_ENGINE_ROOTS` gone | Handed arena as root, or `open` (`under=temp_dir`: fresh child, ambient only); `create_dir`; owner's `reclaim()` | S2a-1 (subtrees S2a-1a) |
| T02 | Staged keys, `_stage.py:71-80` | F06; read via `scratch_access_for` (`:80`) | `parquet_writer(arena, component, schema)`; `LazySource(component, access=arena.reader())`; no release | S2a-1b |
| T03 | Relation files, `_relation.py:459-545`; `_emit.py:137`, `:150` | F06 `COPY` (`:520-521`); `read_parquet` by path | `create_dir`; `COPY` target `create_file(c)` then `local_path(c)`; `ParentKeyRelation.path` read at `_join.py:117-120`, `_batch_join.py:160-163`, `_stream_join.py:551-554`; native calls via `scratch_native` | S2a-1c (joins S2a-2) |
| T04 | Merged relation, `_multi_parent.py:135-172` | Same as T03 | Same as T03 | S2a-1c |
| T05 | DuckDB connections, `_duckdb.py:32-64` (`connect_internal` at `:64`); `_batch_join.py:154`; `_join.py:112`; `_relation.py:471`; `_multi_parent.py:147`; `_stream_join.py:530` | F06 `private_dir` then `chmod` (`:42-43`); `temp_directory=str(path)` | `connect_duckdb` takes an arena, passes `local_path()` of a child as `temp_directory`, and builds the connection under `scratch_native` | S2a-1c (joiners S2a-2, `_stream_join.py` S2b-2) |
| T06 | IPC spools, `_payload_store.py:97-100`, `:147-152`, `:207-213`; `_stream_driver.py:259`, `:279`; `_stream_join.py:244` | F06 `open_private` sink | `adapters.arrow.ipc_new_stream` (0600); reads via a bounded `ipc_iter_stream` | S2b-1 |
| T07 | Sorter runs, `_external_sort.py:215`, `:382`, `:422`, `:512`; reader `_external_sort_run.py:37`; reorder `_stream_join.py:498` | F06 `open_private`; reads by path | `adapters.arrow.ipc_new_file` (0600); readers via the arena | S2b-2 |
| T08 | Quality pairs, `native/_pool_quality.py:245-249` (tests only call `measure_pool_quality`) | Parquet in a `connect_duckdb` dir; `read_parquet(str(pairs_path))` (`:249`) | Arena component; the pairs path is a listed release; query through `scratch_native` | S4b |
| T09-T16 | Publication sites (`_transactional_sink.py:200`, `subset/_materialize.py:51`, `quarantine.py:313`, `profile/_fixed_width_writer.py:313` and peers) | A-4 `OutputDir.begin` | None; T-A10 covers them | S4a (tests) |
| T11 | Isolated work root, sources, payload, `_isolated_run.py:366-372`, `:409-415`, `:425-436`; child command `:456` | `mkdtemp` or caller `staging_dir` (L0.2: allocator); umask `sources/` and sources files (`pq.write_table`, `:414`); payload already 0600 (`write_private`, `:432-436`) | Arena (fresh child under a caller `staging_dir`); `create_dir`; sources via `adapters.arrow.write_table`; payload keeps `write_private` into the released path; child gets `local_path` strings and the descriptor (`pass_fds`) | S3a |
| T12 | Isolated staging output, `_isolated_commit.py:89-100`; `_isolated_worker.py:129-133` | `work_root / "staging_output"`, or `sibling_staging_dir` with `output_dir` | No `output_dir` only: `create_dir("staging_output")` (L8: the arena would give `EXDEV`) | S3a |
| T13 | Snapshot spill, `postgres_snapshot.py:217-218`; stage `:455-459` | Spill `mkdir` then chmod; ambient only (`:447`); L0.2: allocator | Spill an arena under `temp_directory`; stage unchanged | S3b |
| T17 | Discovery, `data_discovery.py:205-249`, `adapters/duckdb.py:45-58` | In-memory, no `temp_directory` | T-DISCOVERY-SPILL decides (4.3) | S3b |
| T18 | Declared staging, `declared_casting/_stream.py:110-141`, `:183-207` | `engine_scratch_root` (`out_of_core/_scratch.py:46-65`); F06 copies; budget on the root (`:127`, `:139`); root passed as `temp_dir` (`:206`) | `run_staged` opens the arena, stages via `parquet_writer(arena, ...)`, reads via `arena.reader()`, hands it on (`_arena=`, 4.1) | S2a-1 |

Not scratch: vault (`vault.py:341-348`), model pack (`profiling/model_pack/_packio.py:54-59`), cloud readers, logs. F06's local protected quarantine store (`f06:declared_casting/_quarantine_local.py:182-207`) is durable (receipts that outlive the run, docstring `:1-12`): never reclaimed or swept; ambient-only.

### 3.1 Enumerations

#### 3.1.1 Exit paths of an arena

Each gets a case in `T-ARENA-EXITS` (S1a; adopted and child cases in `test_adopted_arena_is_non_owning`, S2a-1b).

- Success then `reclaim()` by the opener (`run_staged` for a handed arena, never the route), marker last (4.4); a double `reclaim()` is a no-op.
- Exception in the `with` block: `reclaim()`, primary error re-raised; a failed `reclaim()` is `CleanupReport(RETAINED)`, not masking it.
- Garbage collection runs a `weakref.finalize` reclaim (`fileaccess/_output.py:232`) on the owning root only; `create_dir` children close only their descriptor; the root outlives every path released under it.
- An adopted arena (`_adopt`, until S4b) owns nothing: scope exit closes its descriptor; `reclaim()` raises.
- Kill: the arena stays, lock released, for S5; a kill inside `open` leaves an empty pending directory (L4).
- Isolated child exit leaves the work root to the parent's `reclaim()` (`f06:execution/_isolated_run.py:363`).

#### 3.1.2 Routes

T-PLAIN and T-MODES cells, each under ambient and a strict `RootedFileAccess`; routes A-4 refuses under strict assert the refusal.

- OOC batch and reorder lanes, plain and declared (`f06:execution/out_of_core/_runner.py:181-183`); companion present and absent; sink whole-table and streamed.
- Isolated subprocess (also `output_dir` elsewhere and on another device) and in-process fallback.
- Postgres snapshot (ambient only, stub connection), discovery, subset then mask, quarantine, fixed width.

#### 3.1.3 FK shapes

Through both OOC lanes. The parity suites (`tests/parity/test_out_of_core_fk_parity.py`, F06's `tests/acceptance/test_f06_route_parity.py`) run unchanged before and after S2a-1 to S2b-3 as the class 9 characterization.

- Single, composite, self-referencing.
- Multi-parent, cross-batch.

#### 3.1.4 Fields of public result types touched

No path, name or identifier fields, so G6 holds by field set.

- New frozen `QuotaReport(route, dimension, effective_bytes_cap, observed_bytes, disposition)`, `CleanupReport(disposition, retained_bytes, retained_inodes)`, `SweepReport(scanned, reaped, retained_live, retained_unknown, failed)`.
- Errors gain an optional `report`; `IsolatedRunResult` (`f06:execution/_isolated_common.py:321`), sink results and public signatures unchanged.

#### 3.1.5 Output surfaces

- `Job.log`, API errors (`str(exc)`), CLI stderr: T-REPORT-TEXT, native-text test.
- Output and quarantine files unchanged; scratch files (G1).

## 4. Design: the arena is an A-4 capability

### 4.1 Type and placement

New `src/decoy_engine/fileaccess/_scratch.py` (about 330 lines, plus `ScratchAccess` moved in at S2a-1b). Pattern: `cap-std` `Dir` and WASI preopens (as A-4 cites, `_types.py:3-6`); directory creation is L0.1's allocator (`fileaccess/_protected_dir.py`), never a second one.

```text
ScratchArena(StageDir)                # A-4 protocol, _types.py:179
  display = "engine.scratch"; strict = True   # constants, as ScratchAccess handles (_rooted.py:86-90)
  open(*, under: Path | None = None) -> ScratchArena    # parent rule and allocation: _protected_dir
                                       # (_parent_base, _allocate); records whether it may release a path
  create_file(component) -> int        # reserved-name check, then _output.create_private_at(dir_fd, component)
  open_file(component) -> int
  create_dir(component) -> ScratchArena (child: no finalizer, shares root lock and finished state)
  remove(component), rename(src, dst)  # one dir_fd-relative call each
  local_path(component: str | None = None) -> str   # the only path release (rule below)
  local_stage_path() -> str            # raises, as A-4's _StageDir does (not a sink stage)
  reader() -> ScratchAccess            # moved from f06 out_of_core/_scratch.py:122-151
  reclaim() -> CleanupReport
  _adopt(path) -> ScratchArena         # transitional, non-owning (section 10)
scratch_native(fn, *args, **kwargs)    # the value-free native boundary (below)
```

A scrubbed failure reads `FileAccessReadError("engine.scratch")` (`guarded`, `fileaccess/_errors.py:142-163`), under ambient too (section 8). `ScratchAccess` moves in S2a-1b (after L0.2, which edits `out_of_core/_scratch.py`); `arena.reader()` is its only issuer once `scratch_access_for` loses its last caller (`_stage.py:80`) in S2a-1b. The A-4 writers (`adapters/arrow.py:72-126`, `serial.write_text`) need only `create_file`, `open_file`, `display`, `strict`; S1a widens their annotation to `StageDir`.

Handing over. `run_staged` opens `ScratchArena.open(under=temp_dir)` if the caller named `temp_dir` (the one ambient check), else `open()`, stages T18 into it, pops `_arena` from `call` as F06 pops `_staged` (`f06:declared_casting/_stream.py:200`; `call` is the route's `dict(locals())`, `_runner.py:181`, so a kept `_arena=None` is a duplicate keyword), sets `call["temp_dir"] = Path(arena.local_path())` as F06 sets its root (`:206`; allowed: an `under=` arena exists only under ambient), and calls the route with `_arena=arena`, `_staged=True`. T18's `LazySource` holds the absolute `arena.local_path(component)`, as F06 holds an absolute path (`:142`; `ScratchAccess.select` strips the prefix, `out_of_core/_scratch.py:140-148`), not T02's bare component. A route given `_arena` uses it as its root: no fresh child, no second `require_ambient`, no reclaim, one budget walk (`_runner.py:294` measures that `temp_dir`). So F06's `test_stream_staging.py` (`:41`, `:56`, `:71`, `:100-102`, `:124`, `:148-149`, `:166-169`, `:192-201`) and `test_staging_file_mode.py:73-84` run with no diff: stubs take `_arena` through `**rest`, and every path they read is the released root or a file under it. `_ENGINE_ROOTS` and `_is_engine_root` go with `engine_scratch_root` (S2a-1).

F06 helpers (reuse-first): `open_private` becomes `create_file`, `private_dir` becomes `create_dir`; the `precreate_private` plus `PRIVATE_COPY` pattern is `create_file(c)` (closed) then `local_path(c)` as the `COPY` target. Each is deleted with its last caller: `engine_scratch_root` S2a-1, `scratch_access_for` S2a-1b, `precreate_private` S2a-1c, `open_private`, `close_private`, `private_dir` S2b-2.

Naming and marker. `ARENA_PREFIX = "decoy-arena-v1-"` is defined once, in `fileaccess/_scratch.py`. `open` asks the allocator for the root: `_protected_dir._parent_base(under)` (the parent rule `protected_scratch_dir` applies today: `require_ambient` for a caller path, a live issued root accepted under every policy, missing levels made by `_make`) and `_protected_dir._allocate(base, "decoy-arena-pending-")` (its token loop over `_create_level`: `mkdir` 0700, chmod 0700, `lstat` check, removed on any failure). Both are extracted from `protected_scratch_dir` unchanged, which then calls them. `open` opens the root `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` and checks it (4.2), draws `token = secrets.token_hex(8)`, creates `.decoy-arena` inside with `_output.create_private_at`, holding a fixed magic plus `token`, takes `LOCK_SH`, renames the root by `dir_fd` to `ARENA_PREFIX + token`, then calls `_protected_dir._register(final)`; `reclaim()` and the finalizer call `_protected_dir._unregister(final)` before removal, as `protected_scratch_dir` does. The issued-root set stays in `_protected_dir.py`, so `open_protected_dir` and `protected_scratch_dir(parent=)` accept an arena's paths under every policy, as they accept L0.1's own roots. A failed rename draws a new token and rewrites the fixed-length marker in place by its descriptor. `.decoy-arena` is a reserved component: `create_file`, `create_dir`, `remove` and `rename` refuse it. Sentry: `test_arena_prefix_used_only_by_open`.

Path release. `local_path` is the only path-returning method. Rule fixed at `open`: an `under=` arena releases only under the ambient policy, else `FileAccessUnavailable` (as `_local.py:268-271`); an engine-rooted arena under any policy. Callers, exactly (key `scratch_path_release` in `tests/sentry/file_access_modules.toml`, both ways): `_isolated_run.py` (sources manifest and child command, `:415`, `:456`), `_isolated_commit.py` (payload writer, S3a), `_relation.py`, `_multi_parent.py` (`COPY` targets, `read_parquet`), `_duckdb.py` (`temp_directory`), `_runner.py` (budget walk), `declared_casting/_stream.py` (budget walk, handed `temp_dir`, T18 source path), `_pool_quality.py` (S4b), `postgres_snapshot.py` (S3b), `adapters/duckdb.py` (only if T17 picks the arena fix), `_payload_store.py` and `_external_sort.py` (only under Q5's fallback); `_rewrite_pass.py` transitionally (arm (a)). Released paths are L1.

Native boundary. `scratch_native(fn, *args, **kwargs)` runs one native call under every policy and maps a failure through one closed classifier: by type only, never message text, returning an enum, copying no text:

- resource-errno `OSError` (`_RESOURCE_ERRNOS`, `fileaccess/_errors.py:27`): rebuilt errno-only (`_resurface`, `:128`) carrying `report=QuotaReport(dimension="bytes")`, no errno field on the report;
- `duckdb.IOException`, and a pyarrow `OSError` whose `errno` is `None`: `FileAccessReadError("engine.scratch")` raised `from None`, carrying `report=QuotaReport(dimension="unknown", effective_bytes_cap=None, observed_bytes=None)`;
- memory failures: unchanged from `guarded` (`passes_by_type` for `MemoryError` and `duckdb.OutOfMemoryException`, `:100-110`; `resurface_scrubbed` for text-recognised ones, `:113-125`), so `is_memory_failure` (`execution/_isolated_common.py:178`) still matches;
- anything else: `FileAccessReadError("engine.scratch")` `from None`, no report.

S1b ships the memory and "else" arms; the report arms land in S4a. Batch pulls use the same mapping over `guarded_iter` (`:191`). Every outward error is raised after the `except` block ends, never inside it, so `__context__` and `__cause__` are both `None` (the pattern L0.1 uses, `_protected_dir.py:17-18`). The sink aborts on any of them.

`scratch_native` key: every module that calls a method on, or builds, a connection made by `connect_duckdb` or holding a released path. From `git grep -n "conn\.\(execute\|sql\|register\|fetch\)" -- src/decoy_engine/execution` on F06 (`_stream_join_plan.py` at `:63`, `:64`, `:73`, `:77`, `:120`; `_payload_store.py:233` is a docstring) plus `_duckdb.py`'s `connect_internal` call (`:64`): `_relation.py`, `_multi_parent.py`, `_duckdb.py` (S2a-1c), `_batch_join.py`, `_join.py` (S2a-2), `_stream_join.py`, `_stream_join_plan.py` (S2b-2), `native/_pool_quality.py` (S4b). The sentry fails on a listed module calling a connection method outside `scratch_native`. `postgres_snapshot.py` already raises code-only errors `from None` (`:38-39`, `:239-242`).

### 4.2 Two creation choke points (the root fix) and the descriptor checks

Modes are set by the creator, never inherited, and the arena never creates anything itself. Two functions create, and nothing else in the arena path does:

- Directories by path: L0.1's allocator. The root and each missing `under=` level come from `_protected_dir._create_level` (as `protected_scratch_dir` uses today: `mkdir` 0700, `chmod` 0700, `lstat` check, removed on failure).
- Directories by descriptor: `_output.make_private_dir_at(parent_fd, name) -> int`, in A-4's capability module: `mkdir(name, 0o700, dir_fd=)`, `chmod(name, 0o700, dir_fd=)`, open `_DIR_FLAGS` by `dir_fd`, then the `fstat` check; on any failure `rmdir` by `dir_fd` and raise `ProtectedDirUnsafe` or `ProtectedDirAclFailed` (code-only, L0.1). A-4's two existing descriptor-relative `mkdir` sites, `_StageDir.create_dir` (`_output.py:171`) and the directory stage in `RootedOutputTxn.__init__` (`:228`), call it, which also fixes their own umask-0777 birth at 0000 (Dennis r1 I). Arena `create_dir` children and each level `_adopt` makes call it. `_adopt` walks from the deepest existing ancestor, opened `_DIR_FLAGS` by path under the ambient policy only (it is the transitional `Path` arm).
- Private files: `_output.create_private_at(dir_fd, name) -> int`: `os.open(name, _FILE_CREATE, 0o600, dir_fd=)`, `fchmod` 0600, then the `fstat` check, before any byte; on failure close, `unlink` by `dir_fd`, raise. A-4's existing private branches call it: `_StageDir.create_file` when its mode is 0600 (`:160-161`, which today has no `fchmod`) and `RootedOutputTxn.create_file`'s private stage (`:240-253`). Arena `create_file` and the marker call it. `out_of_core/_scratch.open_private` (`:68-83`) is deleted with its last caller (S2b-2).
- One ownership predicate: `_protected_dir._owned_private(st) -> bool` (`st_uid == geteuid()` and no group or other bits), used by L0.1's `_check` (`:159-160`, which reads `os.getuid()` today; the engine never runs set-uid, so the two agree) and by both creators, which also assert the exact mode they set.

Because `_output.py` is a capability module (`tests/sentry/file_access_modules.toml` `capability_modules`), these sites need no new D9 registry row and no `private-file-sinks.json` growth; G-ONE-PROTECTED-DIR already judges them as descriptor-relative. Not folded, logged in 11: the ambient `_local.py:247-251` `mkstemp` then `fchmod` (path-based, a caller directory) and `declared_casting/_quarantine_local.py:217-239`.

Guard: `tests/sentry/scratch/test_scratch_arena_prefix.py::[create_choke_point]`, built on `tests.sentry.fa.scan` (which resolves aliases), fails on any `os.mkdir`, `os.makedirs`, `mkdtemp`, `Path.mkdir`, `os.chmod`, `os.fchmod` or `O_CREAT` in `fileaccess/_scratch.py` or `fileaccess/_scratch_sweep.py`, and on `O_CREAT` or `os.mkdir` in `_output.py` outside `create_private_at` and `make_private_dir_at`. G-ONE-PROTECTED-DIR (L0.1) already fails a `protected_root` creator outside the allocator. Mutations that must turn it red: an `os.mkdir(name, 0o700, dir_fd=fd)` in `ScratchArena.create_dir`; a second `os.open(..., _FILE_CREATE, ...)` in `_StageDir.create_file`. A new creation site cannot forget the mode, because it has no way to create except through a function that sets it.

The checks. At `open`: `fstat` on the root descriptor must show `S_ISDIR`, `st_uid == geteuid()` and `S_IMODE == 0o700`, else `ProtectedDirUnsafe`, nothing changed. In `make_private_dir_at` and `create_private_at`: type, owner and exact mode (0700, 0600) on the new descriptor, before use. A swap between `mkdir` and `chmod` needs the same UID (L6, as L0.1's own named limit); the `O_NOFOLLOW` open refuses a symlink. A directory the engine did not create (an existing `_adopt` leaf, a swept entry) is checked strictly, never chmodded; a caller's `temp_dir` keeps its mode (Q8), as `protected_scratch_dir(parent=)` leaves an existing parent today.

### 4.3 Producers

Per section 3. T17: if `T-DISCOVERY-SPILL` shows DuckDB writes outside the arena for an in-memory untrusted session, `untrusted_session` sets `temp_directory` to `arena.create_dir("duckdb").local_path()` before `lock_configuration` and its queries go through `scratch_native`. If no spill is possible with external access off, T17 records that as a tested fact, with no production change.

### 4.4 Sweep (S5)

`fileaccess.sweep_scratch() -> SweepReport`, no parameters. It scans only `tempfile.gettempdir()` at call time, the directory `ScratchArena.open()` uses with no `under=`. For each entry, opened by descriptor:

1. Only names for which `re.fullmatch(r"decoy-arena-v1-[0-9a-f]{16}", name)` holds (not `re.match` with `$`, which accepts a trailing newline) (never `decoy-arena-pending-*`, platform `decoy-job-*`, `decoy-preflight-*`, `decoy-src-fd-*`, engine `decoy-ooc-*`, `decoy-isolated-run-*`); a directory (`O_NOFOLLOW`), owned by `geteuid()`, mode 0700.
2. Ownership proof: `.decoy-arena`, opened by `dir_fd` `O_RDONLY|O_NOFOLLOW`, is a regular file owned by `geteuid()`, mode 0600, with the magic and a token equal to the entry's name token. Any failure in steps 1-2 is `retained_unknown`, never deleted.
3. `flock(LOCK_EX | LOCK_NB)` on the entry; `EWOULDBLOCK` is `retained_live`.
4. Lock held: remove every top-level entry except `.decoy-arena` (`_rmtree_fd` alone goes in `scandir` order, `_output.py:82`), then unlink the marker, then `rmdir` the entry. A kill before that leaves a provable arena; a failure is `failed`. `reclaim()` uses the same helper.

Pattern: `flock(2)` on the open file description, inherited across `fork`/`exec`; an ownership marker checked before any delete (exact target). Pending-then-rename closes the `open` window; the lock-to-`rmdir` window is L6. The same lock is what `systemd-tmpfiles` honours: its age-based cleanup of `/tmp` skips a directory, and everything below it, while a shared or exclusive BSD lock is held on it (`systemd-tmpfiles(8)`), so a live arena also survives the distribution's `/tmp` aging. L0.1's own roots (`protected_scratch_dir`, default prefix `decoy-` plus 16 hex) never match the step 1 pattern.

### 4.5 Windows (win32 arm)

The CLI V1 matrix includes Windows x86_64 subject to WIN-FEAS (roadmap 5.5, which supersedes F22's Linux-and-macOS-only note). The descriptor design (`dir_fd`, `fchmod`, `O_NOFOLLOW`, `flock`) is POSIX. Today, after L0.2, Windows scratch roots are L0.1 DACL directories (`D:P`, owner and SYSTEM, inherited by children through `OICI`), so the arena must not regress them. On win32: `open` uses the same `_parent_base` and `_allocate` (the DACL branch), names the root `ARENA_PREFIX + token` directly (no pending rename, no marker, no lock), and registers it; `create_dir` uses `_create_level`; `create_file` calls `_protected_dir._win_create_file(path)`, beside `_win_create`: `CreateFileW` with `CREATE_NEW` and the same protected owner-and-SYSTEM descriptor passed explicitly, so the file never exists without it, returned as a descriptor through `msvcrt.open_osfhandle`; `reclaim()` removes by path and reports `RETAINED` on failure; `sweep_scratch()` returns an empty `SweepReport` and deletes nothing. The 4.2 descriptor checks are replaced by L0.1's `_win_state` read-back on the root. Named limit L9: on win32 a killed run's arena is never reclaimed and check-then-use is path-based (L0.1's own limit). POSIX-only, refused on win32 with `FileAccessUnavailable` (no regression: the isolated route already fails there at `import resource`, `execution/_isolated_common.py:28`): `reader()` (`ScratchAccess` opens with `O_DIRECTORY|O_NOFOLLOW`, `out_of_core/_scratch.py:132`), the `pass_fds` hand-off (S3a), `flock`, the sweep. The arm is about 60 lines in `fileaccess/_scratch.py` behind one `sys.platform` branch in `open`, plus `_win_create_file` (about 20) in `_protected_dir.py`'s win32 block. `test_win32_arena` runs by hand on L0.1w's Windows host (WIN-RUNNER, roadmap row 6; there is no engine Windows CI job). Until then the arm is unverified, as L0.1's Windows branch is.

### 4.6 F22 hand-offs

F22 revision 9 (`plan/f22-publication-replan` `db230090`) names F25 only for stage reclaim (its L2 at line 71, Q1 at line 420). The superseded F22 plan (`2026-10-29-f22-atomic-idempotent-publication.md:112`, `:282`) also expected encryption at rest and a budget from F25; revision 9 dropped both (the backup is deferred), and F25 V1 provides neither, so nothing is owed there.

- F22 Q1 defaults reclaim of SIGKILL-left `_decoy_publication_stage_*` and `._decoy_mkdir_*` stages to F25. F25 does not take it: those stages sit beside caller destinations anywhere on disk, carry no owner lock (F22 says so), and F25's sweep scans one fixed root (`test_sweep_takes_no_root`) and never deletes what it cannot prove (G7, L4). Recommended answer (Q10): F22's next `begin()` for a destination removes only that destination's own `_decoy_publication_stage_*` stages, under the destination lock it already takes (the Maildir `tmp/` and git `*.lock` pattern of the next writer cleaning up after a dead one). `._decoy_mkdir_*` stages sit in parents shared across destinations, so no one destination's lock proves them dead; they stay litter (F22 L2), never removed automatically.
- Platforms: F22 line 70 still says Windows is out; roadmap row 59 says its revision must drop that. Its destination `flock` has the same win32 gap as the arena's; both should take one platform answer from WIN-FEAS rather than two.

F22 does not consume the arena (its stages live beside the destination, L8), so F25 adds no API for F22. The owner's order (F25 before F22, roadmap 8) is unchanged.

### 4.7 Design notes

- New structure: one capability type (`ScratchArena`) and one sweep. Principles from `architecture.md`: one place per fact (prefix, marker name, issued-root set, creation modes each defined once), and capabilities over ambient paths (the arena hands out descriptors; `local_path` is the single, allowlisted release). The change pain addressed: before this, every producer chose its own directory, mode and cleanup (section 3's eighteen rows); after it, a new producer gets all three by taking an arena.
- Where each fact lives: directory creation, the issued-root set and the win32 DACL in `fileaccess/_protected_dir.py` (L0.1); descriptor-relative directory and private file creation in A-4's `fileaccess/_output.py`; the arena, prefix and marker in `fileaccess/_scratch.py`; reports in `fileaccess/_scratch_reports.py`; the sweep in `fileaccess/_scratch_sweep.py`; the native-failure mapping in `scratch_native` beside the arena, reusing `_errors.py`'s classifiers.
- House style: producers take one typed `arena` argument rather than `temp_dir` plus flags; no flag argument switches arena behaviour (owning versus adopted is a separate constructor, `_adopt`).

### 4.8 Established solutions

Search run for this revision: the codebase (`git grep` on `main` for directory and file creators, `protected_scratch_dir`, `_ENGINE_ROOTS`, `write_private`; the LIB plan and handoff for L0.2) and the web (`systemd-tmpfiles` and `tmpfiles.d` lock handling). Found and followed:

- Capability directories: `cap-std` `Dir` and WASI preopens (cited by A-4, `fileaccess/_types.py:3-6`): operations relative to a held directory descriptor.
- Private creation: `tempfile.mkdtemp`/`mkstemp` (0700/0600 with `O_EXCL`), and L0.1's allocator, which already applies the explicit chmod that restores the mode the umask narrowed. Reused, not re-implemented.
- Liveness for cleanup: `flock(2)` on the directory, the rule `systemd-tmpfiles(8)` applies to `/tmp` aging (a directory holding a shared or exclusive BSD lock, and everything under it, is skipped). mmdebstrap takes the same lock on its `/tmp` chroot for that reason.
- Ownership before delete: a marker file proving the creator, and pending-then-rename so a final name is never visible half-built (the Maildir `tmp/` then `new/` rename).

Custom code that remains: the marker format and the sweep's proof order (no library ships them for this layout), and the native-error classifier (DuckDB and pyarrow raise their own types). Each is a few dozen lines.

### 4.9 Placement

The engine CODEMAP has no "Where things go" section; its Entry Points and Directory Map rows place modules. Three new test sub-packages are map changes, stated here and added to the CODEMAP `tests/` row (`CODEMAP.md:127`) in the slice that creates each, because `tests/fileaccess/` (47 entries), `tests/sentry/` (68), `tests/unit/execution/` (230) and `tests/` (28) are all over the 25-module fan-out limit and take no new flat modules.

| New file | Directory | Map line | Sits beside |
|---|---|---|---|
| `_scratch.py` (S1a) | `src/decoy_engine/fileaccess/` | `CODEMAP.md:25` (the capabilities the policy issues; the allocator) | `_output.py`, `_protected_dir.py` |
| `_scratch_reports.py` (S1a) | `src/decoy_engine/fileaccess/` | `CODEMAP.md:25` | `_scratch.py` |
| `_scratch_sweep.py` (S5) | `src/decoy_engine/fileaccess/` | `CODEMAP.md:25` | `_scratch.py` |
| `test_scratch_arena.py`, `test_scratch_plain_host.py`, `test_scratch_native.py`, `test_scratch_sweep.py`, `test_publication_crash_matrix.py`, `test_private_creators.py`, `test_discovery_spill.py` | `tests/fileaccess/scratch/` (new sub-package) | `CODEMAP.md:127` (map change: add `fileaccess/scratch/`) | `tests/fileaccess/protected_dir/` |
| `test_protected_dir_levels.py` (S1a-0) | `tests/fileaccess/protected_dir/` | `CODEMAP.md:127` (`fileaccess/protected_dir/`) | `test_protected_dir.py` |
| `test_scratch_modes.py`, `test_scratch_quota.py`, `test_isolated_payload_mode.py`, `test_crash_mid_spool.py` | `tests/unit/execution/scratch/` (new sub-package) | `CODEMAP.md:127` (map change: add `unit/execution/scratch/`, as `derived_visibility/` at `:130`) | `tests/unit/execution/derived_visibility/` |
| `test_scratch_arena_prefix.py` (S1b) | `tests/sentry/scratch/` (new sub-package) | `CODEMAP.md:127` (map change: add `sentry/scratch/`, as `sentry/protected_dir/`) | `tests/sentry/protected_dir/` |
| `test_staging_arena_budget.py` (S2a-1) | `tests/unit/declared_casting/` (10 entries) | `CODEMAP.md:127` (`unit/`) | `test_quarantine_begin_failure.py` |
| `test_snapshot_spill_mode.py` (S3b) | `tests/connectors/` (11 entries) | `CODEMAP.md:127` (`connectors/`) | existing connector tests |

`src/decoy_engine/fileaccess/` holds 13 modules plus `adapters/`; three more keep it under 25.

## 5. Acceptance tests, red first

Each test is recorded red first, against its neutered control or F06 at the fail-before location (an `ImportError` is not a red); where F06 already makes a site 0600/0700, the red is the control and the mutation shows non-vacuity.

| Test (file::name) | Scenario and observable result | Fail-before location (red reason) | Mutation that must turn it red | Slice |
|---|---|---|---|---|
| `tests/fileaccess/scratch/test_scratch_arena.py::test_modes_under_umask[000,002,022,077,777]` (T-ARENA-DESC) | Per umask: `open()`, `open(under=)` a missing `a/b`, `create_dir`, `create_file`, `_adopt` of a missing `a/b`: created dirs 0700, files 0600, owner euid; `.decoy-arena` 0600 with the name's token. Asserts `geteuid() != 0` first | Under 777 a no-chmod control fails at `open` (`EACCES`) or the `fstat` mode check; under 000 A-4's `0o666` (`_output.py:225`) shows 0666 | Drop the chmod in `_create_level` or `make_private_dir_at`, or the `fchmod` in `create_private_at`; `0o600` to `0o644` | S1a |
| `tests/fileaccess/scratch/test_private_creators.py::test_make_private_dir_at_under_umask[000,022,077,777]` | Non-root: `make_private_dir_at(fd, "x")` gives a 0700, euid-owned directory and an open descriptor; a symlink at the name is refused (`ProtectedDirUnsafe`), nothing left; an `fstat` mode mismatch removes the directory and raises | Control `os.mkdir(name, 0o700, dir_fd=fd)` alone: under 777 the directory is 0000 and the descriptor open fails `EACCES` | Drop the `chmod`; drop the `rmdir` on failure | S1a-0 |
| `tests/fileaccess/protected_dir/test_protected_dir_levels.py::test_extraction_keeps_protected_scratch_dir` | `tests/fileaccess/protected_dir/test_protected_dir.py` and `test_quarantine_arena.py` pass with zero diff after `_allocate` and `_parent_base` are extracted | Green by design (characterization, class 9) | Change the token count or drop `require_ambient` in `_parent_base` | S1a-0 |
| `tests/fileaccess/scratch/test_private_creators.py::test_create_private_at_mode[000,022,777]` | `create_private_at(dir_fd, "f")`: regular, euid, 0600 before the first write; an existing name (`O_EXCL`) and a symlink (`O_NOFOLLOW`) are refused | Control without `fchmod`: under 777 the file is 0000 | Drop the `fchmod`; drop `O_EXCL` | S1a-0 |
| `...::test_a4_private_stages_use_the_creators[file_stage,dir_stage_child,dir_stage]` | Strict `RootedFileAccess`, umask 777, non-root: a private file stage, a file inside a private directory stage, and a directory stage are created 0600, 0600 and 0700 and publish | `_output.py:160-161` (no `fchmod`): the child is 0000 and the write fails; `:228` stage dir 0000, open `EACCES` | Restore the bare `os.open` at `:161` | S1a-0 |
| `tests/fileaccess/scratch/test_scratch_arena.py::test_arena_root_is_an_issued_root` | Under a strict `RootedFileAccess`: `open_protected_dir(arena.local_path("x"), create=True)` succeeds while the arena is open and raises `FileAccessUnavailable` after `reclaim()` | Control arena that never calls `_register`: refused while open | Drop `_register`; drop `_unregister` | S1a |
| `tests/fileaccess/scratch/test_scratch_arena.py::test_win32_arena` (Windows lane only) | `open`, `create_dir`, `create_file`, `reclaim`: root and children carry the protected owner-and-SYSTEM DACL (`_win_state`); `sweep_scratch()` returns an empty report and deletes nothing | Control with `os.mkdir` for the root: `_win_state` reports not closed | Replace `_create_level` with `os.mkdir` in the win32 branch | S1w |
| `...::test_marker_mode_under_umask[000,022,077,777]` | Non-root: after `open()`, a fresh `O_RDONLY` open of `.decoy-arena` by `dir_fd` succeeds; regular, euid, 0600, the name's token. `[777_wrong_mode]`: `fchmod` patched to 0o644 raises `ProtectedDirUnsafe`, no final-name arena | Rev 6 4.1's raw marker `os.open`: under 777 mode 0000, reopen `PermissionError(13)` (Codex, `784cb151`) | Drop the marker `fchmod`; drop the `fstat` mode check | S1a |
| `...::test_refuses_unsafe_root[symlink,wrong_mode,not_dir]` | Symlink, 0755 or file root: `ProtectedDirUnsafe` (`scratch_unsafe`), disk unchanged | Control without the `fstat` checks opens it | Delete the mode comparison | S1a |
| `...::test_exit_paths` (T-ARENA-EXITS) | The 3.1.1 exits; primary error kept; failed reclaim is `RETAINED` | Control swallowing errors reports `REMOVED` | Return `REMOVED` on `OSError` | S1a |
| `...::test_arena_is_stagedir` | `isinstance(arena, StageDir)`; unmodified `parquet_writer(arena, ...)` and `write_text` write 0600 files; `.decoy-arena` refused as a component | Red (mypy) before the annotations widen | Drop the `strict` attribute; drop the reserved-name check | S1a |
| `...::test_local_path_policy` | `under=`: strict raises `FileAccessUnavailable`, ambient returns; engine-rooted returns under both | Refusing every strict release fails the `out_of_core_fk` golden (`test_policy_routes.py:69`) | Drop the `under=` ambient check | S1a |
| `tests/fileaccess/scratch/test_scratch_plain_host.py::test_jobs_succeed_without_quota_or_fscrypt` (T-PLAIN) | No `prjquota` or fscrypt: OOC both lanes (plain, declared) with forced spill, subset then mask, hold-back sink match F06's bytes. `[error_path]`: a non-resource pyarrow failure under ambient raises `FileAccessReadError("engine.scratch")` | Control whose `open` requires a quota fails | Quota precondition in `open`; `strict` from the policy | S1a, each later slice |
| `...::test_report_text_is_value_free[reports,errors,budget_message]` (T-REPORT-TEXT) | Sentinels (path, table, cell, DSN) absent from `str`/`repr` of reports and errors; the budget error over a sentinel `temp_dir` | `budget_message`: `_budget.py:539` prints `under {temp_dir}` | Add `detail=str(exc)`; restore the path | S1a, S4a, S5 |
| `tests/fileaccess/scratch/test_scratch_native.py::test_native_failure_text_is_value_free[connect,relation_copy,merged_copy,batch_join_read,join_read,stream_join_read,stream_join_plan_explain,spill,pool_quality]` | Seeded DuckDB failures with sentinels: `COPY` into a removed child, a truncated relation, spill into a read-only `temp_directory`, `EXPLAIN` over a dropped view, `connect_internal` raising `duckdb.IOException` with the path. Raw message holds the sentinel; outward `str`/`repr` hold neither, ambient and strict | F06 re-raises unchanged (`f06:execution/out_of_core/_relation.py:521`, `_stream_join_plan.py:120`, `_duckdb.py:64`) | Call `conn.execute` bare at one site; drop `_stream_join_plan.py` from the key | S1b helper; `connect`, copies S2a-1c; joins S2a-2; stream S2b-2; pool S4b |
| `tests/sentry/test_file_access_boundary.py::test_local_path_callers_are_allowlisted` | Unlisted `local_path` caller, listed module with no call, bare connection call in a `scratch_native` module: each fails; `fa_fixtures` evasions (alias, `getattr`, bound method) caught | Fixture caller passes without the rule | Add a `local_path` call to `_stage.py` | S1b |
| `tests/sentry/scratch/test_scratch_arena_prefix.py::test_arena_prefix_used_only_by_open` | No `decoy-arena-` literal, `prefix=` or `.decoy-arena` outside `fileaccess/_scratch.py`; `open`'s name matches the sweep pattern; `[create_choke_point]` (on `tests.sentry.fa.scan`): no `os.mkdir`, `os.makedirs`, `mkdtemp`, `Path.mkdir`, `os.chmod`, `os.fchmod` or `O_CREAT` in fa `_scratch.py` or `_scratch_sweep.py`; no `O_CREAT` or `os.mkdir` in `_output.py` outside `create_private_at` and `make_private_dir_at` | Seeded fixture passes without it | Add `prefix="decoy-arena-v1-x"` to `_isolated_run.py:370`; a raw marker `os.open` in `open`; an `os.mkdir(name, 0o700, dir_fd=fd)` in `create_dir` | S1b |
| `tests/unit/declared_casting/test_staging_arena_budget.py::test_declared_ooc_one_arena_one_budget[ambient_temp_dir,no_temp_dir,strict_policy]` | Real `run_fk_out_of_core`, declared LazySource: typed copy C, forced spill about C, budget `1.5 * C`: `out_of_core_temp_disk_exceeded` with a `QuotaReport`, nothing committed, copy and spill under one arena root, arena reclaimed. `strict_policy` (budget `4 * C`): completes with F06's bytes. F06's `test_stream_staging.py` and `test_staging_file_mode.py` (umask 000/077/777) stay green with zero diff (4.1) | Rev 4's T01 control (fresh child via `open(under=)`): `_runner.py:294` measures only the child, so the job completes over budget; strict refused by `require_ambient` | Fresh child arena for the route; drop the `_arena` pop (`TypeError`); bare component in T18's source (F06 `test_stream_staging.py:124` fails) | S2a-1 |
| `tests/unit/execution/scratch/test_scratch_modes.py::test_ooc_scratch_modes[batch,reorder]` (T-MODES, T01-T05) | Umask 000, 077, 777 (non-root), `os.mkdir`/`os.open` wrappers, pre-reclaim `fwalk`: dirs 0700, Python files 0600, both lanes with an outgoing FK edge, from the S2a-1a head | Green on F06 (`private_dir`, `_payload_store.py:147`, `_external_sort.py:215`); red against a control with `private_dir` replaced by `Path.mkdir(parents=True)` (under 777: `EACCES`) | Same replacement in `_stage.py` | S2a-1a to S2b-2 |
| `...::test_caller_temp_dir[mode_untouched,missing_created]` | Caller `temp_dir` 0755 stays 0755 with a 0700 arena child; a missing one is created (ambient) | `f06:execution/out_of_core/_scratch.py:36` chmods it; `missing_created` green by design | Restore the chmod | S2a-1 |
| `...::test_adopted_arena_is_non_owning[reorder_spill,relation_survives_gc,relation_survives_gc_arena,reclaim_refused]` | Reorder spill into the missing leaf `_stream_join.py:530` names (made 0700); a relation built via the `Path` arm, and one in a `create_dir` child of an owning root, survives `del` plus `gc.collect()` of its arena object while the root is open, and the join reads it; adopted `reclaim()` raises | A finalizer on `_adopt` or a child deletes `*_key_relation.parquet` | Register a finalizer on `_adopt` or on children | S2a-1b |
| `...::test_ooc_internals_take_only_an_arena[emit,stager,relation,joiners,stream_table,connect_duckdb]` | Each rejects a `Path`; `_adopt` and arm (a)'s entry gone | Red by design while arms live | Restore a `Path` arm | S2b-3, S4b |
| `...::test_spool_and_sorter_modes` (T-MODES, T06-T08) | Spools, sorter runs, pairs 0600 under umask 000; RSS bound unchanged | Green on F06 (`_payload_store.py:99`, `_external_sort.py:382`); red against a `pa.OSFile(str(path), "wb")` control | Restore that writer | S2b-1, S2b-2, S4b |
| `tests/unit/execution/scratch/test_isolated_payload_mode.py::test_payload_never_readable_by_others` (T-PAYLOAD) | Caller `staging_dir` 0755, umask 022: work root, `sources/` 0700; sources 0600; payload 0600 (green on `main` through `write_private`, kept as a regression row) | `_isolated_run.py:410` makes `sources/` at the umask mode (0755) and `:414` `pq.write_table` makes each source 0644 | Restore `pq.write_table(table, dest)` | S3a |
| `...::test_child_holds_arena_after_parent_death` | Parent killed while the child writes: a `LOCK_EX|LOCK_NB` probe fails | Without `pass_fds` the probe succeeds | Drop the descriptor from `pass_fds` | S3a |
| `...::test_output_dir_commit_keeps_sibling_staging[other_dir,other_device]` | `output_dir` elsewhere and on `/dev/shm`: committed, staged beside it; `other_device` asserts differing `st_dev` first, never skipped (`.github/workflows/ci.yml:29`) | Green by design | Stage in the arena (`EXDEV`) | S3a |
| `tests/connectors/test_snapshot_spill_mode.py::test_spill_dir_created_0700` | Spill dir 0700 at creation, umask 022 (stub connection) | `f06:connectors/postgres_snapshot.py:217` creates 0755 then chmods | Restore `mkdir` then `chmod` | S3b |
| `tests/fileaccess/scratch/test_discovery_spill.py::test_discovery_spill_stays_in_arena` (T-DISCOVERY-SPILL) | Discovery query past a forced-low `memory_limit`: no DuckDB temp file outside the arena | Control without `temp_directory`: expected a DuckDB temp dir in the cwd (not run; a green control is recorded as "no spill") | Remove `temp_directory` (arena fix only) | S3b |
| `tests/unit/execution/scratch/test_scratch_quota.py::test_budget_exceeded_commits_nothing` (T-A02a) | Tiny `temp_disk_budget_bytes`: `out_of_core_temp_disk_exceeded` with a `QuotaReport`; no final output | Red for `report`; "no output" green by design | Commit the sink before the budget check | S4a |
| `...::test_enospc_commits_nothing[T02,T03,T04,T05,T06,T07,T08,T11]` (T-A02b) | Python writers: injected `ENOSPC`/`EDQUOT` via a faulting file object over the real descriptor: `QuotaReport(dimension="bytes")`. DuckDB cells (T03, T04 `COPY`, T05 spill): an injected `duckdb.IOException` with a sentinel path: `QuotaReport(dimension="unknown")`, no sentinel outward. All: nothing committed; a text-recognised memory failure still gives `is_memory_failure(...) is True`. T17 cell only if 4.3 routes discovery through `scratch_native` | Rev 4's `scratch_native` gives a bare `FileAccessReadError`, no `report`; Python cells: bare `OSError` | Drop the `duckdb.IOException` arm; catch and continue the write | S4a (T08 S4b) |
| `tests/unit/execution/scratch/test_crash_mid_spool.py::test_sigkill_mid_spool_commits_nothing` (T-A08) | Pause after the first spool batch, then SIGKILL: no final output; arena remains, lock released | Green by design (A-4 stage) | Publish the stage before spooling ends | S4a |
| `tests/fileaccess/scratch/test_publication_crash_matrix.py::test_crash_points[T09,T15,T16]` (T-A10) | SIGKILL before stage fsync, after the publish rename, after the parent fsync: old or complete new output | Green by design | Write directly to the final leaf | S4a |
| `tests/fileaccess/scratch/test_scratch_sweep.py::test_live_arena_survives` (T-A07) | Held arena: `retained_live`; two sweepers after the holder dies: one reaps, one reports; `foreign_exact_name_no_marker` (same UID, 0700, unlocked) and `marker_token_mismatch`: `retained_unknown`, bytes unchanged; `decoy-arena-v1-nothex`, a 0755 match, a foreign-UID entry (root only) untouched. Temp root via `TMPDIR`, `tempfile.tempdir` | Rev 4's 4.4 steps 1-3 delete `foreign_exact_name_no_marker` | Skip the marker check; drop the `flock` probe | S5 |
| `...::test_sweep_takes_no_root` | Signature snapshot: `sweep_scratch()` has no parameters | Rev 4's `root=` parameter | Add `root=None` back | S5 |
| `...::test_sweep_in_open_window` | Sweeps at barriers after `mkdtemp`, after the marker write and after `LOCK_SH` in `open` leave the arena, which then works | Final-name-first control is reaped | Create the root directly under the final name | S5 |
| `...::test_reap_after_kill_and_resume_after_sweeper_crash[owner_killed,umask_777_owner_killed,kill_after_n_unlinks,marker_first_control]` (T-A08-SWEEP) | Owner killed (also under umask 0777): sweep reaps. Arena of three files plus the marker; sweeper SIGKILLed at a barrier after two unlinks: a second sweep reaps it (`reaped == 1`, entry gone) | No-retry control leaves a partial; the marker-first control, killed at that barrier, is `retained_unknown`; a no-`fchmod` marker under 777 is `retained_unknown` | Count a failed delete as reaped; drop the marker's `fchmod`; let the removal loop take `.decoy-arena` in `scandir` order | S5 |
| Ratchet M-RATCHET | A `tempfile.mkdtemp` added in `_relation.py` fails A-4's boundary sentry | Existing; count drops per migrated site | Raise the registry count | each producer slice |

## 6. Class table (classes 1 to 10)

| # | Class | Applies | Closing structure | Test | Mutation |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: reports, errors, native and budget text reach `Job.log` | Field-free reports; code-only errors; `scratch_native` with a grep-defined key and sentry | T-REPORT-TEXT, T-PAYLOAD, native-text test | add `detail`; bare `conn.execute`; drop a key module |
| 2 | Cleanup on an exit path | Yes | Opener reclaims on each exit; finalizer on owning roots only; `RETAINED`; sweep only on marker-proven arenas | T-ARENA-EXITS, T-A07, T-A08, T-A08-SWEEP, `_adopt` cells | swallow a reclaim error; skip the marker check |
| 3 | State and concurrency | Yes: sweep vs owner | `flock` on the open file description, inherited, taken before the rename | T-A07, child-holds, open-window test | drop `LOCK_SH` or `pass_fds` |
| 4 | Route parity and silent fallback | Yes | One arena handed across the declared boundary; one ambient check; modes set, not inherited; disk failures reported | T-PLAIN, T-MODES, T-ARENA-DESC, marker-mode test, one-arena-one-budget, T-A02b | fresh child under a handed arena; drop a chmod or the marker `fchmod`; drop the `IOException` arm |
| 5 | Referential integrity | N/A: no row selection changes | Parity suites as characterization | FK parity suites | n/a |
| 6 | Untrusted producer | Partly: discovery SQL | Session locked after `temp_directory` | T-DISCOVERY-SPILL | remove `temp_directory` |
| 7 | Check then use on a path | Yes, bounded | Descriptor checks; `dir_fd` ops; `local_path` allowlist; marker read by `dir_fd`; fixed sweep root | T-ARENA-DESC, allowlist sentry, `test_sweep_takes_no_root` | delete a `fstat` check; restore `root=` |
| 8 | Weak tests and overclaimed evidence | Yes | "Not shown" column; neutered controls for F06-green sites; injected failures labelled | all | n/a |
| 9 | Refactor drift | Yes | Output parity per slice; named arms ended by a sentry; assertions kept | T-PLAIN, parity suites, internals test | restore a `Path` arm |
| 10 | Frozen contract drift | Yes | Additive exports; signatures, reasons, `CONTRACT_VERSION` unchanged | `test_file_access_entry_points.py` | rename an export |

## 7. Reuse inventory

- A-4 `StageDir`, `OutputTxn` (`_types.py:179-229`), `_output.py` helpers (`_call`, `_close`, `_rmtree_fd`), `validate_component`, adapters (`arrow.*`, `connect_internal`, `untrusted_session`), `guarded`/`guarded_iter`/`passes_by_type`/`resurface_scrubbed` (`_errors.py:100-172`), `require_ambient` (`_entry.py:115`), boundary sentry and registry.
- `main` (F06) `ScratchAccess` (moved) and the `PRIVATE_COPY` in-place `COPY` pattern (`execution/out_of_core/_duckdb.py:21`) on arena methods; reason `out_of_core_temp_disk_exceeded` and `check_temp_disk_budget`.
- L0.1 (`fileaccess/_protected_dir.py`): `_create_level`, the allocation loop and parent rule (extracted as `_allocate`, `_parent_base`), the live issued-root set (`_register`, `_unregister`), `ProtectedDirUnsafe`, `ProtectedDirAclFailed`, reason `scratch_unsafe`, the win32 DACL branch, G-ONE-PROTECTED-DIR. A-4 `_output.py`'s existing descriptor-relative creators, made the only ones (`create_private_at`, `make_private_dir_at`).
- From `feat/f25-protected-scratch` only `scratch/_reports.py` (trimmed, as `fileaccess/_scratch_reports.py`) and the fault-injection idea in `tests/scratch/test_quota_enforcement_fs.py` (rewritten against the arena); see 10.1.

## 8. Frozen contracts touched

- `fileaccess` public surface: additive exports `ScratchArena`, `scratch_native` (its callers sit in `execution/`, `connectors/` and `native/`), `sweep_scratch`, `QuotaReport`, `CleanupReport`, `SweepReport` (no new error class: L0.1's `ProtectedDirUnsafe` and `ProtectedDirAclFailed` are already exported). `CONTRACT_VERSION` 1, `ENTRY_POINTS` unchanged. `protected_scratch_dir` and `open_protected_dir` keep their signatures and behaviour; the extraction in 4.1 is internal.
- Public entry-point signatures unchanged. `run_fk_out_of_core(temp_dir=...)`, `run_pipeline_isolated(staging_dir=...)` and the snapshot `temp_directory` keep their meaning, except that the engine makes a private child inside a caller directory; `output_dir` jobs still stage beside it (T12). `_arena` is a private keyword like F06's `_staged`. CHANGELOG entries in S2a-1 and S3a.
- Error class (pre-GA): a non-resource library failure on scratch raises `FileAccessReadError("engine.scratch")` under ambient too, where F06 raises the raw error with its text; resource-errno and memory failures keep their class. CHANGELOG in S1a; T-PLAIN `[error_path]`.
- Reason registry (Q4) and output bytes (T-PLAIN) unchanged.

## 9. Owner questions (recommended defaults first)

Q1 at-rest encryption: **not in V1 (owner).** Q2 kernel quotas: **optional V2 profile.** Q3 ledger and authority: **not carried;** the sweep's authority is marker, name, owner, mode and `flock`. Q4: **no new reason code.** Q5 fd-backed IPC writers: **median regression at most 5% on sorter-merge and payload-store benchmarks, five runs, S2b-0;** if missed, T06/T07 keep path writers (L1 widened). Q6: **fresh `feat/f25-v1-scratch` from `origin/main` after L0.2 merges** (F06 has merged). Q7: **`sweep_scratch()` called by the CLI or worker, never on import.** Q8 caller roots: **ambient-only, a fresh 0700 child, caller mode untouched.**

Technical calls taken under the standing rule (recorded, not asked): the arena reuses L0.1's allocator, registry and error classes (4.1, 4.2); descriptor-relative creation stays in A-4's `_output.py` (4.2).

Q9 (open from `PHASE1-TRACKS.md`) fate of `feat/f25-protected-scratch` (`4c901d38`): **keep the branch unmerged and untouched as a read-only source; build fresh from `main`; do not rebase it.** Evidence in 10.1. This differs from the orchestrator's recommendation to use it as the build base. Q10 F22 Q1, reclaim of F22's SIGKILL-left publication stages: **F22 itself, on the next `begin()` for the same destination under its destination lock, that destination's `_decoy_publication_stage_*` only; `._decoy_mkdir_*` stays litter; not F25** (4.6). Q11 Windows scratch in V1: **the win32 arm in 4.5 (DACL root, no sweep), verified when WIN-RUNNER exists; if WIN-FEAS drops Windows, S1w is cut.**

## 10. Slices

Bound: at most 10 files per slice (production, tests, sentry data, docs, CHANGELOG) and 800 lines; estimates scale the `feat:` diffstat (`33ef629b..4c901d38`); an over-bound slice is split. Sizes on F06: `_runner.py` 333 and `_rewrite_pass.py` 374 have room; `_relation.py` and `_batch_join.py` are 599 (past 600 needs a `test_module_size.py` census entry in the same slice, counted below); `_stream_join.py` 645 and `_isolated_run.py` 651 may only shrink. In the table, fa = `fileaccess/`, ooc = `execution/out_of_core/`, tests are `test_<name>.py`, both toml = `file_access_modules.toml` and `file_access_registry.toml`.

| Slice | Files (count) | Est. lines | Content | Gate |
|---|---|---|---|---|
| S1a-0 | fa `_protected_dir.py` (`_allocate`, `_parent_base` extracted; `_register`, `_unregister`, `_owned_private`, win32 `_win_create_file` added; about 399 to 450 lines), fa `_output.py` (`create_private_at`, `make_private_dir_at`; its four creation sites routed; 405 to about 445); tests `protected_dir_levels` (new), `private_creators` (new) (4) | 300 | The creation choke points; L0.1 and A-4 behaviour unchanged except the umask-0777 births | Dennis, then Codex |
| S1a | fa `_scratch.py` (new), `_scratch_reports.py` (new), fa `__init__.py`, `adapters/arrow.py`, `serial.py`; tests `scratch_arena`, `scratch_plain_host`; registry toml and its baseline (D9 rows for the arena's by-path root open, `rename`, `unlink`, `rmdir`); CHANGELOG (10) | 640 | Arena, marker, release rule, `_adopt` | Dennis, then Codex |
| S1b | fa `_scratch.py` (`scratch_native`, memory and else arms); test `scratch_native`; two sentry tests; `file_access_modules.toml` and its baseline; `fa_fixtures` seeds (7) | 300 | `scratch_native`, both keys, prefix sentry, evasions | Codex |
| S2a-1 | ooc `_scratch.py`, `_runner.py`, `declared_casting/_stream.py`; tests `staging_arena_budget`, `stream_staging`, `staging_file_mode` (green, unchanged), `scratch_modes` (`caller_temp_dir`); both toml; CHANGELOG (10) | 420 | T01 root, T18 hand-over; `engine_scratch_root` deleted (L0.2 already removed `_ENGINE_ROOTS`) | Codex |
| S2a-1a | `_rewrite_pass.py`, `_runner.py`; test `scratch_modes` (T-MODES); both toml (5) | 200 | T01 subtrees, arm (a) | Codex |
| S2a-1b | `_stage.py`, `_emit.py`, ooc `_scratch.py`, fa `_scratch.py`; tests `scratch_modes`, `policy_scratch`; both toml (8) | 420 | Stager, emit (arm (d)); `ScratchAccess` moved into fa `_scratch.py`; `scratch_access_for` deleted | Codex |
| S2a-1c | `_relation.py`, `_multi_parent.py`, `_duckdb.py`, ooc `_scratch.py`; tests `scratch_native`, `scratch_modes`; both toml (8, 9 with a size census) | 420 | Relations, connections; arms (b), (c) | Codex |
| S2a-2 | `_batch_join.py`, `_join.py`, `_rewrite_pass.py`; tests `scratch_native`, `scratch_modes`; both toml; size census if needed (8) | 300 | Joiners on the arena (arm (b)) | Codex |
| S2b-0 | benchmark (1-2) | 90 | Q5 medians | Codex |
| S2b-1 | `_payload_store.py`, `_stream_driver.py`, `_runner.py`, `adapters/arrow.py`; test `scratch_modes`; both toml (7) | 420 | Spools, `ipc_iter_stream`, `stream_table` on the arena | Codex |
| S2b-2 | `_external_sort.py`, `_external_sort_run.py`, `_stream_join.py`, `_stream_join_plan.py`, ooc `_scratch.py`; tests `scratch_modes`, `scratch_native`; both toml (9) | 560 | Sorter, reorder joiner; last F06 helpers deleted | Codex |
| M1, M2 | at most 10 test files each | 200 each | Migration of stager, emit, relation, joiner, stream-driver callers | Codex |
| S2b-3 | `_emit.py`, `_stage.py`, `_relation.py`, `_batch_join.py`, `_join.py`, `_stream_join.py`, `_rewrite_pass.py`; test `scratch_modes`; modules toml (9) | 250 | Arms (a), (b), (d) ended | Codex |
| S3a | `_isolated_run.py`, `_isolated_worker.py`, `_isolated_commit.py`; test `isolated_payload_mode`; both toml; CHANGELOG (7) | 520 | T11, T12; payload writer to `_isolated_commit.py` | Dennis, then Codex |
| S3b | `postgres_snapshot.py`, `data_discovery.py`, `adapters/duckdb.py`; tests `snapshot_spill_mode`, `discovery_spill`; both toml (7) | 300 | Probe first; T13, T17 | Codex |
| S4a | `_runner.py`, `_budget.py`, `_scratch_reports.py`, fa `_scratch.py`, `_isolated_run.py`; tests `scratch_quota`, `crash_mid_spool`, `publication_crash_matrix`, `scratch_plain_host`; CHANGELOG (10) | 700 | Report arms, fixed budget text; T-A02a/b, T-A08, T-A10 | Codex |
| M3 | at most 10 test files | 150 | `connect_duckdb`, pool-quality callers | Codex |
| S4b | `_duckdb.py`, fa `_scratch.py`, `_pool_quality.py`; tests `scratch_quota`, `scratch_modes`, `scratch_native`, `pool_quality`; modules toml (8) | 320 | Arm (c) and `_adopt` ended; T08 | Codex |
| S1w | fa `_scratch.py` (win32 branch); test `scratch_arena` (`test_win32_arena`); record of the hand run (3) | 120 | 4.5; needs WIN-RUNNER and L0.1w | Codex |
| S5 | `_scratch_sweep.py` (new), fa `_scratch.py`, `_scratch_reports.py`, fa `__init__.py`; test `scratch_sweep`; `docs/security/file-access-policy.md`; CHANGELOG (7) | 780 | Sweep, real subprocesses | Dennis, then Codex |

Transition (I2 suspended only here; four arms, each with its end). (a) `_rewrite_pass.py` passes `child.local_path()` to unconverted callees (until S2b-3), `_runner.py` to `stream_table` (until S2b-1), under a transitional `scratch_path_release` entry. (b) Relation builders and joiners keep a `Path` arm until S2b-3. (c) `connect_duckdb` keeps one until S4b (last caller `_pool_quality.py:246`). (d) `emit_to_sink` and `MaskedKeyStager` until S2b-3 (`_stream_driver.py:458-468` passes paths until S2b-1). Each arm goes through `_adopt(path)`: levels made and checked as 4.2; non-owning (3.1.1), cleanup stays where it is (`_runner.py:323-327`, `_stream_driver.py:561`).

Existing-test migration. `git grep -n "temp_dir=" -- tests` on F06: 217 lines in 37 files, public `run_fk_out_of_core` calls included (they stay). Arms keep old tests green, so M1, M2 (before S2b-3) and M3 (before S4b) migrate at most 10 files each: a fixture builds the arena, assertions unchanged and shown, except `tests/native/test_pool_quality.py:314`, which compares with the child's `local_path()`.

Delivery and order. All slices land on `feat/f25-v1-scratch` (Q6), merged to `main` once after S4b with `test_ooc_internals_take_only_an_arena` green; S5 may follow. Order (every slice after L0.2 merges, because S1a-0 to S1b also write the D9 registry and modules files, which are L0.2's serial lane): S1a-0, S1a, S1b, S2a-1, S2a-1a, S2a-1b, S2a-1c, S2a-2, S2b-0, S2b-1, S2b-2, M1, M2, S2b-3, S3a, S3b, S4a, M3, S4b, S5; S3a/S3b need S1b and L0.2 (one writer per worktree); S4a needs S2b-3 and S3a; S5 needs S3a; S1w needs S1a and a Windows lane and may land any time after. `tests/sentry/file_access_registry.toml`, `file_access_modules.toml` and their baselines stay one writer at a time with any other open slice (LIB handoff serial lanes). `feat/f25-v1-scratch` takes `main` by merge (never rebase) before any slice whose files `main` changed since the last merge, and records the merge commit. Five units in 21 slices, about 7,300 lines.

Cross-repo note. Platform `api/jobs/disk_quota_matrix.py:116-126` and `tests/test_scheduler_s11_full_frame_local_stage.py:204-215` cite engine `mkdtemp(` sites (0.17.0 lines) that F25 moves; the platform repin that takes F25 updates them (repin checklist).

Loop per slice: red first; baselines from `uv.lock`; each new mechanism mutated once, never committed; lint, format, mypy, touched suites, `tests/sentry`, `tests/fileaccess`.

### 10.1 The existing build branch (Q9)

`origin/feat/f25-protected-scratch` (`4c901d38`, 11 commits after `33ef629b`, Oct 29-30) forks from `33ef629b`, before A-4's merge, F06 (#338), L0.1 (#403) and this design. Its `src` diff against `main` adds a separate `src/decoy_engine/scratch/` package (`_arena.py` 677 lines, `_ledger.py` 463, `_authority.py` 214, `_manager.py` 268, `_local_provider.py` 345, `_seam.py` 406, `_capabilities.py` 368, `_policy.py`, `_provider.py`, `_errors.py`, `_reports.py`) and edits the same OOC modules F06 rewrote. Three reasons not to rebase it or build on it:

- It is the revision-1 design: a ledger and an authority, which Q3 dropped, and a scratch manager that would be a second directory allocator beside L0.1's, which the V1 bar blocks.
- Its OOC edits (`_payload_store.py`, `_stage.py`, `_relation.py`, `_runner.py`, `_duckdb.py`, `_external_sort.py`) conflict with F06's rewrite of the same lines (`open_private`, `private_dir`, `precreate_private`).
- Its tests (`tests/scratch/`, about 3.4k lines) assert the ledger and manager API that F25 V1 does not have.

Kept from it, by `git show` into the new branch (never a merge or cherry-pick): `scratch/_reports.py` (trimmed to the three frozen reports, as `fileaccess/_scratch_reports.py`, S1a), and the faulting-file-object `ENOSPC`/`EDQUOT` injection in `tests/scratch/test_quota_enforcement_fs.py` and `tests/scratch/_fakes.py`, rewritten for T-A02b (S4a). The branch stays on the remote unchanged; deleting it is an owner call.


## 11. V2-LOG (listed, not planned) and deferred features

Revision 2's items stand (`937e88fe` 11). Live:

- [7] Same-UID interference in the sweep window between `flock` and `rmdir`, and a same-UID forged marker (L6): `fileaccess/_scratch_sweep.py` (new, S5).
- [7] DuckDB spill files and the isolated child's path writes take the umask mode inside the arena (L1): `f06:execution/out_of_core/_duckdb.py:46`, `f06:execution/_isolated_worker.py:133`.
- [2] An empty `decoy-arena-pending-*` directory left by a crash inside `open` is never reaped (L4): `fileaccess/_scratch.py` (new, S1a).
- [1] The `QuotaReport(dimension="unknown")` arm also fires for errno-less I/O failures that are not disk exhaustion (a truncated file): true but coarse (L3): `fileaccess/_scratch.py` (new, S4a).
- [4] DuckDB disk-full can raise `OutOfMemoryException` ("failed to offload data block"): classed memory, `oom_killed` (`_isolated_run.py:296`), no report; fails closed; T-A02b injects only `IOException`.
- [1] Lazy reader pulls (`to_arrow_reader`, `_join.py:160`) run outside the wrapped call; the sentry checks connection-method calls only.
- [8] T18's forced spill is mostly DuckDB temp files gone after `conn.close()`, before `_runner.py:294`; use a footprint that persists at the boundary.
- [9] `_adopt` refuses an existing non-0700 leaf F06's `private_dir` accepted; old tests pre-creating 0755 dirs may break.
- [8] `test_local_path_policy`'s fail-before (`test_policy_routes.py:69`) cannot go red in S1a; `[budget_message]` lands in S4a or is a strict xfail in S1a.
- [9] `_stream_join.py` (645 lines, may only shrink) gets 10 wrapped call sites in S2b-2; plan an extraction offset.
- [11] `_relation.py` and `_batch_join.py` (599 lines) take a `test_module_size.py` census entry if S2a-1c or S2a-2 grows them past 600 (the engine rule allows up to 700 with an entry); an extraction would avoid the pin (Dennis r1 J).
- [4] Private files not on the shared creators: ambient `_local.py:247-251` (`mkstemp` then `fchmod` in a caller directory) and `declared_casting/_quarantine_local.py:217-239` (Dennis r1 V1-1 follow-up).
- [8] `test_private_creators.py` and `test_publication_crash_matrix.py` sit in `tests/fileaccess/scratch/` though they test A-4 code (Dennis r1 L).

Deferred features: fscrypt (Q1), kernel quotas (Q2), the ledger (Q3), checkpoint and resume (umbrella slice 6), platform lock authority (umbrella section 4).

## 12. Pre-mortem

Likely next: L0.2 lands in a shape that differs from its handoff (S2a-1, S3a and S3b re-read it first); a creation outside the two choke points (sentry, G-ONE-PROTECTED-DIR, T-MODES); the win32 arm unverified until a Windows lane exists (4.5).

## 13. What revision 8 did not verify

- L0.2 is planned, not built: S2a-1, S3a and S3b assume its handoff text (`p05-lib-build-handoff.md`, "L0.2: route engine row directories"); each re-reads L0.2 as merged before starting.
- `os.chmod(name, mode, dir_fd=fd)` and `os.mkdir(..., dir_fd=fd)` on macOS (`fchmodat`, `mkdirat`) were not run; S1a-0's tests assert `os.chmod in os.supports_dir_fd` first and run on the Mac mini lane as well as Linux.
- Anchors were spot-checked (Dependencies, 0.2), not all re-read; the census rows' anchors in unchanged modules are carried from revision 7 at F06 `19e73714`, whose objects are not in this clone (F06 merged squashed as `05e1c0c6`).
- The win32 arm (4.5) has no lane yet; L0.1's DACL branch it reuses is also unverified there.
- `plan-lint plan` was run; nothing else was. No red or green claimed; sizes are estimates. T-DISCOVERY-SPILL's probe was not run. Revision 7's unverified items stand: a non-root CI runner, the marker's 0000/`EACCES` probe, `/dev/shm` versus `/tmp` `st_dev` on `ubuntu-latest`, `flock` on overlayfs and across a rename, the snapshot stub, T-A02b's injected failures, the internal-callee share of the 217 `temp_dir=` test lines.
- `systemd-tmpfiles` honouring the arena lock is from its manual page and a systemd commit, not a run.

## 14. Provenance

Revisions 1 `affde556`, 2 `937e88fe`, 3 `98a72bb6`, 4 `0aa5c873`, 5 `a3a4c1b3`, 6 `784cb151`, 7 `53950b27` (Codex r7 GO). Revision 8 on `plan/f25-v1-finish`. Next gates: Dennis (at most two rounds), then Codex.

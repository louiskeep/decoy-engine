# F25 V1 scratch on A-4: one arena capability, permissions by default, report and sweep

Status: plan, revision 5, a targeted amendment and F06 re-baseline answering Codex `f2540` on `0aa5c873` (NO-GO, 4 V1) and Dennis (GO-WITH-FIXES, 1 V1, the same root as Codex V1 1); section 0 maps them. No code is on this branch; no build is authorized.

Summary: engine scratch is created 0700 (directories) and 0600 (files the engine creates) under any umask, through one arena capability that is an A-4 `StageDir`. A job that runs out of scratch space stops with a value-free report and commits no output. A sweep removes abandoned arenas it can prove it created and leaves live ones alone. Quotas and encryption are optional profiles.

Rules consulted: read for this revision: `/home/cam/dev-rules/README.md`, `00-universal.md`, `development-loop.md`, `risk-and-exceptions.md`, `scope-discipline.md`, `reuse-first.md`, `security.md`, `testing.md`, `destructive-operations.md` (the sweep deletes; V1-D is its exact-target rule). Also selected by the routing table for an R2 storage-security plan and named from revision 4: `architecture.md`, `feature-dev.md`, `verification.md`, `documentation.md`, `workspace-and-vcs.md`. V1 bar and defect classes as stated in the handoff; the engine `CLAUDE.md`; the code at each anchor.

Risk: R2 (plan only): scratch confidentiality, on-disk cleanup, and the OOC, declared-staging, subset, snapshot and spill failure paths. Not R3: the sweep deletes only arenas that carry `open`'s marker under the one platform temp root (4.4), is caller-invoked, and rollback is a revert on a pre-GA engine (`release.py`).

Dependencies. F25 is planned against F06 (`origin/feat/f06-declared-parsing` `19e73714`) as the stand-in for main and builds only after F06 merges (after the R-b tag `v0.18.0` and B-6). Anchors are re-read there, cited `f06:<path under src/decoy_engine>:<line>` (bare relative anchors are F06 too); `fileaccess/`, `_isolated_*.py`, `postgres_snapshot.py`, `data_discovery.py`, `_join.py`, `_emit.py`, `_batch_join.py`, `_stream_join.py`, `_stream_join_plan.py`, `_pool_quality.py` are byte-identical to main. The plan branch merged main `f5b46a2c` (R-b prep, no anchored file) as `aa30bcb8`. The pending F06 umask fix (`p1-engine-f06-umask0777-fix-handoff.md`) moves `scratch_root` and `engine_scratch_root` (`_scratch.py:26`, `:46`) to `private_dir`; S2a-1 deletes the latter either way.

## 0. Revision 5: finding map

| Finding | Class | Change | Test (new or changed) |
|---|---|---|---|
| V1-A (Dennis V1-1, Codex V1 1; rule 1). Rev 4 ignored F06: T01's fresh child under F06's engine root splits `run_staged`'s one root and budget (`f06:declared_casting/_stream.py:183-207`); `open(under=)` refuses that root under strict. | 4 | Dependencies line; `run_staged` hands its arena to the route (`_arena=`); `_ENGINE_ROOTS`, `engine_scratch_root` deleted; F06 helpers become arena methods; T18; slices recounted. | `test_declared_ooc_one_arena_one_budget` |
| V1-B (Codex V1 2; rule 3). `_stream_join_plan.py` and `_duckdb.py` use or build a released-path connection outside `scratch_native`. | 1 | Both join the key, defined by one `git grep` (4.1). | cells `connect`, `stream_join_plan_explain` |
| V1-C (Codex V1 3, Dennis M2; rule 1). Errno-less DuckDB disk failures leave as a bare read error: G3 false for spill and `COPY`. | 4 | One closed by-type classifier (4.1); G3, L3 agree. | T-A02b T03-T05 cells |
| V1-D (Codex V1 4; rule 2). The sweep proved liveness, not ownership; `root=` aimed it anywhere. | 2, 7 | Marker written inside `open`; `sweep_scratch()` has no root (4.4). | T-A07 cells, `test_sweep_takes_no_root` |
| Folds 1-6 | 2, 9, 8 | M1 (3.1.1); M3 (T-MODES); M4 (10); L1/L3 (10); companion notice; T-DISCOVERY-SPILL control. | in place |
| Found on re-read | 1 | The budget error prints the scratch path (`f06:execution/out_of_core/_budget.py:539`); it gains a `QuotaReport` in S4a, so G6 already requires fixed text; now stated. | T-REPORT-TEXT `[budget_message]` |

Cuts (rev 4: 317 lines, 49,397 bytes): rev 4's section 0, section 12's items (now tests), 4.5 to one line, the T09-T16 prose, section 13 to open items, test-table and reuse prose, the V2-LOG `root=` line. Stop-on-repeat: each finding is in the class and form the handoff lists; the budget message is class 1 inside G6's existing claim.

## 1. Guarantee (V1 rules 1 to 3 only: a ratchet plus named limits)

| # | Claim | V1 rule | Shown by | Not shown |
|---|---|---|---|---|
| G1 | On section 3's rows (T01-T08, T11-T13, T17, T18), engine-created scratch directories are 0700 and engine-owned, and Python-created files 0600, at creation, under umask 000, 002, 022. | 3 | T-MODES, T-ARENA-DESC | Native writes (L1); later producers only via G5. |
| G2 | Without project quotas or fscrypt (overlayfs, tmpfs, plain ext4), OOC (declared and plain), subset, snapshot-shaped and forced-spill jobs succeed with F06's output. | 1 | T-PLAIN | An unwritable root still fails. |
| G3 | Over the OOC budget (copy and spill in one arena), or on a resource-errno `OSError` or errno-less native I/O failure in a scratch write, the job stops, the sink aborts, nothing commits, and the error carries a frozen `QuotaReport` (`"bytes"` or `"unknown"`). | 2, 1 | T-A02a, T-A02b, one-arena-one-budget | `"unknown"` names no limit (L3); no write cap without a kernel quota (L2). |
| G4 | A kill mid-spool commits nothing; a kill at a publication step leaves old or complete new output. | 2 | T-A08, T-A10 | Crash-left output stages stay (L4). |
| G5 | A new `tempfile.*`, DuckDB `temp_directory` or path-taking Arrow/Parquet writer outside `fileaccess/_scratch.py` and the adapters fails A-4's boundary sentry until reviewed. | ratchet | `test_file_access_boundary.py`, registry (205 entries on F06), M-RATCHET | Unmodelled calls (L5). |
| G6 | `str()`/`repr()` of the three reports, errors carrying them (the budget error included) and errors leaving `scratch_native` hold no path, table, column or cell value. | 3 | T-REPORT-TEXT, native-text test | Admin logs, tracebacks, `__cause__` (V2). |
| G7 | `sweep_scratch()` acts only on temp-root entries `open` created (exact name, marker token matching the name), never a held one; safe to re-run after a crash mid-delete. | 2, 1 | prefix sentry, T-A07, open-window test, T-A08-SWEEP, `test_sweep_takes_no_root` | Same-UID forgery (L6); cross-host (L7); foreign-UID cell root-only. |

Named limits: L1 DuckDB spill files and the isolated child's path writes take the umask mode inside the 0700 arena (`COPY` targets are pre-created 0600); L2 no byte cap without the quota profile; L3 errno-less failures report `"unknown"`; L4 never swept: output-directory stages, `under=` arenas, an empty pending directory from a crash in `open`; L5 the scanner model; L6 same-UID interference (forged marker, rename onto an empty arena name); L7 single-host `flock`; L8 publication stages keep the output mode (`_output.py:222`, `sibling_staging_dir`).

## 2. Invariants

- I1. No producer in section 3 creates a scratch directory or a Python-written scratch file except through a `ScratchArena` (`StageDir` protocol, `fileaccess/_types.py:179`).
- I2. Internal producers take `arena: ScratchArena` as a required argument: no default, no `None`, no `getattr` fallback. Public entry points create the arena themselves; an internal caller that already holds one hands it on (`_arena=`, 4.1). No unmanaged fallback route exists.
- I3. An arena root is engine-created (`mkdtemp` in the temp root, or a fresh 0700 child of a caller directory, ambient only) under a pending name, opened `O_DIRECTORY | O_NOFOLLOW`, `fstat`-checked (directory, `geteuid()`, 0700), given its marker, locked, then renamed to its `decoy-arena-v1-` name, and held by descriptor.
- I4. `flock(LOCK_SH)` on the root descriptor from before the rename to reclaim; an isolated child inherits it (`pass_fds`).
- I5. Reclaim is descriptor-relative (A-4 `_rmtree_fd`, `_output.py:80`). A failure is reported as `Disposition.RETAINED`, never as success.
- I6. Validation never mutates: descriptor checks raise and change nothing. Reports are frozen dataclasses.

## 3. Census on F06 `19e73714`

The `937e88fe` section 3 `git grep` re-run on F06, each hit read; a reviewed census, not a proof (G5 covers misses). "F06" under Today means F06 already makes the site 0700/0600 (`private_dir`, `open_private`, `precreate_private` with `PRIVATE_COPY`, `f06:execution/out_of_core/_scratch.py:68-119`, `_duckdb.py:21`); the V1 change there is the arena (checks, one root, reclaim, report), not the mode.

| Row | Producer on F06 | Today | V1 change | Slice |
|---|---|---|---|---|
| T01 | OOC root `scratch_root` (`out_of_core/_scratch.py:26-37`); subtrees `_runner.py:234`, `:258-259`, `_rewrite_pass.py:158`, `:265`; cleanup `_runner.py:323-327` | `mkdtemp`; caller `temp_dir` chmodded (`:36`); `_ENGINE_ROOTS` skips `require_ambient` (`:32`); subtrees F06; `rmtree` by path | Handed arena as root, or `open` (`under=temp_dir`: fresh child, ambient only); `create_dir`; owner's `reclaim()` | S2a-1 |
| T02 | Staged keys, `_stage.py:71-80` | F06; read via `scratch_access_for` (`:80`) | `parquet_writer(arena, component, schema)`; `LazySource(component, access=arena.reader())`; no release | S2a-1b |
| T03 | Relation files, `_relation.py:459-545`; `_emit.py:137`, `:150` | F06 `COPY` (`:520-521`); `read_parquet` by path | `create_dir`; `COPY` target `create_file(c)` then `local_path(c)`; `ParentKeyRelation.path` read at `_join.py:117-120`, `_batch_join.py:160-163`, `_stream_join.py:551-554`; native calls via `scratch_native` | S2a-1c (joins S2a-2) |
| T04 | Merged relation, `_multi_parent.py:135-172` | Same as T03 | Same as T03 | S2a-1c |
| T05 | DuckDB connections, `_duckdb.py:32-64` (`connect_internal` at `:64`); `_batch_join.py:154`; `_join.py:112`; `_relation.py:471`; `_multi_parent.py:147`; `_stream_join.py:530` | F06 `private_dir` then `chmod` (`:42-43`); `temp_directory=str(path)` | `connect_duckdb` takes an arena, passes `local_path()` of a child as `temp_directory`, and builds the connection under `scratch_native` | S2a-1c (joiners S2a-2, `_stream_join.py` S2b-2) |
| T06 | IPC spools, `_payload_store.py:97-100`, `:147-152`, `:207-213`; `_stream_driver.py:259`, `:279`; `_stream_join.py:244` | F06 `open_private` sink | `adapters.arrow.ipc_new_stream` (0600); reads via a bounded `ipc_iter_stream` | S2b-1 |
| T07 | Sorter runs, `_external_sort.py:215`, `:382`, `:422`, `:512`; reader `_external_sort_run.py:37`; reorder `_stream_join.py:498` | F06 `open_private`; reads by path | `adapters.arrow.ipc_new_file` (0600); readers via the arena | S2b-2 |
| T08 | Quality pairs, `native/_pool_quality.py:245-249` (tests only call `measure_pool_quality`) | Parquet in a `connect_duckdb` dir; `read_parquet(str(pairs_path))` (`:249`) | Arena component; the pairs path is a listed release; query through `scratch_native` | S4b |
| T09-T16 | Publication sites (`_transactional_sink.py:200`, `subset/_materialize.py:51`, `quarantine.py:313`, `profile/_fixed_width_writer.py:313` and peers) | A-4 `OutputDir.begin` | None; T-A10 covers them | S4a (tests) |
| T11 | Isolated work root, sources, payload, `_isolated_run.py:363-369`, `:406-411`, `:429-434`; child arguments `:420`, `:453` | `mkdtemp` or caller `staging_dir`; umask `sources/`; payload chmodded after writing | Arena (fresh child under a caller `staging_dir`); `create_dir`; `adapters.arrow.write_table`, `adapters.serial.write_text`; child gets `local_path` strings and the descriptor (`pass_fds`) | S3a |
| T12 | Isolated staging output, `_isolated_commit.py:89-100`; `_isolated_worker.py:129-133` | `work_root / "staging_output"`, or `sibling_staging_dir` with `output_dir` | No `output_dir` only: `create_dir("staging_output")` (L8: the arena would give `EXDEV`) | S3a |
| T13 | Snapshot spill, `postgres_snapshot.py:217-218`; stage `:455-459` | Spill `mkdir` then chmod; ambient only (`:447`) | Spill an arena under `temp_directory`; stage unchanged | S3b |
| T17 | Discovery, `data_discovery.py:181-213`, `adapters/duckdb.py:44-61` | In-memory, no `temp_directory` | T-DISCOVERY-SPILL decides (4.3) | S3b |
| T18 | Declared staging, `declared_casting/_stream.py:110-141`, `:183-207` | `engine_scratch_root` (`out_of_core/_scratch.py:46-65`); F06 copies; budget on the root (`:127`, `:139`); root passed as `temp_dir` (`:206`) | `run_staged` opens the arena, stages via `parquet_writer(arena, ...)`, reads via `arena.reader()`, hands it on (`_arena=`, 4.1) | S2a-1 |

Not scratch: vault (`vault.py:341-348`), model pack (`profiling/model_pack/_packio.py:54-59`), cloud readers, logs. F06's local protected quarantine store (`f06:declared_casting/_quarantine_local.py:182-207`) is durable, not scratch: its tokens are receipts that outlive the run, published by rename after a digest trailer (docstring `:1-12`), so reclaim and sweep never own it; it stays ambient-only.

### 3.1 Enumerations

Anchors below are on F06 `19e73714`.

#### 3.1.1 Exit paths of an arena

Each gets a case in `T-ARENA-EXITS` (S1a; adopted and child cases in `test_adopted_arena_is_non_owning`, S2a-1b).

- Success then `reclaim()` by the opener (`run_staged` for a handed arena, never the route); a double `reclaim()` is a no-op.
- Exception in the `with` block: `reclaim()`, primary error re-raised; a failed `reclaim()` is `CleanupReport(RETAINED)`, not masking it.
- Garbage collection runs a `weakref.finalize` reclaim (`src/decoy_engine/fileaccess/_output.py:229`) on the owning root only; `create_dir` children have no finalizer and close only their descriptor, and the root's lifetime covers every path released under it.
- An adopted arena (`_adopt`, until S4b) owns nothing: scope exit closes its descriptor; `reclaim()` raises.
- Kill: the arena stays, lock released, for S5; a kill inside `open` leaves an empty pending directory (L4).
- Isolated child exit leaves the work root to the parent's `reclaim()` (`f06:execution/_isolated_run.py:363`).

#### 3.1.2 Routes

T-PLAIN and T-MODES cells, each under ambient and a strict `RootedFileAccess`; routes A-4 refuses under strict assert the refusal.

- OOC batch and reorder lanes, plain and declared (`f06:execution/out_of_core/_runner.py:181-183`); companion present and absent; sink whole-table and streamed.
- Isolated subprocess (also `output_dir` elsewhere and on another device) and in-process fallback.
- Postgres snapshot (ambient only, `f06:connectors/postgres_snapshot.py:447`; stub connection), discovery, subset then mask, quarantine, fixed width.

#### 3.1.3 FK shapes

Through both OOC lanes. The parity suites (`tests/parity/test_out_of_core_fk_parity.py`, F06's `tests/acceptance/test_f06_route_parity.py`) run unchanged before and after S2a-1 to S2b-3 as the class 9 characterization. Class 5 is N/A because no row selection changes.

- Single, composite, self-referencing.
- Multi-parent, cross-batch.

#### 3.1.4 Fields of public result types touched

No path, name or identifier fields, so G6 holds by field set.

- New frozen `QuotaReport(route, dimension, effective_bytes_cap, observed_bytes, disposition)` and `CleanupReport(disposition, retained_bytes, retained_inodes)`; `SweepReport(scanned, reaped, retained_live, retained_unknown, failed)`.
- Errors gain an optional `report`; `IsolatedRunResult` (`f06:execution/_isolated_common.py:321`), sink results and public signatures unchanged.

#### 3.1.5 Output surfaces

Scanned by T-REPORT-TEXT and `test_native_failure_text_is_value_free` (section 5).

- User-visible `Job.log` and API errors (via `str(exc)`); CLI stderr.
- Output and quarantine files (unchanged); scratch files (G1).

## 4. Design: the arena is an A-4 capability

### 4.1 Type and placement

New `src/decoy_engine/fileaccess/_scratch.py` (about 350 lines, with `ScratchAccess` moved in). Pattern: `cap-std` `Dir` and WASI preopens (as A-4 cites, `_types.py:3-6`) plus `tempfile.mkdtemp`.

```text
ScratchArena(StageDir)                # A-4 protocol, _types.py:179
  display = "engine.scratch"; strict = True   # constants, as ScratchAccess handles (_rooted.py:86-90)
  open(*, under: Path | None = None) -> ScratchArena    # under != None: require_ambient first;
                                       # records here whether the arena may release a path
  create_file(component) -> int        # O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0o600, dir_fd
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

A scrubbed failure reads `FileAccessReadError("engine.scratch")` (`guarded`, `fileaccess/_errors.py:142-163`); `strict` is `True` under ambient too (section 8). `ScratchAccess` moves in S1a; `arena.reader()` is its only issuer once `scratch_access_for` loses its callers (`_stage.py:80`, `_stream.py:141`) in S2a-1b. The A-4 writers (`adapters/arrow.py:72-126`, `serial.write_text`) need only `create_file`, `open_file`, `display`, `strict`; S1a widens their annotation to `StageDir`.

Handing over (V1-A). `run_staged` opens `ScratchArena.open(under=temp_dir)` if the caller named `temp_dir` (the one ambient check, as F06 does), else `open()`, stages T18 into it, and calls the route with `_arena=arena` beside `_staged=True`. A route given `_arena` uses it as its root: no fresh child, no second `require_ambient`, no reclaim (the opener reclaims), one tree and one budget walk (`f06:execution/out_of_core/_runner.py:294` measures `arena.local_path()`, which holds the copy). `_ENGINE_ROOTS`, `_is_engine_root` and `engine_scratch_root` are deleted in S2a-1. Direct callers of `run_fk_out_of_core(temp_dir=...)` keep revision 4's meaning.

F06 helpers (reuse-first): `open_private` becomes `create_file`, `private_dir` becomes `create_dir`; the `precreate_private` plus `PRIVATE_COPY` pattern is `create_file(c)` (closed) then `local_path(c)` as the `COPY` target. Each is deleted with its last caller: `engine_scratch_root` S2a-1, `scratch_access_for` S2a-1b, `precreate_private` S2a-1c, `open_private`, `close_private`, `private_dir` S2b-2.

Naming and marker (V1-D). `ARENA_PREFIX = "decoy-arena-v1-"` is defined once, in `fileaccess/_scratch.py`. `open` makes the root with `mkdtemp(prefix="decoy-arena-pending-")`, opens and checks it (4.2), draws `token = secrets.token_hex(8)`, creates `.decoy-arena` inside by `dir_fd` with `O_CREAT|O_EXCL|O_NOFOLLOW`, 0600, holding a fixed magic plus `token`, takes `LOCK_SH`, then renames the root by `dir_fd` to `ARENA_PREFIX + token`. A failed rename draws a new token and rewrites the fixed-length marker in place by its descriptor. `.decoy-arena` is a reserved component: `create_file`, `create_dir`, `remove` and `rename` refuse it. Sentry: `test_arena_prefix_used_only_by_open`.

Path release. `local_path` is the only path-returning method. Rule fixed at `open`: an `under=` arena releases only under the ambient policy, else `FileAccessUnavailable` (as `_local.py:174`); an engine-rooted arena under any policy. Callers, exactly (key `scratch_path_release` in `tests/sentry/file_access_modules.toml`, enforced both ways): `_isolated_run.py` (child arguments, `f06:execution/_isolated_run.py:420`, `:453`), `_isolated_commit.py` (only if S3a moves the payload writer there), `_relation.py`, `_multi_parent.py` (`COPY` targets, `read_parquet`), `_duckdb.py` (`temp_directory`), `_runner.py` and `declared_casting/_stream.py` (read-only budget walk, `check_temp_disk_budget(Path(arena.local_path()), ...)`), `_pool_quality.py` (S4b), `postgres_snapshot.py` (S3b), `adapters/duckdb.py` (only if T17 picks the arena fix), `_payload_store.py` and `_external_sort.py` (only under Q5's fallback); `_rewrite_pass.py` transitionally (arm (a)). Released paths are L1.

Native boundary. `scratch_native(fn, *args, **kwargs)` runs one native call under every policy and maps a failure through one closed classifier: by type only, never message text (companion plan: no EDQUOT string parsing), returning an enum, copying no text:

- resource-errno `OSError` (`_RESOURCE_ERRNOS`, `fileaccess/_errors.py:27`): rebuilt errno-only (`_resurface`, `:128`) carrying `report=QuotaReport(dimension="bytes")`, no errno field on the report;
- `duckdb.IOException`, and a pyarrow `OSError` whose `errno` is `None`: `FileAccessReadError("engine.scratch")` raised `from None`, carrying `report=QuotaReport(dimension="unknown", effective_bytes_cap=None, observed_bytes=None)`;
- memory failures: unchanged from `guarded` (`passes_by_type` for `MemoryError` and `duckdb.OutOfMemoryException`, `:100-110`; `resurface_scrubbed` for text-recognised ones, `:113-125`), so `is_memory_failure` (`:178`) still matches;
- anything else: `FileAccessReadError("engine.scratch")` `from None`, no report.

S1a ships the memory and "else" arms; the report arms land in S4a. Batch pulls use the same mapping over `guarded_iter` (`:172`). No new reason code; the sink aborts on any of them.

`scratch_native` key (V1-B): every module that calls a method on, or builds, a connection made by `connect_duckdb` or holding a released path. From `git grep -n "conn\.\(execute\|sql\|register\|fetch\)" -- src/decoy_engine/execution` on F06 (hits in `_pool_quality.py`, `_batch_join.py`, `_join.py`, `_multi_parent.py`, `_relation.py`, `_stream_join.py`, `_stream_join_plan.py` at `:63`, `:64`, `:73`, `:77`, `:120`; the `_payload_store.py:233` hit is a docstring) plus `_duckdb.py`'s `connect_internal` call (`:64`): `_relation.py`, `_multi_parent.py`, `_duckdb.py` (S2a-1c), `_batch_join.py`, `_join.py` (S2a-2), `_stream_join.py`, `_stream_join_plan.py` (S2b-2), `native/_pool_quality.py` (S4b). The sentry fails on a listed module calling a connection method outside `scratch_native`. `postgres_snapshot.py` already raises code-only errors `from None` (`:38-39`, `:239-242`).

### 4.2 Descriptor checks (the whole fail-closed default)

At open: `fstat` on the root descriptor must show `S_ISDIR`, `st_uid == geteuid()` and `S_IMODE == 0o700`; otherwise `ScratchPathUnsafe` (code-only), nothing changed. At `create_dir` and `create_file`: `O_NOFOLLOW` plus `O_EXCL` or `mkdirat`, then `fstat` of the new descriptor for type, owner and mode. No quota or fscrypt is required (T-PLAIN).

### 4.3 Producers

Per section 3. T17: if `T-DISCOVERY-SPILL` shows DuckDB writes outside the arena for an in-memory untrusted session, `untrusted_session` sets `temp_directory` to `arena.create_dir("duckdb").local_path()` before `lock_configuration` and its queries go through `scratch_native`. If no spill is possible with external access off, T17 records that as a tested fact, with no production change.

### 4.4 Sweep (S5)

`fileaccess.sweep_scratch() -> SweepReport`, no parameters. It scans only `tempfile.gettempdir()` at call time, the directory `ScratchArena.open()` uses with no `under=`. Called by the CLI or worker only (Q7). For each entry, opened by descriptor:

1. Only names matching `^decoy-arena-v1-[0-9a-f]{16}$` (never `decoy-arena-pending-*`, platform `decoy-job-*`, `decoy-preflight-*`, `decoy-src-fd-*`, engine `decoy-ooc-*`, `decoy-isolated-run-*`); a directory (`O_NOFOLLOW`), owned by `geteuid()`, mode 0700.
2. Ownership proof: `.decoy-arena`, opened by `dir_fd` `O_RDONLY|O_NOFOLLOW`, is a regular file owned by `geteuid()`, mode 0600, with the magic and a token equal to the entry's name token. Any failure in steps 1-2 is `retained_unknown`, never deleted.
3. `flock(LOCK_EX | LOCK_NB)` on the entry; `EWOULDBLOCK` is `retained_live`.
4. Lock held: `_rmtree_fd` the contents, `rmdir` the entry under the root descriptor; a failure is `failed`, left for the next run.

Pattern: `flock(2)` on the open file description, inherited across `fork`/`exec`; an ownership marker checked before any delete (`destructive-operations.md`, exact target). Pending-then-rename closes the `open` window; the lock-to-`rmdir` window is L6.

### 4.5 Not carried from `feat/f25-protected-scratch`

Unchanged from revision 3 (`98a72bb6` 4.5): only `scratch/_reports.py` (trimmed, as `fileaccess/_scratch_reports.py`) and `ScratchPathUnsafe` are ported.

## 5. Acceptance tests, red first

Each test is recorded red first, against its neutered control or F06 at the fail-before location (an `ImportError` is not a red); where F06 already makes a site 0600/0700, the red is the control and the mutation shows non-vacuity. Baselines from `uv.lock`.

| Test (file::name) | Scenario and observable result | Fail-before location (red reason) | Mutation that must turn it red | Slice |
|---|---|---|---|---|
| `tests/fileaccess/test_scratch_arena.py::test_modes_under_umask[000,002,022]` (T-ARENA-DESC) | Nested dirs 0700, files 0600, owner euid; `.decoy-arena` 0600 with the name's token | Control with A-4's `0o666` (`_output.py:222`) shows 0644/0666 | `0o600` changed to `0o644` | S1a |
| `...::test_refuses_unsafe_root[symlink,wrong_mode,not_dir]` | Symlink, 0755 or file root: `ScratchPathUnsafe`, disk unchanged | Control without the `fstat` checks opens it | Delete the mode comparison | S1a |
| `...::test_exit_paths` (T-ARENA-EXITS) | The 3.1.1 exits; primary error kept; failed reclaim is `RETAINED` | Control swallowing errors reports `REMOVED` | Return `REMOVED` on `OSError` | S1a |
| `...::test_arena_is_stagedir` | `isinstance(arena, StageDir)`; unmodified `parquet_writer(arena, ...)` and `write_text` write 0600 files; `.decoy-arena` refused as a component | Red (mypy) before the annotations widen | Drop the `strict` attribute; drop the reserved-name check | S1a |
| `...::test_local_path_policy` | `under=`: strict raises `FileAccessUnavailable`, ambient returns; engine-rooted returns under both | Refusing every strict release fails the `out_of_core_fk` golden (`test_policy_routes.py:69`) | Drop the `under=` ambient check | S1a |
| `tests/fileaccess/test_scratch_plain_host.py::test_jobs_succeed_without_quota_or_fscrypt` (T-PLAIN) | No `prjquota` or fscrypt: OOC both lanes (plain, declared) with forced spill, subset then mask, hold-back sink match F06's bytes. `[error_path]`: a non-resource pyarrow failure under ambient raises `FileAccessReadError("engine.scratch")` | Control whose `open` requires a quota fails | Quota precondition in `open`; `strict` from the policy | S1a, each later slice |
| `...::test_report_text_is_value_free[reports,errors,budget_message]` (T-REPORT-TEXT) | Sentinels (path, table, cell, DSN) absent from `str`/`repr` of reports and errors; the budget error over a sentinel `temp_dir` | `budget_message`: `_budget.py:539` prints `under {temp_dir}` | Add `detail=str(exc)`; restore the path | S1a, S4a, S5 |
| `tests/fileaccess/test_scratch_native.py::test_native_failure_text_is_value_free[connect,relation_copy,merged_copy,batch_join_read,join_read,stream_join_read,stream_join_plan_explain,spill,pool_quality]` | Seeded DuckDB failures with sentinels: `COPY` into a removed child, a truncated relation, spill into a read-only `temp_directory`; `EXPLAIN` over a dropped sentinel view; `connect_internal` patched to raise `duckdb.IOException` with the arena path and a sentinel. Each cell asserts the raw message holds the sentinel, then outward `str`/`repr` hold neither, ambient and strict | F06 re-raises unchanged (`f06:execution/out_of_core/_relation.py:521`, `_stream_join_plan.py:120`, `_duckdb.py:64`) | Call `conn.execute` bare at one site; drop `_stream_join_plan.py` from the key | S1a helper; `connect`, copies S2a-1c; joins S2a-2; stream S2b-2; pool S4b |
| `tests/sentry/test_file_access_boundary.py::test_local_path_callers_are_allowlisted` | Unlisted `local_path` caller, listed module with no call, bare connection call in a `scratch_native` module: each fails; `fa_fixtures` evasions (alias, `getattr`, bound method) caught | Fixture caller passes without the rule | Add a `local_path` call to `_stage.py` | S1b |
| `tests/sentry/test_scratch_arena_prefix.py::test_arena_prefix_used_only_by_open` | No `decoy-arena-` literal, `prefix=` or `.decoy-arena` outside `fileaccess/_scratch.py`; `open`'s name matches the sweep pattern | Seeded fixture passes without it | Add `prefix="decoy-arena-v1-x"` to `_isolated_run.py:367` | S1b |
| `tests/unit/declared_casting/test_staging_arena_budget.py::test_declared_ooc_one_arena_one_budget[ambient_temp_dir,no_temp_dir,strict_policy]` | Real `run_fk_out_of_core`, declared LazySource: typed copy C, forced spill about C, budget `1.5 * C`: `out_of_core_temp_disk_exceeded` with a `QuotaReport`, nothing committed, copy and spill under one arena root, arena reclaimed. `strict_policy` (budget `4 * C`): completes with F06's bytes. F06's `test_stream_staging.py:132`, `:153` keep their assertions; only the stub signature gains `_arena` (diff in evidence) | Control implementing rev 4's T01 (fresh child via `open(under=)`): `_runner.py:294` measures only the child, so the job completes over budget; strict is refused by `require_ambient` | Give the route a fresh child arena again | S2a-1 |
| `tests/execution/out_of_core/test_scratch_modes.py::test_ooc_scratch_modes[batch,reorder]` (T-MODES, T01-T05) | Umask 000, `os.mkdir`/`os.open` wrappers, pre-reclaim `fwalk`: dirs 0700, Python files 0600, both lanes with an outgoing FK edge, from the S2a-1 head (F06 makes reorder directories with `private_dir`, `_payload_store.py:147`, `_external_sort.py:215`) | Green on F06; red against a control with `private_dir` replaced by `Path.mkdir(parents=True)` | Same replacement in `_stage.py` | S2a-1 to S2b-2 |
| `...::test_caller_temp_dir[mode_untouched,missing_created]` | Caller `temp_dir` 0755 stays 0755 with a 0700 arena child; a missing one is created (ambient) | `f06:execution/out_of_core/_scratch.py:36` chmods it; `missing_created` green by design | Restore the chmod | S2a-1 |
| `...::test_adopted_arena_is_non_owning[reorder_spill,relation_survives_gc,relation_survives_gc_arena,reclaim_refused]` | Reorder spill into the missing leaf `_stream_join.py:530` names (made 0700); a relation built via the `Path` arm, and one in a `create_dir` child of an owning root, survives `del` plus `gc.collect()` of its arena object while the root is open, and the join reads it; adopted `reclaim()` raises | A finalizer on `_adopt` or a child deletes `*_key_relation.parquet` | Register a finalizer on `_adopt` or on children | S2a-1b |
| `...::test_ooc_internals_take_only_an_arena[emit,stager,relation,joiners,stream_table,connect_duckdb]` | Each rejects a `Path`; `_adopt` and arm (a)'s entry gone | Red by design while arms live | Restore a `Path` arm | S2b-3, S4b |
| `...::test_spool_and_sorter_modes` (T-MODES, T06-T08) | Spools, sorter runs, pairs 0600 under umask 000; RSS bound unchanged | Green on F06 (`_payload_store.py:99`, `_external_sort.py:382`); red against a `pa.OSFile(str(path), "wb")` control | Restore that writer | S2b-1, S2b-2, S4b |
| `tests/execution/test_isolated_payload_mode.py::test_payload_never_readable_by_others` (T-PAYLOAD) | Caller `staging_dir` 0755, umask 022: payload 0600 at first byte; work root, `sources/` 0700; sources 0600 | `_isolated_run.py:429` writes 0644, `:434` chmods later | Write then chmod | S3a |
| `...::test_child_holds_arena_after_parent_death` | Parent killed while the child writes: a `LOCK_EX|LOCK_NB` probe fails | Without `pass_fds` the probe succeeds | Drop the descriptor from `pass_fds` | S3a |
| `...::test_output_dir_commit_keeps_sibling_staging[other_dir,other_device]` | `output_dir` elsewhere and on `/dev/shm`: committed, staged beside it; `other_device` asserts differing `st_dev` first, never skipped (`.github/workflows/ci.yml:29`) | Green by design | Stage in the arena (`EXDEV`) | S3a |
| `tests/connectors/test_snapshot_spill_mode.py::test_spill_dir_created_0700` | Spill dir 0700 at creation, umask 022 (stub connection) | `f06:connectors/postgres_snapshot.py:217` creates 0755 then chmods | Restore `mkdir` then `chmod` | S3b |
| `tests/test_discovery_spill.py::test_discovery_spill_stays_in_arena` (T-DISCOVERY-SPILL) | Discovery query past a forced-low `memory_limit`: no DuckDB temp file outside the arena | Seeded control: no `temp_directory`, `memory_limit` forced low; expected red: a DuckDB temp directory in the working directory. Not run; a green control is recorded as "no spill" | Remove `temp_directory` (arena fix only) | S3b |
| `tests/execution/out_of_core/test_scratch_quota.py::test_budget_exceeded_commits_nothing` (T-A02a) | Tiny `temp_disk_budget_bytes`: `out_of_core_temp_disk_exceeded` with a `QuotaReport`; no final output | Red for `report`; "no output" green by design | Commit the sink before the budget check | S4a |
| `...::test_enospc_commits_nothing[T02,T03,T04,T05,T06,T07,T08,T11]` (T-A02b) | Python writers: injected `ENOSPC`/`EDQUOT` via a faulting file object over the real descriptor: `QuotaReport(dimension="bytes")`. DuckDB cells (T03 `COPY`, T04 merged `COPY`, T05 spill): `execute` raises an injected `duckdb.IOException("... No space left on device ...")` with a sentinel path: `QuotaReport(dimension="unknown")`, no sentinel outward. All: job fails, nothing committed; a text-recognised memory failure still gives `is_memory_failure(...) is True`. T17 cell only if 4.3 routes discovery through `scratch_native` | Rev 4's `scratch_native` gives a bare `FileAccessReadError`, no `report`; Python cells: bare `OSError` | Drop the `duckdb.IOException` arm; catch and continue the write | S4a (T08 S4b) |
| `tests/execution/test_crash_mid_spool.py::test_sigkill_mid_spool_commits_nothing` (T-A08) | Pause after the first spool batch, then SIGKILL: no final output; arena remains, lock released | Green by design (A-4 stage) | Publish the stage before spooling ends | S4a |
| `tests/fileaccess/test_publication_crash_matrix.py::test_crash_points[T09,T15,T16]` (T-A10) | SIGKILL before stage fsync, after the publish rename, after the parent fsync: old or complete new output | Green by design | Write directly to the final leaf | S4a |
| `tests/fileaccess/test_scratch_sweep.py::test_live_arena_survives` (T-A07) | Held arena: `retained_live`; two sweepers after the holder dies: one reaps, one reports; `foreign_exact_name_no_marker` (same UID, 0700, exact name, unrelated file, unlocked) and `marker_token_mismatch`: `retained_unknown`, bytes unchanged; `decoy-arena-v1-nothex`, a 0755 match, a foreign-UID entry (root only) untouched. Temp root set via `TMPDIR` and `tempfile.tempdir` | Rev 4's 4.4 steps 1-3 delete `foreign_exact_name_no_marker` | Skip the marker check; drop the `flock` probe | S5 |
| `...::test_sweep_takes_no_root` | Signature snapshot: `sweep_scratch()` has no parameters | Rev 4's `root=` parameter | Add `root=None` back | S5 |
| `...::test_sweep_in_open_window` | Sweeps at barriers after `mkdtemp`, after the marker write and after `LOCK_SH` in `open` leave the arena, which then works | Final-name-first control is reaped | Create the root directly under the final name | S5 |
| `...::test_reap_after_kill_and_resume_after_sweeper_crash` (T-A08-SWEEP) | Owner killed: sweep reaps; sweeper killed mid-delete: a second sweep finishes | No-retry control leaves a partial | Count a failed delete as reaped | S5 |
| Ratchet M-RATCHET | A `tempfile.mkdtemp` added in `_relation.py` fails A-4's boundary sentry | Existing; count drops per migrated site | Raise the registry count | each producer slice |

## 6. Class table (classes 1 to 10)

| # | Class | Applies | Closing structure | Test | Mutation |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: reports, errors, native and budget text reach `Job.log` | Field-free reports; code-only errors; `scratch_native` with a grep-defined key and sentry | T-REPORT-TEXT, T-PAYLOAD, native-text test | add `detail`; bare `conn.execute`; drop a key module |
| 2 | Cleanup on an exit path | Yes | Opener reclaims on each exit; finalizer on owning roots only; `RETAINED`; sweep only on marker-proven arenas | T-ARENA-EXITS, T-A07, T-A08, T-A08-SWEEP, `_adopt` cells | swallow a reclaim error; skip the marker check |
| 3 | State and concurrency | Yes: sweep vs owner | `flock` on the open file description, inherited, taken before the rename | T-A07, child-holds, open-window test | drop `LOCK_SH` or `pass_fds` |
| 4 | Route parity and silent fallback | Yes | One required arena handed across the declared boundary; one ambient check; no profile gate; every disk failure reported | T-PLAIN, T-MODES, one-arena-one-budget, T-A02b | fresh child under a handed arena; drop the `IOException` arm |
| 5 | Referential integrity | N/A: no row selection changes | Parity suites as characterization | FK parity suites | n/a |
| 6 | Untrusted producer | Partly: discovery SQL | Session locked after `temp_directory` | T-DISCOVERY-SPILL | remove `temp_directory` |
| 7 | Check then use on a path | Yes, bounded | Descriptor checks; `dir_fd` ops; `local_path` allowlist; marker read by `dir_fd`; fixed sweep root | T-ARENA-DESC, allowlist sentry, `test_sweep_takes_no_root` | delete a `fstat` check; restore `root=` |
| 8 | Weak tests and overclaimed evidence | Yes | "Not shown" column; neutered controls for F06-green sites; injected failures labelled | all | n/a |
| 9 | Refactor drift | Yes | Output parity per slice; named arms ended by a sentry; assertions kept | T-PLAIN, parity suites, internals test | restore a `Path` arm |
| 10 | Frozen contract drift | Yes | Additive exports; signatures, reasons, `CONTRACT_VERSION` unchanged | `test_file_access_entry_points.py` | rename an export |

## 7. Reuse inventory

- A-4 `StageDir`, `OutputTxn` (`_types.py:179-229`), `_output.py` helpers (`_call`, `_close`, `_rmtree_fd`), `validate_component`, adapters (`arrow.*`, `connect_internal`, `untrusted_session`), `guarded`/`guarded_iter`/`passes_by_type`/`resurface_scrubbed` (`_errors.py:100-172`), `require_ambient` (`_entry.py:115`), boundary sentry and registry.
- F06 `ScratchAccess` (moved) and the `PRIVATE_COPY` in-place `COPY` pattern (`f06:execution/out_of_core/_duckdb.py:21`) on arena methods; reason `out_of_core_temp_disk_exceeded` and `check_temp_disk_budget` (signature unchanged); `feat:` `scratch/_reports.py`, trimmed.

## 8. Frozen contracts touched

- `fileaccess` public surface: additive exports `ScratchArena`, `sweep_scratch` (no parameters), `QuotaReport`, `CleanupReport`, `SweepReport`, `ScratchPathUnsafe`. `CONTRACT_VERSION` 1, `ENTRY_POINTS` unchanged.
- Public entry-point signatures unchanged. `run_fk_out_of_core(temp_dir=...)`, `run_pipeline_isolated(staging_dir=...)` and the snapshot `temp_directory` keep their meaning, except that the engine makes a private child inside a caller directory instead of using or chmodding it; `output_dir` jobs still stage beside it (T12). `_arena` is a private keyword like F06's `_staged`. CHANGELOG entries in S2a-1 and S3a.
- Error class (pre-GA, stated): a non-resource library failure on scratch now raises `FileAccessReadError("engine.scratch")` under the ambient (CLI) policy too, where F06 raises the raw pyarrow or DuckDB error with its text. Resource-errno and memory failures keep their class. CHANGELOG entry in S1a; T-PLAIN `[error_path]`.
- Reason registry (Q4) and output bytes (T-PLAIN) unchanged.

## 9. Owner questions (recommended defaults first)

Q1 at-rest encryption: **not in V1 (owner).** Q2 kernel quotas: **optional V2 profile.** Q3 ledger and authority: **not carried;** the sweep's authority is marker, name, owner, mode and `flock`. Q4: **no new reason code.** Q5 fd-backed IPC writers: **median regression at most 5% on sorter-merge and payload-store benchmarks, five runs, fixed in S2b-0;** if missed, T06/T07 keep path writers (L1 widened). Q6 build base: **fresh `feat/f25-v1-scratch` from `origin/main` after F06 merges.** Q7: **`sweep_scratch()` called by the CLI or worker, never on import.** Q8 caller roots: **ambient-only, a fresh 0700 child, the caller directory's mode untouched.**

## 10. Slices

Bound: at most 10 files per slice counting production, tests, sentry data, docs and CHANGELOG, and 800 lines; estimates scale the `feat:` diffstat (`33ef629b..4c901d38`), evidence records the actual, and an over-bound slice is split, not waived. Sizes on F06: `_runner.py` 333 and `_rewrite_pass.py` 374 have room; `_relation.py` and `_batch_join.py` are 599 (past 600 needs a `test_module_size.py` census entry in the same slice, counted below); `_stream_join.py` 645 and `_isolated_run.py` 651 may only shrink. In the table, fa = `fileaccess/`, ooc = `execution/out_of_core/`, tests are `test_<name>.py`, both toml = `file_access_modules.toml` and `file_access_registry.toml`.

| Slice | Files (count) | Est. lines | Content | Gate |
|---|---|---|---|---|
| S1a | fa `_scratch.py` (new), `_scratch_reports.py` (new), fa `__init__.py`, `adapters/arrow.py`, `serial.py`, ooc `_scratch.py`; tests `scratch_arena`, `scratch_plain_host`, `scratch_native`; CHANGELOG (10) | 780 | Arena, marker, release rule, `scratch_native` (memory and else arms), `_adopt` | Dennis, then Codex |
| S1b | two sentry tests, `file_access_modules.toml`, `fa_fixtures` seeds (5) | 180 | Both keys, prefix sentry, evasions | Codex |
| S2a-1 | ooc `_scratch.py`, `_runner.py`, `_rewrite_pass.py`, `declared_casting/_stream.py`; tests `staging_arena_budget`, `stream_staging`, `scratch_modes`; both toml; CHANGELOG (10) | 560 | T01, T18 hand-over, `_ENGINE_ROOTS` deleted, arm (a) | Codex |
| S2a-1b | `_stage.py`, `_emit.py`, ooc `_scratch.py`; tests `scratch_modes`, `policy_scratch`; both toml (7) | 380 | Stager, emit (arm (d)); `scratch_access_for` deleted | Codex |
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
| S5 | `_scratch_sweep.py` (new), fa `_scratch.py`, `_scratch_reports.py`, fa `__init__.py`; test `scratch_sweep`; `docs/security/file-access-policy.md`; CHANGELOG (7) | 780 | Sweep, real subprocesses | Dennis, then Codex |

Transition (I2 suspended only here; four arms, each with its end). (a) `_rewrite_pass.py` passes `child.local_path()` to unconverted callees (until S2b-3), `_runner.py` to `stream_table` (until S2b-1), under a transitional `scratch_path_release` entry. (b) Relation builders and joiners keep a `Path` arm until S2b-3. (c) `connect_duckdb` keeps one until S4b (last caller `_pool_quality.py:246`). (d) `emit_to_sink` and `MaskedKeyStager` until S2b-3 (`_stream_driver.py:458-468` passes paths until S2b-1). Each arm goes through `_adopt(path)`: missing components `os.mkdir(..., 0o700)`, leaf opened `O_DIRECTORY | O_NOFOLLOW`, `fchmod` 0700 only if created here, then the 4.2 checks; non-owning (3.1.1), cleanup stays where it is (`_runner.py:323-327`, `_stream_driver.py:561`).

Existing-test migration. `git grep -n "temp_dir=" -- tests` on F06: 217 lines in 37 files, public `run_fk_out_of_core` calls included (they stay; Dennis counted about 83 internal sites in 18 files on main). Arms keep old tests green, so M1, M2 (before S2b-3) and M3 (before S4b) migrate at most 10 files each: a fixture builds the arena, assertions unchanged and shown, except `tests/native/test_pool_quality.py:314`, which compares with the child's `local_path()`.

Delivery and order. All slices land on `feat/f25-v1-scratch` (Q6), merged to `main` once after S4b with `test_ooc_internals_take_only_an_arena` green; S5 may follow. Order: S1a, S1b, S2a-1, S2a-1b, S2a-1c, S2a-2, S2b-0, S2b-1, S2b-2, M1, M2, S2b-3, S3a, S3b, S4a, M3, S4b, S5; S3a/S3b need only S1a and S1b (one writer per worktree); S4a needs S2b-3 and S3a; S5 needs S3a. Four units in 18 slices, about 7,100 lines (rev 4: 5,260 in ten); the rise is honest counting (tests, sentry data, CHANGELOG inside the bound, migration slices) plus the V1-A to V1-D cells.

Cross-repo note (Dennis M4). Platform `api/jobs/disk_quota_matrix.py:116-126` cites engine `_isolated_run.py:338` and `out_of_core/_runner.py:210`, and `tests/test_scheduler_s11_full_frame_local_stage.py:204-215` asserts each is a bare `mkdtemp(`. Neither is on engine main or F06 (platform pins 0.17.0); F25 moves those calls to `fileaccess/_scratch.py`, so the platform repin that takes F25 updates the cites (repin checklist).

Loop per slice: red first; baselines from `uv.lock`; each new mechanism mutated once, never committed mutated; lint, format, mypy, touched suites, `tests/sentry`, `tests/fileaccess`.

## 11. V2-LOG (listed, not planned) and deferred features

Revision 2's items stand (`937e88fe` section 11). Live here:

- [7] Same-UID interference in the sweep window between `flock` and `rmdir`, and a same-UID forged marker (L6): `fileaccess/_scratch_sweep.py` (new, S5).
- [7] DuckDB spill files and the isolated child's path writes take the umask mode inside the arena (L1): `f06:execution/out_of_core/_duckdb.py:46`, `f06:execution/_isolated_worker.py:133`.
- [2] An empty `decoy-arena-pending-*` directory left by a crash inside `open` is never reaped (L4): `fileaccess/_scratch.py` (new, S1a).
- [1] The `QuotaReport(dimension="unknown")` arm also fires for errno-less I/O failures that are not disk exhaustion (a truncated file): true but coarse (L3): `fileaccess/_scratch.py` (new, S4a).

Deferred features (not defects): fscrypt (Q1), the kernel project-quota provider (Q2), the ledger and ownership authority (Q3), checkpoint and resume (umbrella slice 6), platform lock-authority integration (umbrella section 4).

## 12. Pre-mortem

Revision 4's five predicted findings are now tests in section 5 (T-A07 and the prefix sentry; `test_local_path_policy`; the `output_dir` cells; strict-policy T-MODES and T-PLAIN; `test_native_failure_text_is_value_free` and `test_adopted_arena_is_non_owning`). The likely new one, "F06 moves again before F25 builds", is answered by the re-verification item in section 13.

## 13. What revision 5 did not verify

- Re-verify every `f06:` anchor against `main` when F06 merges (after R-b and B-6), including the outcome of the F06 umask 0777 fix: if `scratch_root` moves to `private_dir`, `test_caller_temp_dir[mode_untouched]`'s fail-before (`_scratch.py:36` chmod) may vanish and the cell becomes green by design with the chmod as its mutation.
- `plan-lint plan` printed OK; nothing else was run. No red or green is claimed; line counts and slice sizes are estimates. T-DISCOVERY-SPILL's probe was not run.
- The internal-callee share of the 217 `temp_dir=` test lines was not re-counted on F06. Platform anchors in 4.4 (`decoy-job-*` and peers) are from revision 3; the section 10 cross-repo cites were read on platform `origin/main`.
- `/dev/shm` versus `/tmp` `st_dev` on `ubuntu-latest`; `flock` on overlayfs and across a rename (from `flock(2)`); the snapshot stub; T-A02b's injected failures.

## 14. Provenance

Revisions 1 `affde556`, 2 `937e88fe`, 3 `98a72bb6`, 4 `0aa5c873`. Next gates: Dennis, then Codex (at most two rounds). No build before owner approval.

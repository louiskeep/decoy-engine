Status: review

# F25 Slice 4c plan: cross-model hostile review (Opus)

Plan: `docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md` at `c1a46d4` (branch `plan/f25-4c-consumer-capabilities`). Code baseline: `4c901d38` (`feat/f25-protected-scratch` head, which is the plan's stated amendment baseline). Contract: `DEFECT-CATALOG.md` and `HOSTILE-PLAN-REVIEW.md` on decoy-platform `handoffs/queue`. This review carries no authority; the orchestrator re-verifies.

## Verdict: NO-GO for build as written (revise, do not reframe)

The consumer-capability design itself (fd-owning handles, `ScratchDirEntry`, fd-backed writers, `NativePathLease`) is sound, and my probes support its two key technical bets. Three BLOCKERs are design contradictions, not wording: the SQLite profile breaks multi-process ledger access, the strict `run_pipeline` cutover cannot be built or tested before 4b, and the isolated child has no way to allocate under the parent's attempt. Each needs one author remediation that changes units 4c-5a, 4c-6a/6b and 4c-8a. None requires a new plan.

Counts: **3 BLOCKER, 5 HIGH, 6 MEDIUM, 3 LOW.**

## What I ran, and what I did not

* Lock environment: `uv sync --frozen` in a worktree of the plan branch gave Python 3.11.17, DuckDB 1.5.4, SQLite 3.45.1, PyArrow 24.0.0, Polars 1.42.0. Probes P1-P4 below ran there. One earlier SQLite probe (P0) ran on system Python 3.13 / SQLite 3.45.1, outside the lock; I use it only for the fd-delta observation and say so.
* `plan-lint plan` on the plan: `OK`. Mechanical anchor check: 269 `file:line` citations; all resolve within file bounds (two bare `_runner.py` anchors are ambiguous by basename only).
* Not run: any engine test suite, the real-kernel `decoy-f25-dev` host, project quotas, fscrypt, throughput measurement, or strace of DuckDB. No probe ran as a different UID.

Probes (disposable fixtures in my scratchpad, no engine code):

* **P1 SQLite** (lock env): `sqlite3.connect("file:/proc/self/fd/<fd>/ledger.db?mode=rw", uri=True)` after the parent directory was renamed and replaced wrote into the renamed (pinned) directory. strace shows SQLite resolves the magic link and then calls `openat(AT_FDCWD, "<ambient>/ledger.db", ...)`, and opens and stats `ledger.db-journal` by ambient path with `AT_FDCWD`. A second connection to the same file got `database is locked` once the first held `locking_mode=EXCLUSIVE`.
* **P2 DuckDB** (lock env): `SET temp_directory='/proc/self/fd/<fd>'` plus `COPY ... TO '/proc/self/fd/<fd>/o.parquet'` after the directory was renamed and replaced: all eight spill files and the COPY output landed in the renamed (pinned) directory; the ambient replacement stayed empty. DuckDB does not canonicalize on this build.
* **P3 DuckDB mode** (lock env, umask 022): the COPY output was mode `644`, `st_nlink` 1.
* **P4 writers** (lock env): Polars `write_parquet` and PyArrow `ParquetWriter` both wrote into `io.FileIO(fd, "wb", closefd=False)` in 0.1 to 0.4 MB chunks and neither closed the wrapper; the raw fd stayed valid.
* **P0** (system Python 3.13, not the lock): a `/proc/self/fd` listing snapshot taken with `os.listdir` reported an empty delta after `connect`, because the SQLite main fd reused the number that the listing's own transient fd had in the "before" snapshot.

## 1. Grounding (more than 25 citations checked by hand)

Accurate (opened and matched to the claim): `_seam.py:137-143` (`ensure_scratch` returns `LocalArena`), `:146-167` (`as_native_dir` builds `_ADOPTED`), `:199-225`, `:228-253` (`ensure_publication_file` returns `stage / child`), `:256-326`; `_pipeline_route_exec.py:414-430` (no arena passed); `physical/drivers/_out_of_core.py:66-80`; `_isolated_worker.py:217-218` (sink with no arena), `:240-262`; `_isolated_run.py:220`, `:242`, `:276`, `:298`, `:318`, `:471-488` (existing `pass_fds` capability pipe), `:514`, `:532`, `:544`, `:593`, `:609`, `:666`, `:670`; `_runner.py:261`, `:298`, `:318`, `:329`, `:334`; `_transactional_sink.py:328`; `_chunked_output_sink.py:592`; `subset/_api.py:195`, `:223`, `:224`; `postgres_snapshot.py:231` (`temp_directory: str(spill_dir)`), `:575`; `quarantine.py:625`, `:642`; `_fixed_width_writer.py:373`; `_ledger.py:46` (`FORMAT_VERSION = 1`), `:153`, `:177`, `:213`; `_policy.py:24`, `:58`; `_errors.py:23`; `_reports.py:31/49/67/92/109/119` (dataclass decorators one line above each class); `_authority.py:50`; `vault.py:81`, `:128`; `keyprovider.py:123`; `relationships/_graph.py:70`; `config/_relationships.py:24`, `:46`; `tests/property/test_fk_keys_invariants.py:659`; `tests/scratch/test_fd_only_filesystem_sentry.py:26`, `:84-101` (function-wide allowlist incl. `_ledger.py` `bootstrap`, `_verify_single_device`); `tests/scratch/test_f25_slice4_migrated_fs.py:173`, `:201` (`evidence in ("errno", "provider_quota")`); `_isolated_common.py:266`, `:351`; `_adapter.py:41`; `physical/_reasons.py:95`; `_pipeline_routing.py:312`; `_pipeline.py:313`; `progress.py:264`; `internal/logging.py:86`; `docs/security/error-reporting-and-data-exposure.md:25`.

Wrong, phantom, or missing:

* **Phantom entrypoints** (2A.5 para 1, C01, 4c-8a): `materialize_subset` is the internal helper (`subset/_materialize.py:37`); the public API is `run_subset` (`subset/_api.py:158`, exported `subset/__init__.py:15`). `discover_data` does not exist; the export is `run_discovery_sql` (`data_discovery.py:160`, `__init__.py:74`). `postgres_snapshot` is a module; the function is `snapshot_tables_to_parquet` (`postgres_snapshot.py:459`). See HIGH-1.
* **Missing public writers** with `scratch_arena: ArenaLike | None = None`: `write_fixed_width` (`_fixed_width_writer.py:196-201`, exported `__init__.py:167`) and the quarantine finalizer (`quarantine.py:590`). See HIGH-1.
* **Phantom prerequisite**: `liveness_lock_fd` in the 4c-5a `pass_fds` tuple (2A.5 para 3). No liveness lock exists in code; it appears only in docstrings (`_capabilities.py:63`, `_manager.py:170`, `_ledger.py:136`), and the worker comment defers it (`_isolated_worker.py:254-258`). See BLOCKER-3.
* **Phantom prerequisite**: "the configured `ScratchManager`" (2A.5 para 1). `context.py`, `config/`, and `execution/_pipeline.py` contain no `ScratchManager` or scratch configuration. See BLOCKER-2.
* **Mischaracterized defect** C20: both reclaim implementations use `os.listdir(fd)` (`_local_provider.py:316`, `_arena.py:664`), not `os.scandir`; there is no raw `DirEntry` to escape. See MEDIUM-1.
* `_arena.py:444` is labeled "absent record/target success" but is `resource_id = resource.resource_id`; the branch is a few lines below. LOW.
* Enumerations: exit paths, routes, FK shapes, and fields match code for the **existing** owners. Two gaps: exit paths of the **new** 4c owners are not enumerated (MEDIUM-3), and the public entrypoint list is wrong (HIGH-1).

## 2. Findings

### BLOCKER-1. The SQLite profile makes the pool ledger single-process (classes 3 and 10)

* Plan: 2A.3 para 3 (`PRAGMA main.locking_mode=EXCLUSIVE`), 4c-6a/6b.
* Evidence: the ledger is designed for many processes per pool: `_ledger.py:12-14` (`BEGIN IMMEDIATE` "so ... two processes cannot both believe they hold the row"), `_ledger.py:23-24` ("One durable control database per managed pool ... not one independent authority per process"), current `journal_mode = DELETE` with `busy_timeout` (`_ledger.py:187-195`); plan line 153 ("short `BEGIN IMMEDIATE` transactions"), A01 (two concurrent attempts), A07 (second sweeper). P1: a second connection gets `database is locked` while the first holds EXCLUSIVE.
* Failure: job A's manager opens the pool ledger and holds the exclusive lock for its connection lifetime. Job B on the same pool, the Slice 5 sweeper, or the isolated child (BLOCKER-3) waits `busy_timeout` (5 s) and then fails every allocation. Mixed binaries during rollout (old shared-locking, new exclusive) deadlock the same way; plan 1.13 treats only schema/state changes as versioned.
* Fix: keep normal locking and short transactions; pin identity per transaction instead (open, verify main/journal identity, `BEGIN IMMEDIATE`, verify, commit, verify). Add RED: two processes allocate concurrently on one pool under the 4c profile. Declare the locking protocol a frozen ledger contract.

### BLOCKER-2. The strict `run_pipeline` cutover in 4c-8a cannot be built or tested in this slice order (classes 4, 8, 10)

* Plan: 2A.5 paras 1 and 4, 4c-8a, Slice 4b gate (line 615: "No release flips the default production write path onto a managed-but-unencrypted arena").
* Evidence: no scratch configuration surface exists (see grounding). The provider qualifies with `encryption_verified=False` (`_local_provider.py:137`), and the only profile is "admissible only once Slice 4 wires encryption" (`_policy.py:28`). No CI workflow provisions a quota volume (`.github/workflows/*.yml` has no quota/ext4 job). 144 test files call `run_pipeline(`; 2A.5 para 4 forbids `LocalArena` from reaching production entrypoints.
* Failure: after 4c-8a either (a) every `run_pipeline` test fails closed on GitHub runners, so the worker must skip tests (forbidden) or let the test context reach production (forbidden); or (b) a managed-plaintext profile qualifies and becomes the production default, which the 4b gate forbids. Either is a third-round finding.
* Fix: 4c adds a required injected `scratch_arena` on all internal producers and an explicit (not configured) `run_pipeline` arena parameter. The default flip to fail-closed moves to the 4b/6 activation slice. Define the CI story: a named test provider admitted only by a test-only entry point that the reachability sentry proves unreachable from shipped entrypoints.

### BLOCKER-3. The isolated child has no way to allocate under the parent's attempt (classes 3 and 4)

* Plan: 2A.5 paras 1 and 3, 4c-5a, T11 (line 72: "never a fresh independent quota").
* Evidence: the child runs `run_pipeline` (`_isolated_worker.py:218`). Under 2A.5 para 1, `run_pipeline` opens its own attempt, which would be a second quota. The `pass_fds` tuple carries directory handles, a phantom liveness lock, and a bootstrap pipe, but no ledger or allocation channel. Under BLOCKER-1 the child could not open the ledger anyway.
* Failure: any route inside the child that allocates (OOC relation dirs, DuckDB spill, sorter runs, hold-back) either opens a second attempt or writes unregistered bytes under the inherited `work_root`. Both violate T11 and A03, and no C12/C13 RED test exercises a child-side allocation.
* Fix: specify child allocation (parent-brokered allocation over the bootstrap channel, or a pre-registered child sub-arena with its own ledger rows). Drop `liveness_lock_fd` from 4c or move it with Slice 5. Add RED: forced OOC spill inside the isolated child must be charged to the parent attempt with ledger rows before the first write.

### HIGH-1. Wrong and incomplete public-entrypoint inventory (classes 4 and 8; contract section 2.3)

* Plan: 2A.5 paras 1-2, C01 RED, 4c-8a, slice table line 523.
* Evidence: see grounding. Real callers that pass no arena: `decoy/src/decoy/cli/subset.py:247` (`run_subset`), `decoy-platform/api/files/_pg_pull_child.py:88` (`snapshot_tables_to_parquet`), `decoy-platform/api/data_tables/router.py:202` (`run_discovery_sql`), `decoy-platform/api/jobs/fixed_width_output.py:223` (`write_fixed_width`). The plan contradicts itself: 4c-8a migrates "engine, CLI, and platform callers", but line 523 says external caller work gets "a separately sized owning-repo plan".
* Failure: the C01 RED test calls `discover_data(...)` and goes red with `ImportError`, which is the wrong reason (vacuous RED). `write_fixed_width` and the quarantine finalizer keep `None` defaults, so fail-open survives. Or they change, and the platform breaks with no caller migration gate.
* Fix: enumerate every exported symbol that reaches `ensure_*`/`allocate_*` from `__init__.py` and `subset/__init__.py`, with real names. Generate the C01 test from that list (a field walk over exports). Decide one owner for cross-repo migration.

### HIGH-2. `adopt_created_child` requires mode 0600, and DuckDB creates 0644 (classes 6, 7, 8)

* Plan: 2A.3 para 7 ("the expected 0600 mode"), C04/C05/C09/C14, 4c-4b, 4c-5b.
* Evidence: P3, DuckDB 1.5.4 under umask 022 creates the COPY output as `644`. The only umask control in the engine is a process-global flip during ledger bootstrap (`_ledger.py:185-191`).
* Failure: every T03/T04/T08/T13 COPY fails closed on a default-umask host. The worker either weakens the mode assertion (forbidden), or flips the process umask around the query, which races Arrow/DuckDB threads creating other files (the same latent race exists at `_ledger.py:185`).
* Fix: the adoption contract is "no group/other write bits and owner == euid", followed by an fd `fchmod(0o600)` after identity pinning. Forbid `os.umask` outside single-threaded bootstrap, with a sentry rule. Add RED: run COPY under umask 022 and 002.

### HIGH-3. The C19 SQLite witnesses cannot go RED as specified, and the hot journal is read before it is checked (classes 3, 7, 8)

* Plan: 2A.3 paras 5-6, C19, 4c-6a.
* Evidence: P1. Resolving the magic link yields the current name of the pinned inode, so a parent swap before `connect` is followed, not escaped. The real window is inside `sqlite3_open` between resolution and `openat(AT_FDCWD, ...)`, and a Python pause hook cannot reach it. SQLite stats and opens `-journal` by ambient path on the first lock, and can replay a hot journal into the main file during the first read, before the plan's per-write-transaction journal check. P0 (outside the lock): an fd-number delta based on `listdir` can be empty because of number reuse.
* Failure: the worker cannot produce the mandated fail-before for "replace the control-directory path between bootstrap and connect". Under the plan's rule ("a failure returns to plan review"), that alone forces a third round. A planted or stale journal is replayed before verification.
* Fix: identify the SQLite fds by scanning all fds for `fstat == pinned (st_dev, st_ino)` (no delta). Verify the journal's absence or pinned identity before the first statement that reads. Specify the RED interposition (test-only `LD_PRELOAD` shim or ptrace pause on `openat`), or downgrade C19 to detect-only with a written residual and no swap-RED requirement.

### HIGH-4. Collision with the F31 plan; no F31 assumptions table (classes 1, 9, 10)

* Plan: 1.10 row 1, 2A.5 value-free bullets, 4c-5a.
* Evidence: F31 (`origin/plan/f31-safe-output`, `docs/plans/2026-10-30-f31-shared-safe-output-boundary.md:466`) allows only `type(exc)`, `isinstance`, re-raise, or boundary handoff for a caught exception. F25 classification reads `exc.errno` inside handlers (`_arena.py:506`, `_budget.py:566`, `_materialize.py:97`), and the 4c T14 fix keeps "records only the typed classification while handling". F31 Slice 3 (same doc, `:573-583`) rewrites `_isolated_worker.py` bootstrap and inherited descriptors (non-inheritable channel, `dup2` of fds 1/2), which is the same file and fd set as 4c-5a. The 4c "exception type" log field (2A.5) has no rule against reading `__name__`, which F31 forbids (`:39`, `:327`). F31 itself defers to the F25 4c amendment (`:164`).
* Failure: whichever lands second fails the other's sentry or conflicts in `_isolated_worker.py`; the quarantine log leaks a dynamic class name (catalog class 1, STORM instance).
* Fix: add the F31 assumptions table. Classify errno through one named boundary function that F31's manifest admits. Use F31's closed exception-type table. Pick one owner for the child bootstrap and order 4c-5a relative to F31 Slice 3.

### HIGH-5. Touched modules are at the size ceiling; there is no decomposition unit (class 9)

* Plan: 1.12 module-size row ("new 4c modules stay within it").
* Evidence (`tests/sentry/test_module_size.py` census and `wc -l`): `_runner.py` 699 (MAX 700), `_stream_join.py` 709 (legacy, shrink-only), `quarantine.py` 686, `_isolated_run.py` 675, `_arena.py` 677, `_pipeline_route_exec.py` 599, `_chunked_output_sink.py` 594.
* Failure: 4c-4a (OOC), 4c-5a (`pass_fds` bootstrap), and 4c-8a (arena threading) must grow these. The size sentry goes red mid-unit, and the worker does an unplanned split inside a security change (refactor drift), or bumps the census.
* Fix: add a 4c-pre unit of pure moves with characterization tests and no behavior change (the census comments already name the splits: `_isolated_spawn.py`, `_quota_report.py`, `_disk_budget.py`), gated before 4c-4a.

### MEDIUM-1. The C20 RED leg is vacuous; the real reclaim defects are under-specified (class 8)

`_delete_tree_fd` and `_clear_owned_dir_children` use `os.listdir(fd)` (`_local_provider.py:316`, `_arena.py:664`). A "raw-`DirEntry.path` substitution" cannot fail before the change. The real gaps are: no `fstat(sub) == lstat` comparison after the open at `_local_provider.py:326`, no recheck before `rmdir` (`:336`), and errors swallowed by `continue` with no retained charge (`:319`, `:327`, `:337`, `:343`). Retarget the RED legs to these.

### MEDIUM-2. The `rmdir` overclaim (classes 7 and 8)

`rmdir(name, dir_fd=...)` still resolves by name after the pre-`rmdir` recheck. The post-success `st_nlink == 0` check detects a swap only after the replacement is already removed. 2A.6 para 2 ("no attempt to delete a replacement name") is therefore false in the race. Restate it as detect-and-retain, bounded to an empty same-owner directory.

### MEDIUM-3. No exit-path rows for the new 4c owners (class 2)

1.11 enumerates existing owners only. Missing: the `ScratchDirectoryHandle` fd on exception inside a `with`; a `NativePathLease` closed while a DuckDB connection object is still alive (GC-delayed close, so a later spill resolves a recycled fd number); the ledger control fd on bootstrap failure; and the parent copies of `pass_fds` between `Popen` and child validation failure. The C06 neuter covers only "close the lease before DuckDB starts". Add these to the generated exit matrix, including "lease outlives connection" ordering.

### MEDIUM-4. A literal-`getattr` silent fallback survives the planned sentry (class 4)

`ensure_publication_stage` and `ensure_publication_file` call `getattr(arena, "allocate_publication", None)` (`_seam.py:246`, `:277`). Any arena object lacking the method silently takes `tempfile.mkdtemp`. 2A.4 para 4 treats literal `getattr` as resolved and allowed, so this shape passes. Require exact type checks on the qualified branch and add a sentry seed for capability-presence `getattr`/`hasattr` fallbacks.

### MEDIUM-5. No performance guard in 4c (class 8)

T06/T07 move from native `pa.OSFile(path)` to Python-backed file objects. P4 shows 784 Python `write` calls for a 3M-row Parquet write, but I did not measure throughput. A14 is scoped to 4b. Add a before/after sort and payload benchmark to 4c-4a, with a budget fixed before the change. Otherwise a reviewer reports a regression with no agreed baseline.

### MEDIUM-6. The round budget is undefined across 14 units (class 8)

The catalog allows two build remediation rounds. 4c gives each of the 14 units its own adversarial review and remediation, then Codex, argon, and Dennis on the final commit. State whether "two rounds" is per unit or per slice, and which reviewer's BLOCKER stops the line.

### LOW-1. Anchor drift

The `_arena.py:444` label is off by a few lines, and the report anchors point at decorators. `origin/main` is one commit ahead of the merge base; rebase before 4c-0a.

### LOW-2. Bare basename anchors

Two bare `_runner.py:207`/`:271` anchors (lines 62, 552) are ambiguous by basename. Qualify the path.

### LOW-3. Owner contradiction for documentation

"Barry reconciles" (4c docs paragraph) versus "Documentation is part of 4c-8". Name the unit.

## 3. Catalog walk (case the plan does not close)

| # | Closed? | Open case |
|---|---|---|
| 1 | Partly, deferred to absent F31 | `exc.errno` reads and the "exception type" log field conflict with F31 (HIGH-4). `QuotaReport.errno` stays a public dataclass field, so "excluded from public serialization" has no serializer to enforce it until F31 lands. |
| 2 | Existing owners only | New handle, lease, ledger and `pass_fds` owners (MEDIUM-3). |
| 3 | File-handle state machine is good | EXCLUSIVE ledger (BLOCKER-1), hot-journal ordering (HIGH-3), global umask (HIGH-2). |
| 4 | Route matrix is good | Wrong entrypoints (HIGH-1), `getattr` fallback (MEDIUM-4), child allocation route (BLOCKER-3). |
| 5 | N/A justified; FK shapes grounded | None found. |
| 6 | Envelope schema is good | Created-child mode contract is wrong (HIGH-2). |
| 7 | Core design is sound; P2/P4 support it | SQLite window inside `open` (HIGH-3); `rmdir` by name (MEDIUM-2). |
| 8 | Rules are present | Vacuous REDs: C01 names, C19 swap, C20 `DirEntry` (HIGH-1, HIGH-3, MEDIUM-1). |
| 9 | Characterization is required | Size ceilings (HIGH-5); shared `_isolated_worker.py` with F31 (HIGH-4). |
| 10 | Listed | Ledger locking protocol unlisted (BLOCKER-1); public API break named against wrong symbols (HIGH-1). |

## 4. Ten ways a gate reviewer breaks it once built

| # | Break | RED-first test in plan? |
|---|---|---|
| 1 | Run two jobs (or job plus sweeper) on one pool: the second blocks on the EXCLUSIVE ledger. | No |
| 2 | Isolated run that routes OOC inside the child: second quota or unregistered spill. | No |
| 3 | Run the existing suite on a GitHub runner after 4c-8a: everything fails closed, or the test context reaches production. | No |
| 4 | Call `run_subset`, `run_discovery_sql`, `snapshot_tables_to_parquet`, `write_fixed_width` with no arena: still fail-open, or the platform breaks. | Partial (wrong names) |
| 5 | DuckDB COPY under umask 022: adoption rejects every output, or the check was loosened. | No |
| 6 | Plant a journal beside `ledger.db` before restart: replayed before verification. | No |
| 7 | Run the F31 sentry over F25 code, or merge F31 Slice 3: `exc.errno` violations and a worker-bootstrap conflict. | No |
| 8 | Size sentry red on `_runner.py`/`_isolated_run.py`/`_stream_join.py` growth. | Sentry exists; no planned split |
| 9 | Drop the lease while a GC-delayed DuckDB connection still spills: recycled fd number. | Partial (C06 covers only pre-start close) |
| 10 | Arena double without `allocate_publication`: silent `mkdtemp` publication. | Partial (C00 helper sweep only if the double is included) |

## 5. Frozen contracts, F31, sizing, slice order, separability

* Frozen: the public break must name the real symbols (HIGH-1). The ledger locking/journal protocol is a cross-process contract and needs listing (BLOCKER-1). Isolated envelope: private and fine.
* F31: the plan assumes F31 absorbs the typed projection, but F31 forbids the errno access the projection depends on and also owns the child bootstrap (HIGH-4).
* Sizing: 14 units are reasonable, but add the HIGH-5 decomposition unit.
* Order: 4c-6 (ledger) must precede 4c-5a (child allocation depends on it). 4c-8a activation moves after 4b (BLOCKER-2). The liveness lock leaves 4c.
* Separability: 4c is **not** separable as a review unit from the 67-file, 9103-line ungated base. C02-C18 rewrite code that Slices 2-4a introduced on the same branch (for example the `_seam.py` helpers 4c deletes). Gating the base and then rewriting it wastes a gate. Recommend one integration branch: gate the base once, marked "internal, consumer claims superseded", then gate the 4c units as stacked diffs. 4c is separable from 4b/5/6 only after the BLOCKER-2 and BLOCKER-3 fixes.

## 6. Can a remote worker build it in two remediation rounds?

Not as written: BLOCKER-1 to BLOCKER-3 surface in round 1 and change design. With the fixes above, plausibly yes for 4c-0a through 4c-4a and 4c-7. What would cause a third round: the SQLite residual (HIGH-3) on the real host's SQLite build, F31 ordering (HIGH-4), cross-repo caller migration without a named owner (HIGH-1), and unplanned module splits (HIGH-5).

## 7. Self-attack of this review

* BLOCKER-1 counter: "one manager process per pool, with children brokered". The plan never says so, and `_ledger.py:12-24`, line 153 and A07 assume multiple processes. Holds.
* BLOCKER-2 counter: "qualify a test provider on tmpfs". `qualify` requires project-quota mount attributes (`_local_provider.py:118-137`), and 2A.5 forbids a test context in production entrypoints. Holds.
* HIGH-2 counter: "the 0700 parent protects the file". Parent mode does not change the created file's mode bits; the plan's check is on the file. Holds.
* HIGH-3 counter: "the deployed SQLite may differ". True. P1 is the lock env's bundled 3.45.1; `decoy-f25-dev` is unverified, which is why the RED mechanism must be specified rather than assumed.
* P2 caution: DuckDB fd-anchoring holds on 1.5.4 here. A version bump must rerun the probe, which the plan already requires.

## 8. Plan bloat and the five sections that carry the load

Load-bearing for the 4c worker: 2A.2 (census), 2A.3 (contract), 2A.5 (fail-closed and transport), 2A.6 (RED table), and the Slice 4c unit table (lines 570-590).

## What to cut

1. "Rules consulted" list (line 13) and section 1.4 survey: provenance, not build input. Keep one link.
2. Sections 1.6-1.8, A05-A11, and the A14 benchmark protocol: these belong to 4b/5/6. Move them to a separate 4b/5 document, or mark them "not 4c".
3. Slice 0-3, 4b, 5, 6 bodies: summarize as one status table; the base is built.
4. 2A.7 argon closure table: it restates 2A.3-2A.6. Keep a one-line pointer.
5. Section 5 historical Gemini/Dennis provenance: move to the review record.
6. The `os.dup` adapter branch: P4 shows neither pinned library closes the wrapper. Keep it only as "if a probe fails".
7. The DuckDB 0700 pre/post residual branch: P2 shows fd-anchoring works on the pinned build. Make it "fail the workload closed if the probe fails" and delete the residual mechanism.
8. The `/proc/self/fd` delta mechanism: replace it with an identity scan (HIGH-3). It is shorter and not number-reuse fragile.
9. The 1.14 pre-mortem: it duplicates the 1.10 table.

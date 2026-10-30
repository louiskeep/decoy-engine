Status: proposed (re-plan, revision 1 after the Codex hostile review `review/f22-replan-codex:docs/reviews/2026-10-30-f22-replan-codex-review.md`)

Summary: keep the F22 publication backend; restructure ownership (one attempt arena, fd-held, with a safe-parent precondition), the sink lifecycle (one exhaustive transition table including RECOVERING and restart) and reporting (report types that are value-free by construction, so every serialization is safe, plus a closed list of engine-owned emitters); rebuild on F29 in five linearly gateable slices.

Baseline: F22 `2fa4f8a5` (read-only). F29 `main` at `fdd08a8`. Merge base `1b3a922`.

Rules consulted: `DEFECT-CATALOG.md` sections 2 to 5 and `HOSTILE-PLAN-REVIEW.md` (decoy-platform `handoffs/queue`); dev-rules `00-universal.md`, `testing.md`, `security.md`, `observability-and-resilience.md` (log content rule), `refactoring.md`, `api-and-compatibility.md`, `documentation.md` (by title and the review-checklist line `code-review.md:47`; not re-read in full); engine `CLAUDE.md` (600/700 census, pre-GA hard delete, established methodology); PEP 578 (audit hooks); POSIX `renameat`, `unlinkat`, sticky-bit rename rule.

Grounding: `file:line` is at `2fa4f8a5` unless prefixed `main:`. Probes run from `uv.lock` (`uv sync --frozen --extra dev`, Python 3.11.17, DuckDB 1.5.4, PyArrow 24.0.0), one worktree per commit, results in section 0. Not run: the real-child kill (Codex's K2 leftover is taken as reported), the full test suites, any mutation.

# F22 publication: re-plan (revision 1)

## 0. Review dispositions

| Finding | Disposition | Where |
|---|---|---|
| B1 `to_report()` is not a boundary | Fixed. `to_report()` deleted. Report types are value-free by construction (exact-type field validation), so raw object, `repr`, `asdict`, `json`, `pickle` are safe; unsafe fields moved to named data carriers; a closed emitter list is the boundary. Reproduced: `exception_error_text` returns a dynamic class name `CELLMARK_secret_value`; F22's own `os_error_label` returns `CELLMARK_x/ENOSPC`; `approve_ref("TBLMARK_55d0")` passes; `asdict(AbortReport)` holds `PATHMARK` paths; retention `meta.json` holds `operation_id="/srv/PATHMARK_op"`. | 2 P6, 5.3, 7.2 |
| B2 producers create paths outside the arena | Fixed. Pointer temp, backup and restore stages move into the arena; retention is an explicit ownership transfer by rename; the containment proof is runtime (PEP 578 audit hook plus tree walks), not a `tempfile` sentry. Reproduced: an injected pointer-temp failure leaves `generations/<d>` and `CURRENT.<uuid>.tmp` under the dataset root. Renamed-arena attack closed by a safe-parent precondition plus fd-relative disposal; residual named. | 2 P1 to P3, 5.1, 7.3 A5, A6 |
| B3 RECOVERING incomplete and contradictory | Fixed. One exhaustive table: idle ops per state plus in-flight exits `(prior, outcome) -> final, pending abort`; validation during recovery is busy and never mutates; found binding seals from any prior; restart is the OPEN row. Reproduced at `2fa4f8a5`: validation FAIL accepted during a blocked lookup, lookup then returns `already_committed`; abort during lookup sets `_aborted` and lookup still returns `already_committed`; a fresh sink reconciles and then accepts writes; a commit after a non-refusal `OSError` publishes. | 5.2, 7.2 P4, P5 |
| H4 enumerations incomplete | Fixed: arena, work-root, payload, retention, reader-hold, recovery and disposal exits; eight routes plus three sink variants; every field of every public type; emitters incl. repr, traceback, logs, evidence. | 4 |
| H5 five vacuous proofs, K5 and K6 | Fixed per class row (section 3); oracle twins replace in-test non-vacuity asserts. K5 accepted: green at baseline (prep rename lands at the leased path), now characterization. K6 accepted: hook is `_isolated_worker._write_envelope_file` (`execution/_isolated_worker.py:52`, called at `:274`). | 3, 7.1, 7.3 |
| H6 F29 integration not executable | Fixed: token census rule, outcome map, emitter order, observer writes removed, registry `1.0.0 -> 1.1.0`, producer inventory, no `object_ref` from F22. Provenance-aware `approve_ref` deferred to owner (Q2). | 5.4, 12 |
| H7 slices not gateable | Fixed: S0 to S4, each depends only on earlier slices, each carries its own docs delta. | 9 |
| H8 sizing understated | Accepted and reproduced: `git diff --shortstat 1b3a922 2fa4f8a5 -- src` is 3,632 insertions, `publication/` is 2,620 LOC; S0 is a mechanical port with a byte-identity gate; census per near-limit module. | 9.1 |
| M grounding defects | Accepted; fixed: pointer switch `publication/_managed.py:418`, sibling mint `execution/_isolated_commit.py:129-130`, `AbortReport.retained_path` `:39`, `resource` `:40`, immutable rename `publication/_local_directory.py:379`; phantom `api/jobs/memory_evidence.py` dropped; plan-lint is run from a downloaded copy, not a repo path. | 4, 9 |
| M frozen contracts | Fixed: `PublicationEvidence`, registry bump, full schema snapshots, rules line. | 8 |

## 1. Where we are

Kept (no gate round found a defect): the descriptor `decoy-publication/v1` and `result_id` commit truth (`publication/_types.py:136`), the `flock` destination lock without fallback (`publication/_lock.py:33`), managed generations behind `CURRENT`, the parent-only publisher with a preparation-only child (`execution/_isolated_worker.py:214`), the bounded trailer-first child gate (`publication/_child.py`). Not kept as is: `publication/_tokens.py:12-23` falls back to `type(exc).__name__`, so `os_error_label` of a dynamic `OSError` subclass returns `CELLMARK_x/ENOSPC` (probe, this revision).

Structurally wrong, each a third or later recurrence of a catalogued class: (a) producers choose temp locations next to shared parents (`execution/_transactional_sink.py:350`, `execution/_chunked_output_sink.py:471` on main, `execution/_isolated_commit.py:129-130`, `publication/_managed.py:316`, `:402`, `:491`), class 2; (b) sink state is nine flags (`execution/_transactional_sink.py:176-186`) guarded per method, and `_recovery_read` snapshots then drops the lock (`:553-561`), class 3; (c) value-freedom by deleting fields from chosen types, class 1. Verdict: restructure these three edges, keep the core, rebuild on F29 (not a rewrite, not a rebase).

## 2. Invariants as properties

Threat model: another principal may act on the destination's parent directory; same-uid hostile code and native code that ignores the paths it is given are out of scope (they can do anything). Each invariant names the test in 7.2 that proves it.

- **P1 containment (narrowed).** During an attempt A, every filesystem entry created by A's Python code, parent or child, lies in exactly one location class: `arena(A)`, the destination binding, the destination lock file (`publication/_lock.py:33`), a retention record created only by `rename` out of `arena(A)`, a reader hold (reader-owned, `publication/_managed.py:541`), or the attempt's scratch root (same owner). Native writers (PyArrow, DuckDB) are given only arena paths; their entries are observed at phase barriers and at the end, not continuously. Residual: a native file created and removed between two observations is not seen.
- **P2 disposal.** When the owner of `arena(A)` exits by any path other than bound, no byte written by A survives outside the binding and a retention record; the arena and scratch root are gone, or the result carries `cleanup_state=failed` and `leftover_count>0`. Residual: after a process kill (no in-process exit) the arena survives until `reclaim_orphan_arenas` (Q1).
- **P3 no orphan generation.** Under the destination lock, a managed attempt that renamed a generation and then failed to switch `CURRENT` removes that generation before releasing the lock.
- **P4 linearizable sink.** Every concurrent history of public calls on one sink equals a sequential history allowed by the table in 5.2; a busy refusal changes nothing.
- **P5 recovery coherence.** A recovery result is computed from the request in force for the whole call; no validation change or abort is linearized inside it; a found binding leaves the sink SEALED from any prior state, in this or a fresh process.
- **P6 report types are value-free.** For every report type (4.4) and every instance on any exit, `repr`, `str`, `dataclasses.asdict`, `json.dumps(asdict)` and `pickle` hold no data value, path, path component, OS message, DSN, exception class name, child-supplied string or table identifier. Caller-chosen identifiers (`operation_id`, `attempt_id`, `job_id`, `check_id`) appear as given after a shape check; that is the caller's own text, not data. Data carriers (4.4) are data by contract.
- **P7 closed emitters.** Every engine-owned emitter in 4.5 receives only report-type values or constant text. Residual: emitters outside F22's modules (explain output `execution/_planner.py:238`, strategy exceptions propagating unchanged) are F31's (Q3).
- **P8 bounded child gate.** Unchanged from r5.
- **P9 unit atomicity.** For every route and FK shape, the destination resolves to the complete prior unit or the complete new unit. FK closure inside a complete unit is the producer's (F06) invariant, not F22's.

## 3. Class table

| # | Class | Closing structure | Proof (test id in 7) | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | report types validated by exact type at construction; data carriers named; closed emitter list; error text from an exact-type table, never `__name__`; publication exceptions `raise ... from None` | `test_report_types_field_walk`, `test_marker_corpus_all_emitters` (A9) | annotate a report field `str`; restore `type(exc).__name__`; restore `from exc` |
| 2 | Cleanup missing on an exit path | `AttemptArena` sole owner, fd-held, disposal on every non-bind exit; retention by rename only; orphan generation removed under the lock | `test_exit_matrix_inprocess`, `test_real_child_kill_matrix`, `test_managed_orphan_generation` | skip `dispose()` on the timeout branch; mint the pointer temp under the root |
| 3 | State machine and concurrency | `SinkState` enum, literal table, single `_enter`/`_leave`, RECOVERING, COMMITTING split pre-bind/binding | `test_transition_cells_generated` (every cell of both tables), `test_restart_recovery` | run recovery in OPEN; map binding+exception to FAILED |
| 4 | Route parity and silent fallback | one `open_attempt()` for every route; capability row per route; a refused cell raises a registered reason | `test_route_capability_matrix` | flip one capability cell; drop the reason on the isolate=False fallback |
| 5 | Referential integrity | unit atomicity over every FK shape and route | `test_fk_shapes_all_or_none` | publish one rename per table |
| 6 | Untrusted producer | r5 bounded gate kept; `row_errors.json` byte cap and exact-type rebuild; child names never reach an emitter | `test_child_row_errors_bounded`, A10 | remove the cap; pass the child table to `context(table=)` |
| 7 | Check then use on a path (TOCTOU) | safe-parent precondition (sticky or not writable by others); arena fd held; disposal by `dir_fd`, never following links; binding rename `src_dir_fd`-relative; identity check at dispose | `test_arena_parent_unsafe_refused`, `test_arena_swap` (A6) | dispose by path; drop the precondition |
| 8 | Weak tests and overclaimed evidence | oracle twins (each oracle is run on a planted defect and must fail); baselines from the lock in one environment | `test_oracles_detect_planted_defects` | delete any oracle assertion: its twin turns red |
| 9 | Behavior-preserving refactor drift | S0 byte-identity port; sentries scan the whole package, not fixed roots; census exact | `test_sentry_scope_is_whole_package`, `test_module_size` | move a producer into a new subpackage |
| 10 | Frozen contract drift | additive only; registry 1.1.0; schema snapshots of every report type and `PublicationEvidence` | `test_report_schema_snapshots`, F29 `test_reason_registry.py` | drop a receipt field; add a code without the bump |

## 4. Enumerations

### 4.1 Exit paths of everything that owns files or state

Isolated parent and spawn (`execution/_isolated_run.py:345-396`, `execution/_isolated_spawn.py:47-146`):

| Id | Exit | Code | Arena design |
|---|---|---|---|
| X1 | work root `mkdtemp` or caller `staging_dir` `mkdir` fails before `try` | `execution/_isolated_run.py:358-366` | arena opened first; nothing to dispose; registered reason |
| X2 | arena open fails (unsafe parent, `mkdir`, owner `flock`) | new | refuse before any write; `arena_parent_unsafe` / `arena_open_failed` |
| X3 | sources or payload write, `json.dumps`, `chmod` fail | `execution/_isolated_run.py:413`, `:426-436` | inside arena; disposed |
| X4 | `Popen` raises | `execution/_isolated_spawn.py:69` | disposed |
| X5 | `on_spawn` raises; child killed, reaped | `execution/_isolated_spawn.py:85`, `:106-116` | disposed after reap |
| X6 | timeout; child killed | `execution/_isolated_spawn.py:88-105` | disposed after reap |
| X7 | external kill, no envelope | `execution/_isolated_spawn.py:118-146` | disposed |
| X8 | child self-reports crash or OOM | `execution/_isolated_run.py:449-464` | disposed |
| X9 | malformed envelope, gate refusal, unreadable table | `execution/_isolated_run.py:504-551` | disposed |
| X10 | promotion refused, conflict, cancelled | `execution/_isolated_commit.py:162` | disposed |
| X11 | promotion `commit_uncertain` | `publication/_local_directory.py:379-395` | arena disposed; binding untouched |
| X12 | `already_committed` or published | `execution/_isolated_commit.py:163`, `execution/_isolated_run.py:555` | disposed |
| X13 | `KeyboardInterrupt` while waiting | `execution/_isolated_spawn.py:106` | disposed after reap |
| X14 | parent killed | none | arena survives; `reclaim_orphan_arenas` (Q1) |

In-process sinks and backend:

| Id | Exit | Code | Arena design |
|---|---|---|---|
| S1 | write or sidecar raises | `execution/_transactional_sink.py:483-497` | FAILED; disposal on abort |
| S2 | commit raises before bind (`OSError` in `_ensure_staging`) | `execution/_transactional_sink.py:595-604` | FAILED (today re-committable) |
| S3 | exception inside the bind call | `execution/_transactional_sink.py:606` | UNCERTAIN |
| S4 | refused, uncertain, published | `execution/_transactional_sink.py:608-626` | per 5.2 |
| S5 | lookup or reconcile: found, none, refused, raised | `execution/_transactional_sink.py:553-573` | per 5.2 |
| S6 | quarantine sidecar staged, unlink fails | `quarantine.py:358`, `:498-504` | temp in arena; constant log |
| D1 | disposal partial (`unlinkat` fails) | new | `cleanup_state=failed`, `leftover_count` |
| D2 | arena renamed by another principal | new | refused by precondition; else identity mismatch, failed |
| D3 | `KeyboardInterrupt` during disposal | new | disposal completes in `finally`, then re-raises |
| M1 | generation renamed, then `fsync_dir` or pointer temp fails | `publication/_managed.py:398-416` | remove the generation under the lock (P3) |
| M2 | pointer switch fails, not visible | `publication/_managed.py:418-438` | same as M1 |
| M3 | pointer switch fails, visible | `publication/_managed.py:422-428` | UNCERTAIN, generation kept |
| M4 | protected backup fails | `publication/_managed.py:308-343` | backup temp in arena |
| M5 | restore stage fails | `publication/_managed.py:491-499` | own arena |
| R1 | retention record created | `publication/_retention.py:111-137` | rename out of arena; meta holds report values only |
| R2 | retention refused (store not private, budget, expiry) | `publication/_retention.py:104-110` | arena disposed |
| R3 | record `mkdir`/rename fails, `rmtree(record)` fails | `publication/_retention.py:135-136` | `cleanup_state=failed`, leftover counted |
| H1 | reader hold write fails; reader killed | `publication/_managed.py:533-546`, `:515-529` | reader-owned; stale holds reaped by pid check (unchanged) |

### 4.2 Execution routes

- sequential `execution/_sequential.py:591` commit, `:635` abort (`main:` via `observed_commit`/`observed_abort`, same lines).
- out-of-core `execution/out_of_core/_runner.py:313`, `:329`; scratch `main:execution/out_of_core/_runner.py:210`.
- full-frame `execution/_pipeline_full_frame.py:243` through `OutputPublish` (`execution/_chunked_output_sink.py:159`, `:171`).
- mixed `execution/_pipeline_mixed_route.py:264`, same `OutputPublish`.
- streamed generation `generation/_streamed.py:475`.
- isolated parent promotion `execution/_isolated_run.py:555`.
- isolated in-process fallback `execution/_isolated_run.py:301-313` (`main:execution/_isolated_run.py:212-230` warns, records no reason).
- resident `publish_tables` `execution/_transactional_sink.py:664`.

Sink variants (not routes): immutable directory, managed dataset (`execution/_transactional_sink.py:644`), preparation (`execution/_isolated_worker.py:214`).

### 4.3 FK shapes

- Shapes: single, composite, self-referencing, cross-table chain (parent, child, grandchild), child rows in a later batch than their parent.
- F22 binds the unit with one rename (`publication/_local_directory.py:379`, `publication/_managed.py:400`), so the claim per shape is P9 all-or-none, tested per route (7.2), not FK closure.

### 4.4 Fields of public types

Report types (every field validated at construction; safe under every serialization):

- `CommitReceipt` (`publication/_types.py:136-155`): `outcome` (closed), `operation_id` (SafeId), `reason_code` (registered base token), new `os_error` (errno name), `result_id`, `descriptor_digest`, `evidence_digest` (hex64), `artifacts` (`ArtifactSummary`), `is_current`, `visible`, `durable` (bool), `backup_status`, `cleanup_status` (closed); `generation`, `backup_ref` (today relative paths) become `generation_digest`, `backup_digest` (hex64).
- `ArtifactSummary` (`publication/_types.py:121-133`): `role` (closed), `size`, `rows` (int), `sha256`, `schema_digest` (hex64).
- `AbortReport` (`publication/_retention.py:32-41`): `status`, `reason`, `classification` (closed), `record_id` (hex32); `retained_path` (`:39`) and `resource` (`:40`) are deleted and move to `RetentionHandle`.
- `CleanupResult` (`publication/_retention.py:57-60`), new `DisposalReport(status, leftover_count)`.
- `CheckResult` (`publication/_types.py:65-68`): `check_id` (SafeId), `disposition` (closed), `required`.
- F29 `ReasonRecord` and `ReasonContext` (`main:src/decoy_engine/reasons/_record.py:110-122`, `:245-254`): already closed; `object_ref` is shape-only (`:72-76`) and F22 never sets it.
- F29 `MemoryEvidence` (`main:src/decoy_engine/execution/_isolated_memwatch.py:112-123`): floats, ints, bool; `kind`, `stop_reason`, `basis` become closed tokens.

Data carriers (data by contract; only their report-type fields reach an emitter):

- `PublicationRequest` (`publication/_types.py:86-104`), `PreparedPublication` (`:187-196`), `ChildPreparationEnvelope` (`:200-209`), `ArtifactRef` (`:108-118`), `ResolvedGeneration` (`publication/_managed.py:70-77`), `RetainedRecord` (`publication/_retention.py:44-54`), new `RetentionHandle(path)`.
- `IsolatedRunResult` (`main:src/decoy_engine/execution/_isolated_common.py:321-388`): report fields `outcome`, `peak_rss_mb`, `returncode`, `signal_number`, `error` (exact-type token), `isolated`, `pid`, `reason`, `publication_receipt`, `memory_evidence`; data fields `outputs`, `quality_metrics`, `table_kinds`, `committed_output_dir`, `row_errors`, `extra`.
- `ExecutionResult` (`main:src/decoy_engine/execution/_adapter.py:43-71`), `GenerationResult` (`main:src/decoy_engine/generation/_streamed.py:80-88`), `RowErrorRecord` (`main:src/decoy_engine/execution/_row_errors.py:48-59`), F29 `PublicationEvidence` (`main:src/decoy_engine/execution/_publication_observer.py:42-49`, `stored_bytes` keyed by config table).

### 4.5 Output surfaces

Engine-owned emitters (the closed list for P7):

- receipts on `sink.receipt`, `CommitError.receipt`, `ExecutionResult`/`GenerationResult`/`IsolatedRunResult.publication_receipt`.
- exception `str`, `args`, `__cause__`, `__context__` and formatted traceback of `CommitError`, `PublicationRefusedError`, `CommitUncertainError`, `ChildEnvelopeError`, `ResolveError` (`publication/_types.py:162-184`).
- `sink.abort_report`, `sink.disposal_report`, result `reason` payloads (`main:src/decoy_engine/execution/_isolated_reason.py:41-52`).
- `IsolatedRunResult` report fields, incl. `error` (`main:src/decoy_engine/execution/_isolated_common.py:238-259`).
- logs: `execution/_isolated_commit.py:89`, `main:src/decoy_engine/execution/_isolated_run.py:215`, `:223` (constant), `quarantine.py:498-504` (today paths and raw exception; becomes constant text plus errno token), new arena disposal log.
- progress `publish_stored(table, bytes)` (`main:src/decoy_engine/progress.py:268`): config table names only, F29-owned.
- persisted: retention `meta.json` (`publication/_retention.py:121-132`), `CURRENT` pointer (digests), reader hold files (pid), descriptor and evidence core (bind relative artifact paths: data by design, `docs/publication.md:48`, whose claims of exception type names and a debugging `__cause__` S1 rewrites).
- channels in, not out: child envelope `result.json` (`execution/_isolated_worker.py:52-65`), child stdout and stderr (`execution/_isolated_spawn.py:129`).

## 5. Design

### 5.1 Attempt arena (classes 2, 4, 7)

`publication/_arena.py` (about 200 LOC). Patterns: `tempfile.TemporaryDirectory` scoped ownership; `shutil._rmtree_safe_fd` fd-relative removal; POSIX sticky-bit rename rule; SQLite's journal-beside-database rule.

- `open_attempt(destination, *, scratch=True) -> AttemptArena`: refuse with `arena_parent_unsafe` unless the parent has the sticky bit or no principal other than the uid (and root) can write it (owner is uid or root, no group or other write bit). `os.mkdir(".decoy-attempt-<hex>", 0o700, dir_fd=parent_fd)` under `destination.parent` (managed: `<root>/.staging`); hold an `O_DIRECTORY|O_NOFOLLOW` fd and its `(st_dev, st_ino)`; `owner` file with pid under a held `flock`. Scratch root `.decoy-scratch-<hex>` under `TMPDIR`, same owner (keeps out-of-core scratch off the destination disk).
- Layout subdirectories: `prep`, `stage`, `hold`, `sidecar`, `ptr`, `backup`, `restore`, `work`. A producer receives the arena, not a parent path.
- The bind rename uses `os.replace(name, target, src_dir_fd=arena_fd)`; the managed pointer temp is `ptr/CURRENT.tmp`, replaced into `<root>/CURRENT` (same filesystem).
- `dispose() -> DisposalReport`: fd-relative walk with `follow_symlinks=False`, idempotent, in `finally`; then `rmdir` by name only if the name still has the held identity, else `failed` with `leftover_count=1`.
- Retention: `dispose_staging` renames `stage` out of the arena into `store/<record>/payload` (no copy); `meta.json` holds report values only.
- Isolated: the payload names arena-relative locations; child `TMPDIR` is the scratch root; caller `staging_dir` becomes the parent of `work`, never written itself.
- Managed P3: `_promote_and_switch` notes that its own `os.replace` created `generations/<d>` and removes it under the lock when `CURRENT` does not name it.
- `reclaim_orphan_arenas(parent)`: removes `.decoy-attempt-*` only when its owner `flock` is free (Q1).
- In-process fallback (`isolate=False`) records registered reason `isolation_disabled` (class 4).

### 5.2 Sink state machine (class 3)

Split `execution/_transactional_sink.py` (684 at `2fa4f8a5`, 380 on main) into `_sink_state.py` (enum, literal table, `_enter`/`_leave`), `_preparation_sink.py`, `_publishing_sink.py`, thin `_transactional_sink.py`. One `RLock`; transitions under it; backend I/O outside it. COMMITTING has two phases: `pre_bind` until `commit_prepared` is entered, `binding` inside it.

Idle and busy cells (`p` = prior state stored in RECOVERING; abort while in flight sets `pending`):

| state | write, sidecar | record_validation | commit | lookup | reconcile | abort |
|---|---|---|---|---|---|---|
| OPEN | WRITING | apply | COMMITTING | RECOVERING(OPEN) | RECOVERING(OPEN) | ABORTED, dispose |
| WRITING | WRITING (n+1) | apply | busy `write_in_progress` | busy `write_in_progress` | busy `write_in_progress` | pending |
| COMMITTING | busy `commit_in_progress` | busy | busy | busy | busy | pending |
| RECOVERING(p) | busy `recovery_in_progress` | busy, request unchanged | busy | busy | busy | pending |
| SEALED(R) | refuse `attempt_sealed` | refuse `attempt_sealed` | return R | return R, no I/O | return R, no I/O | no-op |
| UNCERTAIN | refuse `commit_uncertain` | refuse | refuse `commit_uncertain` | refuse `commit_uncertain` | RECOVERING(UNCERTAIN) | no-op, report `deferred_uncertain` |
| REFUSED | refuse `attempt_rejected` | refuse | refuse | refuse | refuse | ABORTED, dispose |
| FAILED | refuse `attempt_invalid` | refuse | refuse | refuse | refuse | ABORTED, dispose |
| ABORTED | refuse `attempt_aborted` | refuse | refuse | refuse | refuse | no-op |

In-flight exits (`pending` = an abort arrived during the in-flight call):

| from | outcome | final | if pending |
|---|---|---|---|
| WRITING (last writer) | ok | OPEN | ABORTED, dispose |
| WRITING | any exception | FAILED | ABORTED, dispose |
| COMMITTING | published, already_committed | SEALED(R) | dropped; abort report `not_needed` |
| COMMITTING | `CommitUncertainError`, or any other exception in `binding` | UNCERTAIN | dropped; `deferred_uncertain` |
| COMMITTING | `PublicationRefusedError` | REFUSED | ABORTED, dispose |
| COMMITTING | any other exception in `pre_bind` | FAILED | ABORTED, dispose |
| RECOVERING(OPEN), nothing staged | receipt R | SEALED(R) | dropped |
| RECOVERING(OPEN), bytes staged | receipt R | OPEN, R returned; `commit()` judges (`already_committed` or `operation_conflict`, `docs/publication.md:28`) | ABORTED, dispose |
| RECOVERING(OPEN) | none | OPEN | ABORTED, dispose |
| RECOVERING(OPEN) | refused | REFUSED | ABORTED, dispose |
| RECOVERING(OPEN) | other exception | OPEN | ABORTED, dispose |
| RECOVERING(UNCERTAIN) | receipt R | SEALED(R) | dropped |
| RECOVERING(UNCERTAIN) | none | REFUSED `commit_not_visible` | ABORTED, dispose |
| RECOVERING(UNCERTAIN) | refused | REFUSED | ABORTED, dispose |
| RECOVERING(UNCERTAIN) | other exception | UNCERTAIN | dropped; `deferred_uncertain` |

Restart: a fresh sink starts OPEN and knows nothing; a `reconcile()` or `lookup_committed()` before any write that finds the binding seals it, so it refuses writes and returns R on commit (sealing after writes would hide an `operation_conflict`, so a sink with staged bytes stays OPEN and lets the backend judge); a `commit()` without recovery reaches the backend judge, which returns `already_committed` (SEALED) or refuses a different result. Dispose never touches a binding: only the arena. The preparation sink uses the same table (SEALED abort is a no-op, closing the `execution/_transactional_sink.py:378-380` defect).

### 5.3 Report boundary (classes 1, 6)

- `publication/_report.py` (about 150 LOC): `report_type` decorator; `__post_init__` validates each field by exact type (`type(v) is str`, never `isinstance`, so a `str` subclass with a hostile `__repr__` is refused) against its annotation: `Token[vocab]`, `Hex64`, `Hex32`, `SafeId` (`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`), `ErrnoName`, `int`, finite `float`, `bool`, `None`, a closed `Enum`, an exact nested report type, a tuple of these. Report types are `frozen=True, slots=True`; a property is allowed only from a listed set (today `CommitReceipt.committed`, `publication/_types.py:158`, a bool) whose return values the corpus walks.
- `PublicationRequest.__post_init__` shape-checks `operation_id`, `attempt_id`, `job_id`, `check_id` before any write.
- `exception_error_text`, `commit_error_text` (`main:src/decoy_engine/execution/_isolated_common.py:238-259`) and `os_error_token`/`os_error_label` (`publication/_tokens.py:12-23`) map `type(exc)` through an explicit literal table of engine and builtin exception classes; anything else is `UnregisteredError`. A hostile subclass of a listed class is unregistered.
- Publication backend raises `PublicationRefusedError(...) from None`; the errno lives in `receipt.os_error`.
- `quarantine.py:498-504` log becomes constant text plus errno name.

### 5.4 F29 integration

- Re-apply on a branch from current `main`; `2fa4f8a5` stays the reference.
- Receipt `reason_code` base tokens (`already_committed`, `conflict`, `target_occupied_different`, `required_check_failed`, `lock_unavailable`, `promote_failed`, `switch_failed`, `rename_failed`, `staging_changed`, `backup_failed`, and the rest found by the census) each register as `decoy.publication_<token>` in `main:src/decoy_engine/reasons/decoy/_core.py`; the `:<errno>` suffix moves to `os_error`. Busy and arena codes: `write_in_progress`, `commit_in_progress`, `recovery_in_progress`, `attempt_sealed`, `commit_not_visible`, `arena_parent_unsafe`, `arena_open_failed`, `arena_cleanup_failed`, `isolation_disabled`. `CommitReceipt.__post_init__` refuses an unregistered token, so the exit matrix proves the mapping exhaustive at runtime; `REGISTRY_VERSION` goes `1.0.0 -> 1.1.0` (`main:src/decoy_engine/reasons/_registry.py:24`, rule `main:tests/unit/test_reason_registry.py:151-159`); new sites go into `main:tests/fixtures/f29/producer-inventory.json`.
- Outcome to `PublicationState`: published, already_committed -> `published`; rejected, conflict, unsupported, cancelled_before_commit -> `not_published`; commit_uncertain -> `unknown`. `DisposalReport.status` to `CleanupState`: confirmed -> `confirmed`, failed -> `failed`, not run -> `not_needed`.
- Ordering: the sink's `_leave` sets `SinkState` and, for disposal, `DisposalReport`; `publication_evidence` becomes a read-only projection of both; the evidence writes in `observed_commit`/`observed_abort` (`main:src/decoy_engine/execution/_publication_observer.py:110-111`, `:123`, `:128`) are deleted for sinks with evidence, kept for foreign sinks. `commit_reason` takes the `DisposalReport` instead of `staging.exists()` (`main:src/decoy_engine/execution/_isolated_reason.py:50`), read after disposal.
- Table identifiers: F22 passes no `table=` to `PublicationEvidence.context` and sets no `object_ref`; `stored_bytes` keys are config-declared tables, asserted in A10.

## 6. Reuse inventory

- F29 registry, `ReasonRecord`, `ReasonContext`, `PublicationState`, `CleanupState` (`main:src/decoy_engine/reasons/_record.py:43-72`); `_publication_observer.py`; `_isolated_reason.py`; reason sentries `main:tests/sentry/test_reason_registry.py`, `main:tests/sentry/_reason_scan.py`, fixtures under `main:tests/fixtures/f29/`.
- Engine sentries `tests/sentry/test_module_size.py`, `test_physical_seam_disconnection.py`, `test_public_import_boundary.py`, `test_publication_commit_boundary.py` (ported).
- F22 backend verbatim: descriptor, codec, lock, child gate, tokens, `tests/unit/publication/_support.py` oracle helpers.
- F31 boundary: not landed; F22 builds no general boundary, only report types for its own types (Q2, Q3).
- F25: orphan scheduling and quotas (Q1).

## 7. Hostile corpus (acceptance tests, red first)

All tests are written and recorded red on the slice's start commit before the fix. Where a test is green at baseline it is labelled characterization and paired with the mutation that must turn it red.

### 7.1 Harness and oracle twins

- `tests/unit/publication/_audit.py`: runs the scenario in a fresh subprocess that installs `sys.addaudithook` (PEP 578) for `open` (write modes, `O_CREAT`), `os.mkdir`, `os.rename`, `os.replace`, `os.link`, `os.symlink`, `tempfile.mkstemp`, `tempfile.mkdtemp`, `shutil.copyfile`; the child inherits it through a `sitecustomize.py` on `PYTHONPATH` (spawn copies `os.environ`, `execution/_isolated_spawn.py:68`). Audit events fire for every call route (alias, `getattr`, `functools.partial`, property), so containment does not depend on syntax.
- Oracles: `assert_contained(events, tree_before, tree_after, arena)`, `assert_no_markers(blob)`, `assert_all_or_none(dest)`, `assert_cell(state, op, outcome)`.
- `test_oracles_detect_planted_defects`: each oracle runs on a planted leftover, a planted marker, a half unit and a wrong state, and must raise. Deleting an oracle assertion turns this red independently of test order.
- Phase hooks in the child (sitecustomize wrappers that write `reached-<phase>` with a tree listing, then block on a FIFO): `pq.ParquetWriter.write_batch`, `pq.write_table`, `_Spill.add`, `ParquetPreparationSink.commit` after its rename, `_isolated_worker._write_envelope_file` after return. A thread started by `on_spawn` (`execution/_isolated_spawn.py:85`) waits for the phase file and sends SIGKILL. The phase file must exist, so a broken hook fails rather than passes.

### 7.2 Per-invariant tests

| Test | Proves | Cases | Red at baseline? |
|---|---|---|---|
| `test_exit_matrix_inprocess` | P1, P2 | S1 to S6, D1 to D3, M1 to M5, R1 to R3 on every in-process route | red: M1 (orphan generation reproduced), R1 (meta marker), S2 |
| `test_real_child_kill_matrix` | P1, P2 | K1 before write, K2 mid table 1, K3 spill active, K4 between tables, K5 after prep rename, K6 after envelope; kill, timeout, `on_spawn` raise | red K2 to K4 (Codex); K1, K5, K6 characterization, mutation: drop disposal on the self-report path |
| `test_managed_orphan_generation` | P3 | fault at `fsync_dir(generations)`, pointer temp write, switch | red |
| `test_transition_cells_generated` | P4 | in-flight {write, commit pre_bind, commit binding, lookup, reconcile} x arriving {write, validation FAIL, commit, lookup, reconcile, abort} x every outcome row of the in-flight table (cell count asserted equal to the table); asserts arriving result, final state, request unchanged on busy, lock not held across I/O | red on the reproduced cells |
| `test_restart_recovery` | P5 | subprocess A SIGKILLed after the bind rename, before return; fresh sink B: reconcile -> SEALED, write refused `attempt_sealed`, commit returns R, abort no-op, binding intact | red (write accepted today) |
| `test_report_types_field_walk` | P6 | every class in 4.4 classified; every report field annotation allowed; slots hold no extra attribute and every property is in the listed set; a `str` subclass value is refused | red (`AbortReport.retained_path`) |
| `test_marker_corpus_all_emitters` | P6, P7 | markers `PATHMARK` (dirs), `OSMARK` (injected `OSError` text), `CELLMARK` (data and a dynamic exception class name), `TBLMARK` (child-only table), `DSNMARK` (config DSN); every exit in 4.1 x every emitter in 4.5 incl. `repr`, `asdict`, `pickle`, traceback, `caplog`; non-empty: the same markers are present in the data carriers | red (P1 to Q3 probes) |
| `test_fk_shapes_all_or_none` | P9 | 4.3 shapes x 8 routes x {kill mid table k, refused commit} | characterization; mutation: one rename per table |
| `test_route_capability_matrix` | class 4 | every route: arena used, recovery available, managed unit; `isolate=False` records `isolation_disabled` | red (no reason today) |
| `test_child_row_errors_bounded` | P8 | oversize and wrong-type `row_errors.json` | red |
| `test_arena_parent_unsafe_refused` | class 7 | parent 0o777 without sticky refused before any write; with sticky accepted | red (no check) |

### 7.3 Ten gate attacks (Codex list)

| # | Attack | Test | Red first |
|---|---|---|---|
| A1 | kill real child mid-spill | kill matrix K3 | red |
| A2 | kill after prep rename | K5 | characterization; mutation: lease skips `prep` |
| A3 | kill after envelope, before exit | K6 via `_write_envelope_file` hook | characterization; mutation as K5 |
| A4 | retain rejected staging outside arena | `test_retention_transfer`: audit shows one `rename` arena -> store; `meta.json` and `AbortReport` free of markers; refused retention leaves the store unchanged | red (`/srv/PATHMARK_op` in meta, path in `asdict`) |
| A5 | fail managed pointer-temp write | `test_managed_orphan_generation` plus SIGKILL at the pointer write: no new entry under root except the arena | red |
| A6 | rename arena, symlink its old name | `test_arena_swap`: under a sticky parent the swap by a second uid is impossible (precondition test); same-uid swap: sentinel untouched, contents removed via fd, `cleanup_state=failed`, `leftover_count=1` | red (lease follows `islink` then path, `execution/_isolated_commit.py:82-85`) |
| A7 | recovery versus validation FAIL | cell: lookup in flight, validation busy, request unchanged; after release with none, validation applies and commit refuses | red (reproduced) |
| A8 | crash after bind, fresh process recovers then commits | `test_restart_recovery` | red (reproduced) |
| A9 | hostile class name or path in result, cause, log | corpus with `type("CELLMARK_x", (OSError,), {})` raised at write, bind and dispose | red (reproduced: `exception_error_text` on main, `os_error_label` at `2fa4f8a5`) |
| A10 | valid-shaped child table into `object_ref` or `stored_bytes` | child declares `TBLMARK_x`; absent from every reason payload, evidence, progress event, receipt, log | worker records; if green, mutation: pass the child name to `context(table=)` |

### 7.4 Sentries (tripwires; the runtime tests above are the proof)

- `test_creation_sites_census`: AST over every `.py` under `src/decoy_engine/` (file set asserted equal to `git ls-files`); resolves `import x as y`, `from x import f as g`, literal `getattr(mod, "f")`, `functools.partial(f, ...)`; flags non-literal `getattr` on `os`, `shutil`, `tempfile`, `pathlib` as unresolved; flags method names `mkdir`, `write_bytes`, `write_text`, `touch`, `open` on any receiver (covers properties returning paths). Every hit must be in `tests/sentry/creation_sites.json` with a location class from P1. Seeded evasion fixtures for each alias form must fail it.
- `test_report_schema_snapshots` (class 10) and F29 `test_reason_registry.py` (version bump).

## 8. Frozen contracts touched

- `decoy-publication/v1` descriptor and evidence core: unchanged.
- F29 reason manifest and consumer contract (`main:tests/fixtures/f29/reason-manifest-v1.json`, `consumer-contract.json`): additive, registry 1.1.0.
- `PublicationEvidence` fields: unchanged; writes move from the observer into the sink projection; snapshot added.
- `IsolatedRunResult`: additive (`publication_receipt`); `error` text changes for unregistered classes (pre-GA).
- `CommitReceipt`, `AbortReport` (unreleased): changed as in 4.4, then frozen by snapshots.
- `TransactionalSink` protocol (`execution/_transactional_sink.py:84`): methods unchanged.

## 9. Slices

Branch `f22/rebuild` from current `main`, repo `decoy-engine`. Each slice depends only on earlier ones, lands its red tests in its own first commit, and updates the `docs/publication.md` section it changes.

| Slice | Content | Size | Acceptance |
|---|---|---|---|
| S0 | port `publication/` and its backend-only tests and sentries from `2fa4f8a5` verbatim | 2,620 src; tests measured at start (Codex: up to 4,607 unit, 239 sentry) | `git diff 2fa4f8a5 -- src/decoy_engine/publication` empty; ported suites and F29 sentries green |
| S1 | `_report.py`, report types reshaped, id shape checks, `from None`, registry 1.1.0 codes, token census | ~350 | field walk, marker corpus on backend types, A4 meta part, snapshots |
| S2 | `_arena.py`, managed P3, pointer/backup/restore in arena, retention transfer, audit harness, creation census | ~450 | `test_managed_orphan_generation`, A4, A5, A6, precondition, oracle twins |
| S3 | sink split, state table, RECOVERING, restart, in-process routes on the arena, observer projection | ~500 new, ~400 moved | all table cells, restart, in-process exit matrix, route matrix, FK matrix |
| S4 | isolated parent and child on the arena, scratch root, exact-type error text, `commit_reason` with `DisposalReport`, quarantine log, `row_errors` cap | ~350 | kill matrix, A9, A10, full corpus |

### 9.1 Module census after each slice (`main` LOC)

| Module | main | Rule |
|---|---|---|
| `execution/_sequential.py` | 696 | call sites only, net <= 0 |
| `execution/out_of_core/_runner.py` | 681 | net <= 0 |
| `quarantine.py` | 645 | net <= 0 |
| `execution/_isolated_run.py` | 622 | arena code goes to `_isolated_commit.py`; <= 622 |
| `execution/_chunked_output_sink.py` | 558 | spill takes the arena; <= 600 |
| new modules | 0 | each <= 600 |

### 9.2 Commands (from `uv.lock`, one environment; record `python -V`, DuckDB and PyArrow versions)

```bash
uv sync --frozen --extra dev
```

```bash
uv run pytest tests/unit/publication tests/sentry -q -p no:cacheprovider
```

```bash
uv run pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider
```

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/decoy_engine
```

The baseline is the same commands on the slice's start commit in the same environment; both failure sets go in the evidence file. `scripts/dev/gates.sh` does not exist at `2fa4f8a5` or `fdd08a8`.

## 10. Worker evidence rules

- Name the class of every finding (section 3) and fix the class structure, not the instance.
- Write the generated tests first (7.1 to 7.3) and record them red with exact ids; label characterization tests and record their mutation.
- Attack your own diff: apply each mutation in section 3 once and record the test that turned red.
- Run baselines from `uv.lock` in the same environment as the branch; state what you did not run.
- Never widen a threshold, skip a test, weaken an assertion or raise a census number.
- If a defect of a class already in the catalog recurs, stop and report it with a class-level proposal.

## 11. Pre-mortem

- **R1 audit hook misses native writes.** Named residual in P1; barrier walks catch survivors; native writers only receive arena paths.
- **R2 safe-parent precondition breaks a real deployment** (group-writable shared output without sticky bit). Typed refusal before any write; owner decides (Q4).
- **R3 `src_dir_fd` unsupported.** Assert `os.rename in os.supports_dir_fd` at import (`os.replace` shares `renameat` but is not listed in that set: probe on 3.11 shows `os.replace in os.supports_dir_fd` is False while `os.replace(..., src_dir_fd=fd)` works); otherwise refuse, no path fallback.
- **R4 S0 port drifts from `main`.** Byte-identity gate; F29 lands first; S1 onwards rebase their own commits only.
- **R5 the cell generator itself is wrong.** Its expectations are read from the literal table; the oracle twin plants a wrong state.
- **R6 a third round.** Most likely cause: a producer outside 4.1. The creation census fails on it and the audit oracle names its path class.

## 12. Open questions for the owner

- Q1: F22 ships `reclaim_orphan_arenas` (owner-lock proof); F25 owns scheduling and quotas. Agree?
- Q2: F29 `approve_ref` is shape-only (`main:src/decoy_engine/reasons/_record.py:72-76`). F22 avoids it by never passing table text. Should F29 or F31 replace it with a config-provenance capability?
- Q3: explain output (`execution/_planner.py:238`) and strategy exceptions are outside F22's emitter list; route to F31?
- Q4: accept the safe-parent precondition (refuse a non-sticky, other-writable destination parent)?
- Q5: exact-type error text changes `IsolatedRunResult.error` for unlisted classes to `UnregisteredError`; acceptable pre-GA?

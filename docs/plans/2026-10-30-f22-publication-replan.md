Status: proposed (re-plan; replaces the remediation track of `2026-10-29-f22-atomic-idempotent-publication.md` for the execution-side integration)

Summary: F22 r1 to r5 each ended NO-GO because three resource and reporting concerns were patched per site. This plan keeps the publication backend, and restructures ownership (one attempt arena), the sink lifecycle (one executable transition table with a RECOVERING state) and reporting (one report-plane serializer on F29's reason vocabulary), rebuilt on top of F29 on `main`.

Baseline: `decoy-engine` `claude/wizardly-allen-8ymekg` at `2fa4f8a5` (read-only). F29 baseline: `main` at `fdd08a8` (merge base `1b3a922`).

Rules consulted: `DEFECT-CATALOG.md` (decoy-platform `handoffs/queue`, sections 2 to 5), `HOSTILE-PLAN-REVIEW.md`, engine `CLAUDE.md` (600/700 LOC census, pre-GA hard delete, established methodology, comments explain why), `docs/publication.md`, `docs/compatibility-contract.md`, the F22 plan and r5 evidence, F29 `src/decoy_engine/reasons/_record.py` and `execution/_publication_observer.py` at `fdd08a8`.

Grounding note: every `file:line` below was read at `2fa4f8a5` unless prefixed `main:` (read at `fdd08a8`). No tests, probes or baselines were run for this plan. The four Codex HIGH findings are taken as reported by Codex; the code paths they name were read and agree, but the leftovers were not re-reproduced here.

# F22 publication: re-plan

## 1. Where we are

### 1.1 What is sound and kept

- **The commit truth.** The descriptor (`decoy-publication/v1`), `result_id` over the artifact inventory, the evidence core bound before the descriptor, `already_committed` / `conflict` / `commit_uncertain` outcomes (`publication/_types.py:19`, `docs/publication.md:9`). Reasoned, cited (Delta Lake transaction id, Git content naming, `publication/_types.py:3`), and no gate round found a defect in it.
- **The backend layout.** `publication/` as the one place that binds a destination (`tests/sentry/test_publication_commit_boundary.py:110`, `:120`, `:137`), the `flock` destination lock with no lock-free fallback (`publication/_lock.py:1`), managed generations behind an atomically replaced `CURRENT` pointer (`publication/_managed.py:385`).
- **The isolated child is preparation-only** and the parent is the only publisher (`execution/_isolated_worker.py:214`, `execution/_isolated_commit.py:141`). One `PublishContext` builds the request for both the gate and the promotion (`execution/_isolated_commit.py:111`).
- **The bounded child gate.** Trailer-first footer read with a 64 MiB cap (`publication/_child.py:31`, `:149`), typed envelope rebuilt against the parent-assigned path (`publication/_child.py:68`), verify before any payload read (`execution/_isolated_run.py:536` to `:546`).
- **Value-free tokens** for OS errors (`publication/_tokens.py:12`) and `ArtifactSummary` without paths (`publication/_types.py:121`).
- **The test investment** under `tests/unit/publication/` (r2 to r5, about 3,900 lines) as characterization, except where section 1.4 says it proved the wrong thing.

### 1.2 What is structurally wrong

1. **Ownership is derived from paths, not held by an owner.** Each producer mints its own temporary directory next to a shared parent: the sink's `_decoy_stage_*` in `target.parent` (`execution/_transactional_sink.py:342` to `:354`), the hold-back spill `_decoy_hold_*` in `sink.spill_parent` (`execution/_chunked_output_sink.py:463` to `:476`, `execution/_transactional_sink.py:318`), the isolated prep dir `_decoy_isolated_stage_*` (`execution/_isolated_commit.py:124`), the quarantine `mkstemp` (`quarantine.py:358`), managed backups and restores (`publication/_managed.py:316`, `:491`), and the out-of-core scratch in `TMPDIR` (`execution/out_of_core/_runner.py:209`). The r5 `StagingLease` owns exactly the one path the parent named (`execution/_isolated_run.py:366`, `:371`), so it cannot own what the child's sink or spill created beside it. That is Codex HIGH 1, and it will recur for every new producer as long as producers choose locations.
2. **The sink's state is eight independent flags with a prose table.** `_failed`, `_sealed`, `_aborted`, `_uncertain`, `_writes`, `_committing`, `_abort_requested`, `_reason`, `receipt` (`execution/_transactional_sink.py:176` to `:186`); the table lives only in the docstring (`:38` to `:46`). Each public method hand-picks which flags to test. `_recovery_read` tests `_committing` and then drops the lock before backend I/O (`:553` to `:561`) without entering any in-flight state, so `abort()` and `record_validation()` interleave freely (Codex HIGH 2). Three further code-grounded gaps of the same shape:
   - A non-refusal exception inside `commit()` (for example `OSError` from `_ensure_staging`, `:604`) leaves `_sealed=True` with no receipt, no `_reason`, no `_failed`; a second `commit()` passes `_enter_publication` and commits again (`:585` to `:596`). The table has no row for "COMMITTING ended by an unexpected exception".
   - `ParquetPreparationSink`: an `abort()` after the seal reaches `_settle` with `receipt is None` and `_uncertain` false, so the sealed sink is flagged ABORTED (`:378` to `:380`), which the table forbids (SEALED abort is a no-op).
   - `_before_first_stage` runs admission and `validate_request` while holding the sink lock (`:345` to `:347`, `:541` to `:543`), which is the "lock across I/O" the module docstring says never happens (`:31`).
3. **Value-free is enforced by deleting fields from chosen types and testing chosen fields.** The r5 helper serializes four fields of `IsolatedRunResult` (`tests/unit/publication/test_remediation_r5_value_free.py:56` to `:64`). Nothing classifies every field of every public type, and there is no single serializer, so `AbortReport.resource` / `retained_path` (`publication/_retention.py:39`, `:40`), `IsolatedRunResult.committed_output_dir`, `outputs`, `row_errors`, `extra`, `quality_metrics` (`execution/_isolated_common.py:404` to `:426`) all reach any caller that runs `dataclasses.asdict`. Also unfound by any round: `PublicationRequest.operation_id` is caller text with no shape check (`publication/_types.py:90`, `publication/_local_directory.py:106`) and is echoed into every receipt and into persisted retention metadata (`publication/_retention.py:126`).
4. **F22 was built on a fork that predates F29 and edits the same files.** `fdd08a8` is not an ancestor of `2fa4f8a5`; F29 rewrote `_transactional_sink.py`, `_isolated_common.py`, `_isolated_run.py`, `_isolated_worker.py` and added `_publication_observer.py` and `_isolated_reason.py`. The overlap is semantic, not textual (section 9).

### 1.3 Recurring classes (catalog numbers)

| Round finding | Catalog class | Why it recurred |
|---|---|---|
| r4 HIGH 1, r5 HIGH 1, 2fa4 HIGH 1: staging left after refusal, then after child kill | 2 cleanup | producers pick locations; the owner was added after, per path |
| r3 sink acceptance split-brain, r4 six siblings, r5 HIGH 3, 2fa4 HIGH 2 | 3 state machine | flags plus prose table; each new method guards a subset |
| r5 HIGH 4, 2fa4 HIGH 3 | 1 value-bearing output | per-field deletion; tests over chosen fields |
| r5 HIGH 2 footer | 6 untrusted producer | closed in r5, kept |
| r5 baseline counts, 2fa4 LOW baseline | 8 weak evidence | environment not recorded (Python 3.11 vs 3.12, DuckDB version) |
| 2fa4 HIGH 4 F29 | 9 refactor drift / 10 frozen contract | long-lived branch over a squash-merged neighbor |

### 1.4 Verdict

**Restructure three edges, keep the core; not a rewrite.** The descriptor, backend, lock, managed pointer, child gate and receipt vocabulary are right. The lease, the sink state machine and the value-free surface each need a class-level structure, because each is the third or later recurrence of a catalogued class (catalog section 5: stop patching). The existing parent-assigned exit-path matrix uses a fake worker that writes straight into the leased directory (`tests/unit/publication/test_remediation_r5_staging_lease.py:22`, `:87`), so it never exercised the real child sink; those tests stay as characterization but are not evidence for the arena invariant.

### 1.5 The new design in five sentences

1. Every publication attempt owns exactly one **attempt arena**, a private 0700 directory on the destination filesystem created by the attempt's owner, and every byte the attempt writes (child prep dir, sink staging, hold-back spill, unit sidecars, in-flight managed generation, scratch) is created inside it from an `ArenaLayout` capability; disposing the arena is the only cleanup and no producer may mint a temporary location anywhere else.
2. The sink's lifecycle is one `SinkState` enum and one literal `TRANSITIONS` table that every public method enters through a single `_enter(op)` / `_leave(op, result)` pair, with WRITING, COMMITTING and **RECOVERING** as first-class in-flight states and an explicit target state for every exception exit.
3. RECOVERING refuses `record_validation`, `commit`, writes and a second recovery as busy (not latched) and defers `abort` until recovery resolves, so a recovery result is always computed from the request in force when it returns.
4. Every public result, receipt and report type has each field registered in one `FIELD_PLANES` map (report, handle or data), and one serializer, `to_report()`, emits only report-plane fields through F29's closed `ReasonContext` and `approve_ref`; a generated field walk and a seeded marker corpus cover the complete payload.
5. F22's execution-side integration is re-applied on a fresh branch from `main` (F29), with F29's `PublicationEvidence` / `ReasonRecord` as the one channel for publication and cleanup state, fed by the arena disposal report and the sink state rather than by a second set of flags.

## 2. Invariants as properties

- **P1 (arena containment).** For any attempt A and any time t, every filesystem entry created by A's code (parent or child process) is a descendant of `arena(A)`, of the committed destination binding, or is the destination lock file `.<name>.decoy-lock` (`publication/_lock.py:33`, holds no data).
- **P2 (disposal).** When the owner of `arena(A)` exits by any path other than "bound", `arena(A)` does not exist afterwards, or the result carries `cleanup_state=failed` and the arena is still a single contained directory named in a handle-plane field only.
- **P3 (no orphan generation).** In a managed dataset, under the destination lock, every `generations/<d>` is referenced by `CURRENT`, by history lineage, by a backup, or by a live reader hold; an attempt that renamed a generation and then failed to switch removes that generation before releasing the lock.
- **P4 (linearizable sink).** Every concurrent history of public calls on one sink is equivalent to some sequential history permitted by `TRANSITIONS`; every busy refusal leaves the state unchanged.
- **P5 (recovery coherence).** A `lookup_committed` / `reconcile` result is computed from the request that is current at its return, and no `abort` or validation change is linearized inside it.
- **P6 (report plane is value-free).** For every public type T and every instance reachable on any exit path, `json.dumps(T.to_report())` contains no input data value, no filesystem path or path component, no OS error message text, no DSN, no child-supplied string, and no table identifier except an F29-approved config identifier in an `object_ref` key.
- **P7 (one truth for state).** `IsolatedRunResult.reason.context.publication_state` and `cleanup_state`, the `CommitReceipt.outcome`, and the sink's `SinkState` agree under the mapping in section 9.2 for every exit.
- **P8 (bounded child gate).** Unchanged from r5: the parent reads no child payload before the typed envelope, declared table set and footers verify within the footer budget.

## 3. Class table

| # | Class | Instances in F22 | Closing structure | Proof (sentry / property test) | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | `AbortReport.resource` (`publication/_retention.py:40`); `IsolatedRunResult` full asdict (`execution/_isolated_common.py:402`); unvalidated `operation_id` (`publication/_types.py:90`); child table names in refusals (r5) | `FIELD_PLANES` registry over every public type; one `to_report()`; report fields only from closed enums, digests, counts, errno names, registered reason codes and `approve_ref` identifiers; `operation_id` / `attempt_id` shape-checked at construction | `test_report_plane_field_walk` (every field of every type in `PUBLIC_RESULT_TYPES` registered); `test_report_plane_marker_corpus` (section 6.3) | add a `Path` field to `CommitReceipt` without a plane entry; route `resource` into `to_report()`; drop the `operation_id` check |
| 2 | Cleanup missing on an exit path | child `_decoy_stage_*` beside prep (`execution/_transactional_sink.py:350`); `_decoy_hold_*` (`execution/_chunked_output_sink.py:476`); orphan managed generation (`publication/_managed.py:400`); caller `staging_dir` keeps `payload.json` and source Parquet (`execution/_isolated_run.py:395`, `:408`) | `AttemptArena` owner per attempt; `ArenaLayout` capability is the only way to obtain a writable temp path; arena-temp AST sentry | `test_real_child_kill_matrix` (section 6.1); `test_inprocess_exit_matrix`; `test_no_temp_outside_arena` AST sentry | restore `tempfile.mkdtemp(dir=self._target.parent)` in the sink; make `_Spill` ignore the layout; skip `arena.dispose()` on the timeout branch |
| 3 | State machine and concurrency | `_recovery_read` drops the lock (`execution/_transactional_sink.py:553`); unexpected exception in commit re-enables commit (`:595`, `:604`); SEALED prep sink flagged ABORTED (`:378`) | `SinkState` enum + literal `TRANSITIONS` table + single `_enter` / `_leave`; RECOVERING; exception exits named per state | `test_transitions_generated` (every state x op cell); `test_barrier_interleavings` (section 6.2); `test_docs_table_matches_transitions` | delete RECOVERING (recovery runs in OPEN); map COMMITTING+exception to OPEN; remove the deferred-abort branch |
| 4 | Route parity and silent fallback | eight publishing routes (section 4.2) each obtain staging differently | every route takes its arena from one `open_attempt()`; capability row per route | `test_every_route_writes_inside_arena` parametrized over section 4.2 | change one route to construct `ParquetTransactionalSink(target)` without an arena |
| 5 | Referential integrity | N/A for new design: publication is all-or-nothing over the unit, so parent and child tables bind in one rename; no per-row path exists in F22 | unit atomicity (kept) | `test_fk_parent_child_bind_together` (kill between the two table writes: neither visible) | publish tables one rename per table |
| 6 | Untrusted producer | child envelope and footer (`publication/_child.py:68`, `:93`); child-written `row_errors.json` read unbounded (`execution/_isolated_commit.py:182` to `:194`) | keep r5 bounded gate; add a byte cap and exact-type rebuild for `row_errors.json`; child strings never enter the report plane | `test_child_row_errors_bounded`; corpus seeds child-supplied markers | remove the `row_errors.json` size cap |
| 7 | Check then use on a path (TOCTOU) | lease checks `islink` then `rmtree` by path (`execution/_isolated_commit.py:82` to `:85`) | arena held by an `O_DIRECTORY` fd; disposal walks with `dir_fd` and `follow_symlinks=False` (the `shutil.rmtree` fd-based variant) | `test_arena_swap_is_not_followed` (replace arena with a symlink to a sentinel dir between create and dispose) | dispose by path string instead of the held fd |
| 8 | Weak tests and overclaimed evidence | fake child in the lease matrix (`tests/unit/publication/test_remediation_r5_staging_lease.py:87`); four-field serializer (`tests/unit/publication/test_remediation_r5_value_free.py:56`); baseline on 3.11 without DuckDB version | evidence rules (section 11); real child only; complete payload only; every generated test has a non-vacuity assertion | reviewer reruns slice commands in one environment | delete the non-vacuity assertion in any generated test; the test must then fail its own meta-check |
| 9 | Behavior-preserving refactor drift | r5 split moved code to `_isolated_spawn.py`; F29 rewrote the same modules; `_transactional_sink.py` split pending | re-apply on `main`; characterization tests ported first; sentries scan new module paths | `test_module_size` census exact; ported r2 to r5 suites green at each slice | move a module out of the arena sentry's scan roots |
| 10 | Frozen contract drift | F29 reason manifest v1 (`main:tests/fixtures/f29/reason-manifest-v1.json`), F29 consumer contract, `decoy-publication/v1` | additive-only; snapshot tests | `test_reason_manifest_snapshot` (F29, kept); new `test_commit_receipt_schema_snapshot` and `test_report_schema_snapshot` | rename a F29 code; drop a receipt field |

## 4. Enumerations

### 4.1 Exit paths of everything that owns files or state

Isolated parent (`execution/_isolated_run.py:345` to `:396`) and spawn (`execution/_isolated_spawn.py:47` to `:146`):

| Id | Exit | Code | r5 behavior | Arena design |
|---|---|---|---|---|
| X1 | payload not JSON-serializable | `execution/_isolated_run.py:425` to `:430` | lease disposes prep; owned work root removed | arena disposed |
| X2 | `Popen` raises | `execution/_isolated_spawn.py:69` | lease | arena disposed |
| X3 | `on_spawn` raises, child killed and reaped | `execution/_isolated_spawn.py:106` to `:116` | prep only; child `_decoy_stage_*`, `_decoy_hold_*` left | arena disposed after reap |
| X4 | timeout, child killed | `execution/_isolated_spawn.py:89` to `:105` | same leftover as X3 | arena disposed after reap |
| X5 | child killed externally or by the kernel, no envelope | `execution/_isolated_spawn.py:118` to `:146` | same leftover | arena disposed |
| X6 | child self-reports `crashed` / `oom_killed` | `execution/_isolated_run.py:449` to `:464` | prep rmtree; inner stage relies on the child's own best-effort abort | arena disposed |
| X7 | malformed child envelope | `execution/_isolated_run.py:504` to `:515` | lease | arena disposed |
| X8 | gate refusal (checks, tables, footer) | `execution/_isolated_run.py:539` | lease | arena disposed |
| X9 | declared table unreadable | `execution/_isolated_run.py:547` to `:551` | lease | arena disposed |
| X10 | promotion refused / conflict / cancelled | `execution/_isolated_commit.py:162` | lease | arena disposed |
| X11 | promotion `commit_uncertain` | `publication/_local_directory.py:386`, `:395` | prep renamed; lease no-op | arena disposed; bound dir untouched |
| X12 | `already_committed` | `execution/_isolated_commit.py:163` | `cleanup_staging` + lease | arena disposed |
| X13 | published | `execution/_isolated_run.py:555` | prep renamed away | arena disposed (now empty of data) |
| X14 | `KeyboardInterrupt` in the parent while waiting | `execution/_isolated_spawn.py:106` | lease | arena disposed after reap |
| X15 | parent process killed | none | everything left, including caller `staging_dir` payload | not closable in-process: arena is reclaimable by `reclaim_orphan_arenas` (section 5.1, open question Q1) |
| X16 | no `output_dir` | `execution/_isolated_commit.py:177` to `:179` | prep under work root; inner stage in work root, kept if caller supplied `staging_dir` (`execution/_isolated_run.py:395`) | arena under the run's scratch; same disposal |

In-process sinks (`execution/_transactional_sink.py`) driven by routes:

| Id | Exit | Code | Target state |
|---|---|---|---|
| S1 | write raises | `:259` to `:263`, `:288` to `:308` | FAILED; route aborts (`execution/_sequential.py:635`, `execution/out_of_core/_runner.py:329`) |
| S2 | abort cleanup fails | `:384` to `:389` | ABORTED with `cleanup=failed`; path stays handle-plane |
| S3 | commit refused | `:613` to `:616` | REFUSED |
| S4 | commit uncertain | `:608` to `:612` | UNCERTAIN; nothing disposed |
| S5 | commit raises a non-refusal exception | `:604`, `:606` | today: sealed, re-committable (defect); new: FAILED |
| S6 | publishing process killed | none | arena left; X15 rule |
| S7 | hold-back spill on route failure | `execution/_chunked_output_sink.py:485` to `:488` | spill lives in the arena; disposed with it |
| S8 | quarantine sidecar staged then unlink fails | `quarantine.py:358` to `:405` | sidecar temp lives in the arena |

Managed dataset (`publication/_managed.py`):

| Id | Exit | Code | Target |
|---|---|---|---|
| M1 | generation renamed, then `fsync_dir` or pointer temp write fails | `publication/_managed.py:398` to `:416` | remove `generations/<d>` this attempt created, under the lock (P3) |
| M2 | pointer switch fails, not visible | `publication/_managed.py:417` to `:438` | same as M1 |
| M3 | pointer switch fails, visible | `publication/_managed.py:422` to `:428` | UNCERTAIN; generation kept |
| M4 | protected backup fails | `publication/_managed.py:308` to `:343` | backup temp in the arena |
| M5 | restore stage fails | `publication/_managed.py:491` to `:499` | restore stage in the arena |

### 4.2 Execution routes that publish

- sequential: `execution/_sequential.py:591` (commit), `:635` (abort).
- out-of-core: `execution/out_of_core/_runner.py:313` (commit), `:329` (abort); scratch `:209` in `TMPDIR`.
- full-frame and mixed through `OutputPublish` (`execution/_chunked_output_sink.py:129`): `execution/_pipeline_full_frame.py:243`, `execution/_pipeline_mixed_route.py:264`.
- streamed generation: `generation/_streamed.py:475`.
- isolated parent promotion: `execution/_isolated_run.py:555`.
- in-process fallback: `execution/_isolated_run.py:301` to `:313`.
- `publish_tables`: `execution/_transactional_sink.py:664`.
- managed unit: `execution/_transactional_sink.py:644` (staging parent `.staging`, `:656`).

### 4.3 FK shapes

- N/A as a per-shape matrix, because F22 binds the whole unit in one rename (`execution/_transactional_sink.py:416`, `publication/_managed.py:400`), so single, composite, self-referencing and cross-table FKs all publish or fail together.
- One test proves it for the cross-table shape (parent and child in one unit, kill between their writes): `test_fk_parent_child_bind_together`.

### 4.4 Fields of public result types (plane classification)

- `CommitReceipt` (`publication/_types.py:136` to `:155`): all report plane: `outcome`, `operation_id` (shape-checked), `reason_code` (registered base token; errno moves to a new `os_error` field holding only an `errno.errorcode` name), `result_id`, `descriptor_digest`, `evidence_digest`, `artifacts` (`ArtifactSummary`), `is_current`, `visible`, `durable`, `backup_status`, `cleanup_status`, `generation` and `backup_ref` (digest-only relative refs; asserted to match `^(generations|backups)/[0-9a-f]{64}$`).
- `AbortReport` (`publication/_retention.py:32` to `:41`): report: `status`, `reason`, `record_id`, `classification`; handle: `retained_path`, `resource`.
- `RetainedRecord` (`publication/_retention.py:44` to `:54`): report: `record_id`, `reason_code`, `classification`, `expires_at`, `holds`; handle: `path`; caller-identity (shape-checked, report): `owner`, `operation_id`, `attempt_id`.
- `CleanupResult` (`publication/_retention.py:57` to `:60`): report.
- `ResolvedGeneration` (`publication/_managed.py:70` to `:77`): handle (the reader protocol needs the path): `path`, `artifacts` (`ArtifactRef.path`), `doc`; report: `descriptor_digest`, `result_id`.
- `IsolatedRunResult` (`execution/_isolated_common.py:402` to `:429`, plus `main:` `reason`): report: `outcome`, `peak_rss_mb`, `returncode`, `signal_number`, `error` (already safe text), `isolated`, `pid`, `publication_receipt` (via its own `to_report`), `reason` (F29 payload), `memory_evidence` (value-free per platform `api/jobs/memory_evidence.py:4`), `table_kinds` (keys through `approve_ref`, values closed enum); data: `outputs`, `quality_metrics`, `extra`, `row_errors`; handle: `committed_output_dir`.
- `ExecutionResult.publication_receipt` (`execution/_adapter.py:75`) and `GenerationResult.publication_receipt` (`generation/_streamed.py:89`): report via `CommitReceipt.to_report`.

### 4.5 Output surfaces

- Receipts and refusal exceptions: `CommitReceipt`, `PublicationRefusedError.receipt` and `str()` (`publication/_managed.py:408`, `:430` use `os_error_label`, safe).
- `IsolatedRunResult` and its F29 `reason` (`main:src/decoy_engine/execution/_isolated_reason.py:27`).
- `AbortReport` on `sink.abort_report` (`execution/_transactional_sink.py:389`).
- Persisted retention `meta.json` (`publication/_retention.py:121` to `:132`).
- Persisted descriptor and evidence core inside the unit (data plane by design: they bind real relative paths, `docs/publication.md:52`).
- Child result envelope `result.json` (child to parent, untrusted; `execution/_isolated_worker.py:52` to `:65`).
- Logs: `execution/_isolated_commit.py:89` (constant text), `execution/_isolated_run.py:227`, `:235` (constant text).
- Progress channel `publish_stored(table, size)` (`main:src/decoy_engine/execution/_transactional_sink.py`, F29 `_stored`): table via the channel's own policy; F29-owned.
- Explain output `execution/_planner.py:238` interpolates a table name with `!r`: not an F22 surface; F22 docs stop claiming an engine-wide property; routed to F31 (Q3).

## 5. Design

### 5.1 Attempt arena (closes classes 2, 4, 7)

New module `publication/_arena.py` (about 180 LOC). Pattern: `tempfile.TemporaryDirectory` scoped ownership, plus the fd-relative, symlink-safe removal that `shutil.rmtree` uses on platforms with `os.open(dir_fd=...)` (`shutil._rmtree_safe_fd`), plus SQLite's rule that a journal lives beside its database on the same filesystem.

- `open_attempt(destination, *, operation_id) -> AttemptArena`: `mkdtemp(prefix=".decoy-attempt-", dir=destination.parent)` (or `<root>/.staging` for a managed unit), mode 0700, opened and held as an `O_DIRECTORY | O_NOFOLLOW` fd; an `owner` file holds `pid` and an `flock` kept for the arena's lifetime.
- `ArenaLayout` (frozen): `prep`, `stage`, `hold`, `sidecar`, `scratch`, `work`. Producers receive the layout; nothing else yields a writable temp path.
- `dispose() -> DisposalReport(status: "confirmed" | "failed", leftover_count: int)`: fd-relative walk, never follows links, idempotent, thread-safe. The report has no path; the arena's path is handle-plane.
- `reclaim_orphan_arenas(parent)`: removes only `.decoy-attempt-*` whose `owner` lock can be taken non-blocking (owner process dead). Never a prefix sweep without the lock proof (Codex's warning). Ships behind Q1.
- Isolated runs: the payload carries the layout; the child builds `ParquetPreparationSink(layout)`, so its staging is `layout.stage`, its spill parent is `layout.hold`, and its env sets `TMPDIR=layout.scratch` so DuckDB and the out-of-core runner scratch (`execution/out_of_core/_runner.py:209`) are inside the arena. A caller-supplied `staging_dir` gets a fresh `work` subdirectory, never the caller directory itself (closes X16 leftovers of `payload.json` and source Parquet). Scratch on the destination filesystem changes where out-of-core spills land; the disk budget must be measured at `layout.scratch` (`execution/_pipeline_route_exec.py:254` to `:266`), see pre-mortem R4.
- Managed M1/M2: `_promote_and_switch` records `created_generation=True` only when its `os.replace` succeeded (a non-empty target makes it fail, so success means this attempt created it) and removes it under the lock when the pointer does not reference it.

### 5.2 Sink state machine (closes class 3)

Split `_transactional_sink.py` (684 LOC) into `_sink_state.py` (enum, table, `_enter` / `_leave`, about 160), `_preparation_sink.py` (about 120), `_publishing_sink.py` (about 330) and a thin `_transactional_sink.py` re-export (protocol, adapter, `publish_tables`, about 150). The table is a literal dict; the docstring and `docs/publication.md` render it and a test asserts equality.

| state \ op | write / sidecar | record_validation | commit | lookup / reconcile | abort |
|---|---|---|---|---|---|
| OPEN | -> WRITING | ok | -> COMMITTING | -> RECOVERING | -> ABORTED (dispose) |
| WRITING | -> WRITING (count) | ok (as r5) | busy `write_in_progress` | busy `write_in_progress` | defer |
| COMMITTING | busy `commit_in_progress` | busy | busy | busy | defer |
| RECOVERING | busy `recovery_in_progress` | busy | busy | busy | defer |
| SEALED | refuse | refuse | same receipt | -> RECOVERING (read-only) | no-op |
| UNCERTAIN | refuse | refuse | refuse `commit_uncertain` | reconcile only -> RECOVERING | no-op |
| REFUSED | refuse | refuse | refuse `attempt_rejected` | refuse | -> ABORTED (dispose) |
| FAILED | refuse | refuse | refuse `attempt_invalid` | refuse | -> ABORTED (dispose) |
| ABORTED | refuse | refuse | refuse `attempt_aborted` | refuse | no-op |

Leaving in-flight states: WRITING -> OPEN on success, FAILED on any exception. COMMITTING -> SEALED (published, already_committed), UNCERTAIN (`CommitUncertainError`), REFUSED (`PublicationRefusedError`), FAILED (any other exception, closing S5). RECOVERING -> prior state on a `None` result or a non-refusal exception (read-only, nothing changed); from UNCERTAIN, a `reconcile` that finds the binding -> SEALED with that receipt, one that finds none -> REFUSED `commit_not_visible`; a refusal -> REFUSED. A deferred abort runs in `_leave` only if the state reached is OPEN, REFUSED or FAILED. Busy refusals never change state. Admission and `validate_request` run in `_enter` under the lock only as pure checks; `inspect_destination` I/O runs after the transition. `ParquetPreparationSink` uses the same table (SEALED abort is a no-op, closing the prep-sink defect).

### 5.3 Report plane (closes class 1)

- `publication/_report.py`: `FIELD_PLANES: dict[type, dict[str, Plane]]` for every type in section 4.4, and `to_report(obj) -> dict` that walks only `report` fields, recursing into registered types, rejecting any value whose type is not `str` of a closed vocabulary, `int`, `float`, `bool`, `None`, an `Enum`, or a registered type. Strings must pass one of: registered reason code, errno name, hex digest, shape-checked caller id, F29 `approve_ref` (identifier from config provenance only), or the digest-ref pattern.
- Every public type gets a `to_report()` method delegating to it. `IsolatedRunResult.to_report()` uses F29's `ReasonRecord.to_payload()` for `reason`.
- `PublicationRequest.__post_init__` shape-checks `operation_id`, `attempt_id`, `job_id` (`^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$`).
- Child-supplied names never enter a report: refusals name a defect class (kept from r5).
- Docs state the guarantee for `to_report()` only, and say `dataclasses.asdict` of a result is not a safe surface; a sentry forbids `asdict` / `json.dumps` of these types inside the engine outside `_report.py`.

### 5.4 Reuse inventory

- F29 `reasons` registry, `ReasonRecord`, `ReasonContext`, `approve_ref`, `Operation`, `PublicationState`, `CleanupState` (`main:src/decoy_engine/reasons/_record.py:43` to `:72`): the only vocabulary for publication and cleanup state.
- F29 `_publication_observer.py` (`main:src/decoy_engine/execution/_publication_observer.py:42`, `:99`, `:114`): `PublicationEvidence` becomes a projection of `SinkState` plus the `DisposalReport`.
- F29 `_isolated_reason.commit_reason` (`main:src/decoy_engine/execution/_isolated_reason.py:41`): changed to take the `DisposalReport` instead of `staging.exists()`.
- F29 sentries `tests/sentry/test_reason_registry.py`, `_reason_scan.py` and fixtures: new refusal sites get producer-inventory entries.
- Engine sentries: `tests/sentry/test_module_size.py`, `tests/sentry/test_physical_seam_disconnection.py`, `tests/sentry/test_publication_commit_boundary.py` (extended with the arena sentry).
- `publication/_tokens.py` errno tokens; `publication/_child.py` bounded gate (kept verbatim).
- F31 boundary: not landed; `approve_ref` is F29's documented plug-in seam for it (`main:src/decoy_engine/reasons/_record.py:12` to `:14`). No local boundary is built.
- F25 (protected scratch, quotas, orphans; plan on `main`): owns long-term orphan reclamation policy; `reclaim_orphan_arenas` is the minimal hook (Q1).

## 6. Hostile corpus (acceptance tests, red first)

Every test below is written and recorded red on the slice's starting commit before the fix. Each has a non-vacuity assertion.

### 6.1 Real child killed at every phase

`tests/unit/publication/test_arena_real_child_kill.py`. Real `decoy_engine.execution._isolated_worker`, no fake worker. Fault injection without a production hook: the test prepends a directory holding a `sitecustomize.py` to the child's `PYTHONPATH` (the spawn copies `os.environ`, `execution/_isolated_spawn.py:68`); it wraps `pq.ParquetWriter.write_batch`, `pq.write_table`, `os.replace` and `_Spill.add` to write a `reached-<phase>` file listing the arena's current entries, then block on a FIFO. The test kills the child with SIGKILL from an `on_spawn` thread after the phase file appears. Phases: K1 before first write; K2 mid `write_batches` of table 1 (seeded name `TBLMARK_<hex>`); K3 hold-back spill active; K4 after table 1 closed, before table 2; K5 after the prep sink's rename, before the envelope; K6 after the envelope, before exit. Same matrix with timeout and with `on_spawn` raising. Assertion: after `run_pipeline_isolated` returns, a walk of `tmp_path` finds no `TBLMARK` file, no `.decoy-attempt-*`, no `_decoy_*`, and nothing but the lock file beside `output_dir`; the result reports `cleanup_state=confirmed`. Non-vacuity: the `reached-<phase>` listing contains the seeded file in its arena subdirectory (K2 to K5) and a `hold/` entry at K3. Red on `2fa4f8a5` for K2 to K5 (the inner stage is outside the lease).

### 6.2 Barrier interleavings

`tests/unit/publication/test_sink_interleavings.py`, generated from `TRANSITIONS`. Harness: monkeypatch the backend call of the in-flight op (`_pub.lookup_committed`, `_pub.reconcile`, `_pub.prepare_directory`, `pq.write_table`) to set `entered` and wait on `release`. For every in-flight op X in {write, commit, lookup, reconcile} and every arriving op Y in {write, record_validation(fail), commit, lookup, reconcile, abort}: start X, wait `entered`, call Y on another thread, assert Y's outcome equals the table cell, release, assert X's result and the final state. Required named cases:
- I1 recovery vs `record_validation(FAIL)`: validation is busy; recovery returns from the old request; a following `commit()` is refused by the failing check. Red today (Codex reproduced the stale `already_committed`).
- I2 recovery vs `commit()`: commit busy `recovery_in_progress`, not latched.
- I3 recovery vs `abort()`: abort deferred; recovery result returned; then state ABORTED and arena disposed; the result never has `aborted` before it. Red today.
- I4 recovery vs a second recovery: busy.
- I5 commit vs commit, commit vs abort, write vs commit (r5 cases, kept).
- I6 commit raising a non-refusal exception then a second `commit()`: refused `attempt_invalid`. Red today (S5).
- I7 prep sink sealed then `abort()`: state stays SEALED. Red today.
Non-vacuity: each case asserts `entered.is_set()` before Y and that the call Y blocked zero times on the sink lock for longer than the barrier (the lock is not held across X's I/O).

### 6.3 Marker corpus over the complete payload

`tests/unit/publication/test_report_plane.py`. Markers: `PATHMARK` in `output_dir`, the caller `staging_dir` and the retention store; `OSMARK` as the message of an injected `OSError(ENOSPC, ...)` at rename, pointer switch, backup, fsync, arena dispose; `TBLMARK` as a child-supplied undeclared table and as a config table; `CELLMARK` in data values; `DSNMARK` in the config's connection string. For every exit in section 4.1 and every type in section 4.4, build the instance, `json.dumps(obj.to_report())`, assert no marker, except `TBLMARK` (config provenance) only under an `object_ref` key. The field walk test asserts `FIELD_PLANES` covers every `dataclasses.fields()` entry of every public type and every type reachable from them. Non-vacuity: the same instances' handle and data fields do contain the markers (proof the seed reached the object), and a deliberately unregistered dummy field makes the walk fail.

### 6.4 Other red-first tests

- `test_inprocess_exit_matrix`: S1 to S5, S7, S8 per route in section 4.2; arena gone or `cleanup=failed`.
- `test_managed_orphan_generation`: inject failures at M1 and M2; `generations/` holds only referenced digests (red today).
- `test_no_temp_outside_arena` (AST sentry over `execution/`, `publication/`, `generation/`, `quarantine.py`): no `mkdtemp`, `mkstemp`, `TemporaryDirectory`, `NamedTemporaryFile` outside `publication/_arena.py`, with a reasoned allow-list (the profiler and benchmark scripts are outside the scan roots).
- `test_arena_swap_is_not_followed`.
- `test_f29_cleanup_state_after_disposal`: a promotion refusal gives `reason.context.cleanup_state == confirmed` (red on a naive merge, where `commit_reason` checks `staging.exists()` before the lease exits).
- `test_publication_state_mapping` (section 9.2).
- `test_operation_id_shape`.
- `test_child_row_errors_bounded`.
- `test_docs_table_matches_transitions`.

### 6.5 Ten ways a gate reviewer breaks it (each has a test above)

1. Kill the real child mid-spill (6.1 K3). 2. Swap the arena for a symlink (6.4). 3. Validation FAIL during lookup (I1). 4. Abort during reconcile (I3). 5. Commit twice after an `OSError` (I6). 6. `json.dumps(asdict(result))` (sentry plus 6.3 docs scope). 7. `operation_id="/srv/PATHMARK"` (6.4). 8. Pointer switch failure leaves a generation (6.4). 9. Caller `staging_dir` keeps source Parquet (6.1 assertion over `tmp_path`). 10. Merged F29 reports `cleanup_state=failed` after a clean disposal (6.4).

## 7. Frozen contracts touched

- `decoy-publication/v1` descriptor and evidence core: unchanged.
- F29 reason manifest v1 and consumer contract (`main:tests/fixtures/f29/reason-manifest-v1.json`, `consumer-contract.json`): additive only: a `publication` group of codes for receipt reason tokens and `recovery_in_progress`, `commit_not_visible`, `arena_cleanup_failed`. No rename, no removal.
- `IsolatedRunResult`: additive (`publication_receipt` beside F29 `reason`; `memory_evidence` stays last per `main:` comment).
- `CommitReceipt`: not yet released (F22 unmerged); changed (errno split to `os_error`) then frozen by `test_commit_receipt_schema_snapshot`.
- `TransactionalSink` protocol (`execution/_transactional_sink.py:84`): unchanged methods; `ArenaLayout` is an optional constructor argument.
- `docs/compatibility-contract.md`: the F22 entry is rewritten to the arena and report-plane claims.

## 8. Behavior changes (pre-GA, hard delete)

- Staging, spill and scratch move from `target.parent` siblings and `TMPDIR` into `.decoy-attempt-*`.
- `StagingLease` and `sibling_staging_dir` are deleted, not adapted.
- `staging_dir=` is a scratch parent; the engine never leaves `payload.json` or source Parquet in it.

## 9. F29 integration

### 9.1 Strategy

Re-apply, do not rebase. Cut `f22/rebuild` from current `main` (at least `fdd08a8`). Slice F0 copies the new `publication/` package and its tests from `2fa4f8a5` (new files, no textual conflict), then the execution-side files are rebuilt on F29's versions under sections 5.1 to 5.3. Reason: the conflicting files (`_transactional_sink.py`, `_isolated_common.py`, `_isolated_run.py`, `_isolated_worker.py`) are exactly the ones this plan restructures, so a rebase would resolve conflicts in code that is then rewritten, and a merge commit would hide the semantic overlaps listed below. `claude/wizardly-allen-8ymekg` stays as the reference; nothing is pushed to it.

### 9.2 What stays true on both sides

| Fact | F29 rule kept | F22 rule kept | Reconciliation |
|---|---|---|---|
| Failure identity | `IsolatedRunResult.reason` set on every non-completed result; governor reroutes on typed reason (`main:` `_isolated_common.py` removes route markers) | receipt carries the publication outcome | both fields; F22's port drops `preserved_route_markers` (present at `execution/_isolated_common.py:261`) |
| Publication state | `PublicationState` closed enum | receipt outcomes | published, already_committed -> `published`; rejected, conflict, unsupported, cancelled_before_commit -> `not_published`; commit_uncertain -> `unknown`; F29's `observed_commit` sets `published` on return, which is right for both success outcomes |
| Cleanup state | `CleanupState` closed enum; `confirmed` only when checked | arena disposal | from `DisposalReport.status` after disposal; `commit_reason` takes the report |
| Table identifiers | `approve_ref` permits config identifiers in `object_ref` | r5 said "never table names" | F22 adopts F29: config identifiers only, only in `object_ref`; never child-supplied, never path-derived (Q2) |
| Sink evidence | `PublicationEvidence` mutated at write/close/commit/abort | `SinkState` | evidence is derived from `SinkState`; no second flag set |
| Stored bytes | `_stored` publishes `stored_output` progress | arena staging | `_stored` reads the size inside `layout.stage`; unchanged event |

## 10. Slices

Each slice is separately gateable on `decoy-engine`, branch `f22/rebuild`, red tests in their own commit first.

| Slice | Content | Size | Acceptance (red first) |
|---|---|---|---|
| F0 | port `publication/` package and its unit tests onto `main`; drop `preserved_route_markers` users; census | ~0 new LOC, port only | ported suites green; F29 sentries green |
| F1 | `_sink_state.py` + module split + RECOVERING + exception exits | ~450 moved, ~200 new | 6.2 all, `test_docs_table_matches_transitions` |
| F2 | `publication/_arena.py` + in-process routes + managed M1/M2 | ~350 | 6.4 in-process matrix, managed orphan, AST sentry, swap test |
| F3 | isolated parent and child on the arena; `TMPDIR` and disk budget at `layout.scratch`; caller `staging_dir` | ~250 | 6.1 full matrix |
| F4 | `_report.py`, `FIELD_PLANES`, `to_report()`, id shape checks, F29 codes, mapping | ~300 | 6.3, `test_f29_cleanup_state_after_disposal`, `test_publication_state_mapping`, snapshots |
| F5 | docs (`docs/publication.md`, CHANGELOG, compatibility contract), evidence record, archive the r1 to r5 remediation notes | docs | doc claims each cite a test id |

Exact commands per slice (from `uv.lock`, one environment, recorded in the evidence file with `python -V`, `duckdb.__version__`, `pyarrow.__version__`):

```bash
uv sync --frozen --extra dev --extra lint --extra vault --extra cloud --extra xdist
```

```bash
uv run pytest tests/unit/publication tests/sentry -q -p no:cacheprovider -n 4
```

```bash
uv run pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider -n 4
```

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/decoy_engine
```

```bash
python docs/handoffs/tools/plan-lint plan docs/plans/2026-10-30-f22-publication-replan.md
```

The baseline for each slice is the same commands on the slice's starting commit in the same environment; both failure sets go in the evidence file. `scripts/dev/gates.sh` (catalog section 6) does not exist in this repo at `2fa4f8a5` or `fdd08a8`; the commands above stand in for it.

## 11. Worker evidence rules

- Name the class of every finding (section 3 numbering) and fix the class structure, not the instance.
- Write the generated tests first (6.1 to 6.4) and record them red with the exact failing ids.
- Attack your own diff: mutate each mechanism once (the mutation column in section 3) and record the test that turned red.
- Run baselines from `uv.lock` in the same environment as the branch; record Python, DuckDB and PyArrow versions; state what you did not run.
- Never widen a threshold, skip a test, weaken an assertion or raise a census number to pass.
- If a defect of a class already in the catalog appears a third time, stop and report it to the owner with a class-level proposal.

## 12. Pre-mortem

- **R1: arena on the destination filesystem fills the output disk with scratch.** Out-of-core scratch moves from `TMPDIR` to `layout.scratch`. Mitigation: F3 measures the disk budget at `layout.scratch`; if the owner prefers scratch on `TMPDIR`, the arena gets a second, scratch-filesystem root owned by the same `AttemptArena` (two roots, one owner, one disposal). Q4.
- **R2: `sitecustomize` injection is fragile across interpreters.** If the child runs with `-I` or `-S` later, injection silently stops and 6.1 becomes vacuous. The non-vacuity assertion (phase file must exist) makes that a failure, not a pass.
- **R3: the state table grows a ninth column and a hand-written guard reappears.** `_enter` / `_leave` are the only callers of the table; an AST sentry forbids reading the state attribute outside `_sink_state.py`.
- **R4: F29 lands more changes during the rebuild.** F0 is cut from the newest `main`; each slice rebases onto `main` (its own commits only, all on one branch).
- **R5: report-plane strictness rejects a legitimate value** (for example a long operation id from the platform). The shape check fails at request construction with a typed refusal, before any write, never at report time.
- **R6: `reclaim_orphan_arenas` deletes a live attempt.** Only with the owner `flock` taken non-blocking; NFS without `flock` refuses (no fallback), matching `publication/_lock.py:1`.
- **R7: a third round.** Most likely cause: a route not in section 4.2 that stages output (a new producer). The AST sentry in 6.4 is the guard; a new producer must take an `ArenaLayout` or the sentry fails.

## 13. Open questions for the owner

- Q1: F22 or F25 owns `reclaim_orphan_arenas` for a killed publishing process (X15, S6)? Default: F22 ships the lock-proved primitive, F25 owns scheduling and quotas.
- Q2: adopt F29's rule (config table identifiers allowed in `object_ref` only) and drop r5's "never table names"? Default: yes.
- Q3: explain output (`execution/_planner.py:238`) and `quality_metrics` are value-bearing today and outside F22; route to F31?
- Q4: scratch on the destination filesystem (one root) or on `TMPDIR` (second root)?
- Q5: approve re-application on a fresh branch from `main` instead of rebasing `claude/wizardly-allen-8ymekg`?

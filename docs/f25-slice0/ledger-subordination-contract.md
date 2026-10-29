Status: record

Summary: The contract that makes the engine's local SQLite ResourceLedger a subordinate cache of the platform's durable ownership authority, so there is never a second competing lease source of truth. Grounded in platform source read first-hand.

# F25 ledger-subordination contract

Deliverable 2 of F25 Slice 0. This freezes how the engine's local ownership ledger defers to the platform's durable ownership authority. The guiding rule from the plan (§1.5, §1.8, §4): the platform adapter binds to its existing durable ownership authority and does not run competing local lease decisions; lease expiry alone never authorizes deletion; one authority advances the fence.

All platform anchors below were opened first-hand in `/home/cam/vscode/decoy-platform` at the local checkout.

## 1. Where the real authority lives (verified)

### 1.1 Designated durable authority (built, tested, flag-gated off)

`api/jobs/host_lease_authority.py` is the durable Postgres lease authority for the adaptive job scheduler. State machine `PREPARING -> RUNNING -> TERMINATING -> RELEASED`; only RELEASED frees capacity; heartbeat expiry alone never frees capacity (two-phase reap). Verified entrypoints:

- `committed(db) -> dict` at `:175` (sum of capacity over non-RELEASED leases).
- `claim(db, job_id, worker_id, want: Demand) -> LeaseVerdict` at `:190`. Locks the singleton `host_capacity` row `FOR UPDATE`, inserts a fresh-generation `PREPARING` lease on admit. `LeaseVerdict` carries `decision, lease_id, generation, reason`.
- `advance(db, lease_id, generation, frm, to) -> bool` at `:254`. Fence-aware guarded state transition; refuses when `recovery_fenced_generation == generation`.
- `heartbeat(db, lease_id, generation) -> None` at `:292`. Generation-scoped; guarded against `RELEASED`/`TERMINATING`.
- `reap_once(db, stale_seconds, ...) -> list[...]` at `:310`. Two-phase reaper: fenced takeover of newly-stale leases, then release only when both cgroups confirm empty.
- `claim_priced(db, *, job_id, worker_id, routes, ...) -> ClaimPricedResult` at `:397` (coordinated admit+reserve+mint; returns `lease_id, generation, binding_id`; the caller owns the transaction).

The whole authority is gated by `settings.adaptive_scheduler_lease_authority_enabled` (default False); every entrypoint raises `LeaseAuthorityDisabledError` (`:112`) while off (checked at `:146`). Its docstring states nothing in the platform calls `claim`/`advance`/`heartbeat`/`reap_once` yet.

Durable store schema in `api/jobs/host_lease_models.py`:
- `HostCapacity` at `:117`, singleton allocatable ceiling (`alloc_mem_mb/alloc_cores/alloc_disk_mb`, `admission_drained`).
- `HostJobLease` at `:153`, one row per fenced generation. `UniqueConstraint(job_id, generation)` at `:165`; `recovery_fenced_generation` nullable at `:193`; write-once `intent_id` and `result_acked_at` (documented `:63`-`:70`). `job_id` is deliberately an opaque Text, not a foreign key.
- `DispatchPlanBindingRow`, 1:1 per lease, carries `disk_budget_mb` and `local_stage_root` (the admitted local staging root).

Recovery primitives:
- `api/jobs/host_lease_recovery.py`: `fenced_takeover(...)` at `:89`, `has_live_lease(db, job_id) -> bool` at `:146`, `release_after_confirmed_empty(db, lease_id, generation) -> bool` at `:193`.
- `api/jobs/lease_recovery_engine.py`: `recover_lease_and_reconcile(...)` at `:348`.

### 1.2 Live-today authority (shipped, flag independent)

Until the lease authority flag is enabled, the authority that actually runs in production is:
- `api/jobs/reaper.py`: `reap_stale_workers(db, *, worker_id="")` at `:93` (heartbeat liveness reaper) and `release_orphaned_terminal_locks(db, *, cutoff)` at `:249` (releases `JobTargetLock` rows orphaned by a dead worker on terminal jobs). This is the live orphaned-lock recovery today.
- `JobTargetLock` (`api/models.py`), the output-target collision registry; release via `api/jobs/target_locks.py:release_target_locks`.
- The durable reservation ledger on `SchedulerState` (`api/models.py`): memory `held_bytes`/`budget_bytes` and the scratch/disk mirror `held_scratch_bytes`/`scratch_budget_bytes` (constraints around `:848`-`:863`). A NULL `scratch_budget_bytes` means disk gating is not provisioned, never unlimited.

### 1.3 Names that are provenance tags, not code (so the engine does not bind to them)

- `G14`, `P-C4`, `MX-04` exist only as portfolio/provenance identifiers in `docs/ROADMAP.md` and the MASTER spec and review docs. There is no platform code symbol for them. `G14` also collides with a dropped legacy engine feature name; ignore that collision. The engine binds to the modules in §1.1/§1.2, not to these tags.
- There is no class named `ResourceLedger` in the platform. The real durable disk/quota ledger is the `scheduler_state` scratch columns; the per-worker in-process memory ledger is `MemoryLedger` in `api/jobs/worker_budget.py`. The F25 engine `ResourceLedger` is a new engine-local deliverable (plan §1.5), not an existing platform class.

### 1.4 S10/S11 localstage (the staging-root seam)

`docs/plans/2026-09-26-s10-s11-tmpdir-localstage.md` exists. The admitted LOCAL_STAGE root is persisted on `DispatchPlanBindingRow.local_stage_root` and written at claim time. Disk-quota admission (`admit_disk_quota`) is built but inert in production (no non-test caller yet; real wiring tracked as S11b). The engine arena root for an attempt is the `local_stage_root` the platform admitted, not a path the engine chooses.

## 2. The contract

### 2.1 Single source of truth

The platform `HostJobLease` row (keyed by `job_id`, fenced by `generation`) is the sole authority for whether an attempt owns its resources. The engine's local SQLite `ResourceLedger` is a subordinate cache and local-enforcement record. It records arena identities, quota-domain ids, fscrypt policy/key-version references, resource kinds, retention classes, and lifecycle states for resources on this host. It does not mint, renew, expire, or adjudicate ownership. When the two disagree, the platform authority wins and the engine ledger is reconciled to it.

### 2.2 What the engine receives, never decides

For each attempt the platform supplies, through the worker handoff (see `lifecycle-and-spool-contracts.md`), the tuple the engine treats as read-only authority input:

- `job_id` (opaque), `lease_id`, `generation` (the fence), and the admitted `local_stage_root`.
- The disk budget (`disk_budget_mb` on the binding) and inode budget, which the deployment adapter has already turned into a kernel project quota on the arena root before any writable capability is issued.

The engine writes its ledger rows stamped with this `(job_id, lease_id, generation)` and the root device/inode identity. It never invents a `generation`, never advances the fence, and never treats a local row as proof of ownership if the platform generation has moved on.

### 2.3 Renewal and liveness

Lease liveness is the platform's `heartbeat(db, lease_id, generation)`. The engine does not keep a second heartbeat clock. The engine holds a kernel advisory liveness lock on the arena (plan §1.8) for the lifetime of open native handles; that lock is a local writer-presence fact, not an ownership claim. Ownership is the lease; writer-presence is the lock. A sweep needs both a successful platform-authority decision and the exclusive local lock.

### 2.4 Deletion is never an engine decision alone

The engine exports a bounded sweep (plan §1.8) but may delete an arena only when all of the following hold:
1. The platform authority says the owning lease is reap-eligible. Under the enabled flag this is `recover_lease_and_reconcile` / `reap_once` returning a released outcome; under the live-today authority this is `release_orphaned_terminal_locks` / `reap_stale_workers` having released the owning worker's target locks, plus the job being in a terminal state.
2. The engine then acquires the exclusive kernel liveness lock and revalidates identity and generation immediately before destructive work.
3. No valid checkpoint, PUBLISHING, PUBLISHED, or unknown state is attached.

Lease expiry alone is only candidate selection. PID existence, PID absence, mtime, and directory-name prefix are never deletion authority. This mirrors the platform's own rule that heartbeat expiry never frees capacity by itself.

### 2.5 Two bindings, one decision

Because the designated authority is flag-gated off, the adapter exposes one `OwnershipAuthority` seam with two implementations that make the same decision:

- `PlatformLeaseAuthority` (flag on): reads/writes through `host_lease_authority` + `host_lease_recovery` + `lease_recovery_engine`. Generation is `LeaseVerdict.generation`; recovery is `recover_lease_and_reconcile`.
- `TargetLockAuthority` (flag off, live today): treats the `JobTargetLock` + `reaper.py` + terminal-job state as the ownership signal; the engine arena is reap-eligible only once the platform has released the owning worker's locks.

The engine binds to the seam, not to either implementation directly, so enabling the flag later is a platform-side switch, not an engine rewrite. The engine ledger schema carries the lease identity in both modes, so no record written under one mode is misread under the other (plan Slice 1 requires the full retention/state schema up front for exactly this reason).

### 2.6 Prohibited

- No second lease store. The engine SQLite ledger must not contain a `generation` or lease the platform did not issue.
- No engine background daemon and no import-time cleanup (plan §1.8).
- No deletion driven by the engine's own clock, PID checks, or path prefixes.
- No reconciliation that resurrects a local row over a newer platform generation.

## 3. Open items to resolve before the consuming slice

- The exact worker-to-engine handoff of `(lease_id, generation, local_stage_root, disk_budget_mb)` is a worker-lifecycle interface; it is design-frozen in `lifecycle-and-spool-contracts.md` but has no shipped carrier yet.
- Whether Slice 5 (orphan recovery) ships first against `TargetLockAuthority` (live) or waits for the lease-authority flag is a sequencing decision for the plan owner. The contract supports both; the recommendation is to build and test against both implementations behind the seam and let the platform flag decide at deploy time.
- S11b (disk-quota admission wiring) is the platform work that makes `disk_budget_mb` a live admitted value rather than an inert field. The engine can consume it as soon as it is populated; until then the deployment adapter sets the project quota directly from the provisioned pool size.

Status: record

Summary: The design-level contracts for the worker/CLI lifecycle hooks (secret injection, lease maintenance, sweep invocation) and the caller-side cloud-to-local spool, frozen at F25 Slice 0 and grounded in real engine and platform anchors.

# F25 lifecycle and cloud-to-local spool contracts

Deliverable 3 of F25 Slice 0. These are interface contracts, not implementations. They name the real seams the engine exposes and the real caller surfaces that must drive them. Where a caller surface does not exist yet, it is marked as such rather than invented (plan §4: these are "asserted, verify before build").

## 1. Worker / CLI lifecycle hooks

The engine is a library. It does not acquire root, mount volumes, hold a daemon, or run cleanup at import time (plan §1.5, §1.8). All privileged setup and all scheduling of lifecycle calls belong to the caller (the platform worker or the CLI). The engine exposes four hook points; the caller owns when they fire.

### 1.1 Provision (caller, before any engine write)

The deployment adapter prepares a qualified ext4 pool (encryption + project quotas + metadata headroom + a protected control-storage arena), and for each attempt assigns the project id, inheritance, hard byte and inode limits, encryption policy, and key access on the arena root before issuing any writable capability. This is the platform half of the strict profile the Slice-0 report qualified. The engine receives an already-qualified root; it verifies the qualification (project quota present, encryption policy present, single-device placement) and fails closed with `scratch_backend_unavailable` if any check fails (plan §1.5; Slice-0 report §6 condition 1: engine execution runs unprivileged).

Anchor for the staging root: the platform admits a `local_stage_root` on `DispatchPlanBindingRow` (`decoy-platform api/jobs/host_lease_models.py`); the engine uses that as the arena root, not a self-chosen path.

### 1.2 Secret / key injection (caller to engine, out-of-band)

The arena data key and the scratch-wrapping-key reference reach the engine only through an inherited restricted channel: a dedicated pipe or an inherited file descriptor cleared after a single read. Never through argv, environment, a discoverable path, configs, manifests, or the isolated-run `payload.json` (plan §1.5 A04 transport security, §1.7). A subprocess child receives the arena capability and the liveness-lock file descriptor by the same restricted-fd transport, never by argv/env/path.

Engine reuse anchors (no new crypto dependency): `src/decoy_engine/keyprovider.py:123` is the existing secret-provider boundary; `src/decoy_engine/context.py` delegates HKDF to the canonical implementation; `src/decoy_engine/vault.py` uses the installed Fernet stack. The F25 key envelope reuses these, not the vault's private functions or file format (plan §1.6).

Platform secret-provider carrier: F13 (ROADMAP `[V1][PARTIAL]`, "env/file; api/keys/ KMS merged"). The engine binds to the `keyprovider` boundary; the platform supplies the actual key service. The exact worker-to-engine key handoff call is design-frozen here and has no shipped carrier yet.

### 1.3 Lease maintenance (caller only)

The caller maintains the lease through the platform authority (`host_lease_authority.heartbeat(db, lease_id, generation)` when the flag is on; the live reaper path otherwise). The engine does not keep a second heartbeat. The engine's only liveness artifact is the kernel advisory lock it holds on the arena for the lifetime of open native handles, which signals writer-presence, not ownership (see `ledger-subordination-contract.md` §2.3). Every child that can write holds or inherits that lock independently of parent survival; an untracked subprocess is inadmissible.

### 1.4 Sweep invocation (caller schedules, engine executes bounded)

The engine exports one bounded sweep and one startup reconciliation entrypoint (plan Slice 5). The caller decides when to call them (worker boot, periodic maintenance, CLI reconcile command). The engine never sweeps on import and never on a timer of its own. A sweep deletes an arena only under the joint condition in `ledger-subordination-contract.md` §2.4 (platform authority reap-eligible, plus exclusive local lock, plus no valid checkpoint/publication/unknown state). The current cleanup sites the sweep will replace or wrap are `src/decoy_engine/execution/out_of_core/_runner.py` (subtree deletion), `src/decoy_engine/execution/_transactional_sink.py` (stage cleanup), `src/decoy_engine/execution/_isolated_run.py`, and `src/decoy_engine/connectors/postgres_snapshot.py`.

Platform recovery that must consume the same decision rather than independently deleting paths: `reaper.release_orphaned_terminal_locks` and `_recover_stuck_jobs_on_restart` (live), `recover_lease_and_reconcile` / `reap_once` (flag-gated). Lock recovery alone must not authorize arena deletion (plan §4, Platform lock recovery row).

### 1.5 CLI surface (out of Slice-0 scope, noted for completeness)

The approved CLI cross-OS amendment (handoff, Cam 2026-10-29) adds the `strict` and `encrypted-volume` profiles with explicit opt-in flags and per-OS verification. That surface follows its own grounding and gate and is not qualified by this Slice-0 report. The lifecycle hooks above are the same for the CLI host; only the provisioning half (§1.1) differs (a user-provided verified encrypted volume instead of a platform-provisioned pool).

## 2. Cloud-to-local spool contract

The plan (§1.3 classifications, §4 connector row) establishes that the cloud source adapters are memory/range backed and do not themselves produce local spool files today:
- `src/decoy_engine/connectors/s3.py:259`, `gcs.py:188`, `sftp.py:321` yield network chunks.
- `src/decoy_engine/profile/_cloud_readers.py:49` is a seekable remote reader that is memory/range backed.

So there is no engine cloud-spool producer to migrate. The contract governs any caller or future code that materializes a cloud source to local disk.

### 2.1 Rules

1. Any local materialization of a cloud source obtains a managed arena file from the ScratchManager before the first byte is written. No naked `/tmp` or home-directory path.
2. The spool consumes exactly one source stream and stops reading the source the moment a quota or write failure occurs. It does not retry into a second unregistered download and does not open a second concurrent stream to the same source for the same spool.
3. On quota failure the spool surfaces the shared `scratch_quota_exceeded` report and releases its handle before the reservation, so a failed spool never leaves a charged-but-orphaned file (plan §1.5 item 6).
4. The spooled file is an intermediate: it stays registered and quota-charged until consumed, then released; it is never silently promoted to a durable output without going through a registered publication stage on the destination filesystem.
5. The in-repo PostgreSQL snapshot connector (`connectors/postgres_snapshot.py`, T13) is the one existing local-materialization case and migrates under Slices 2/3; it is not a cloud spool but obeys the same allocate-before-write and stop-on-failure rules.

### 2.2 What is asserted, not built

The caller-side cloud-to-local spool implementations are asserted in the plan and do not exist in the engine today. This contract is the interface they must implement. No engine spool producer is fabricated for the cloud readers (plan §1.3 explicitly forbids that).

## 3. Summary of real anchors used

Engine (worktree HEAD `33ef629b`): `keyprovider.py:123`, `context.py`, `vault.py`, `connectors/s3.py:259`, `gcs.py:188`, `sftp.py:321`, `profile/_cloud_readers.py:49`, `connectors/postgres_snapshot.py`, `execution/out_of_core/_runner.py`, `execution/_transactional_sink.py`, `execution/_isolated_run.py`.

Platform (local checkout): `api/jobs/host_lease_authority.py` (`heartbeat`), `api/jobs/host_lease_models.py` (`DispatchPlanBindingRow.local_stage_root`), `api/jobs/reaper.py` (`release_orphaned_terminal_locks`, `reap_stale_workers`), `api/main.py` (`_recover_stuck_jobs_on_restart`). F13 secret-provider carrier per `docs/ROADMAP.md`.

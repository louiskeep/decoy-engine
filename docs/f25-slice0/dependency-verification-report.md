Status: record

Summary: F25 Slice 0 hard-gate report. Proves the ext4 + fscrypt v2 + project-quota storage profile is achievable and enforces the F25 guarantees on the designated qualifying baseline, records the pinned versions, binds the engine ledger under the platform ownership authority, and returns an explicit go/no-go.

# F25 Slice 0 dependency-verification report

Scope: Slice 0 only (qualify the storage profile and freeze integration contracts). No engine module from Slices 1-6 is implemented here. This report is the hard gate the plan requires before any later slice starts (`docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md` §3 Slice 0).

- Engine build HEAD: `33ef629b22b7c3dc0ae804e942bbc7d69bd4f844` (worktree `feat/f25-protected-scratch`, off `origin/main`).
- Plan baseline cited in the plan: `2a44723f`. The Slice-0 anchors below were re-opened at HEAD; only cosmetic line drift exists between the two commits for the named files.
- Prototype host: GCP `34.74.130.221` (`decoyf25`), Ubuntu 24.04 LTS, kernel `7.0.0-1014-gcp`, e2-standard-4. The F25 handoff designates this host as the qualifying deployment baseline for Slice 0.
- Raw evidence bundle: `docs/f25-slice0/evidence/20261029T161416Z/` (also on the host at `/root/f25/evidence/20261029T161416Z/` and `.../evidence_20261029T161416Z.tgz`). The prototype script is archived with the bundle as `_prototype_script.sh`.

## 1. Go / no-go

**GO** to build Slices 1-6 against the strict ext4 + fscrypt v2 + project-quota profile, with the conditions in §6.

The deployment can provide the hard boundary F25 requires: kernel project quotas charge and cap native third-party writes (not only Python writes), fscrypt v2 renders scratch unreadable without the key, same-filesystem publication rename preserves the encryption policy, and non-qualifying filesystems fail closed. The owner decision that temp-storage operations hard-fail with `scratch_backend_unavailable` on any host lacking the profile is recorded as GIVEN in the F25 handoff (Cam, 2026-10-29).

Two carry-forwards, neither a blocker for starting the engine slices, both to resolve before the features that consume them land:

1. The platform durable ownership authority (`host_lease_authority.py`) is real, durable, tested source but is gated OFF by default and has no wired runtime caller yet. The ledger-subordination contract (`ledger-subordination-contract.md`) designates it as the authority and defines the binding for both the current live authority and the gated future one, so the engine ledger is never a second competing lease source. This is the plan's standing L2 risk, bounded as intended.
2. The caller-side cloud-to-local spool and the CLI/worker lifecycle hooks have no engine producer today and must be built to the contracts in `lifecycle-and-spool-contracts.md`. They are design-frozen here, not implemented.

No hand-rolled cipher, userspace encrypted filesystem, Python byte-counter, or distributed lock service is proposed or needed. The kernel and fscrypt provide the enforcement; the engine provides only the lifecycle adapter.

## 2. What was proven on the host (first-hand)

Every row ran on the GCP host. Privileged setup ran as root; every writer that must observe a quota failure ran as the unprivileged user `decoyf25`, because the kernel exempts root from quota enforcement (accounting still charges root, but EDQUOT never fires for it). This root-exemption is itself a design constraint recorded in §6.

| Probe | Plan acceptance | Result | Primary evidence |
|---|---|---|---|
| ext4 image with `encrypt,project,quota` | A13 | PASS. Features present: `... encrypt ... quota ... project` | `01_mkfs.log`, `00_environment.log` |
| Mount with project quota enforced | A02 | PASS. `/dev/loop3 /mnt/f25 ext4 rw,relatime,prjquota`; `project quota ... is on (enforced)` | `02_mount.log` |
| Arena tagged with project id + inheritance, owned by non-root | A01/A02 | PASS. `chattr +P -p 777`, inherit flag set, `du` and `repquota` agree | `03_project.log`, `04_charge.log` |
| Native `write(2)` charged to the project quota | A02 | PASS. 64 MiB written by a C binary; `repquota` project 777 used 65540 KB; strace shows `write(3, ...) = 1048576` into the arena path | `04_charge.log`, `04_strace_nwrite.log` |
| Byte hard quota blocks on exceed | A02 | PASS. `write(3, ...) = -1 EDQUOT (Disk quota exceeded)`; usage pinned at the 81920 KB hard cap; `WROTE_OK` absent | `05_edquot_bytes.log`, `05_strace_edquot.log` |
| Inode hard quota blocks on exceed | A02 (inode case) | PASS. 10-inode cap; `manyfiles` returned EDQUOT (errno 122) short of 50 files | `06_edquot_inode.log` |
| DuckDB (library-managed) native spill charged + path proven | A02/A05 | PASS. strace shows DuckDB's own `duckdb_temp_block-*.block` and `duckdb_temp_storage-0.tmp` opened and written under the project-tagged `duckspill/` | `07_duckdb_charge.log`, `07_strace_duckdb.log` |
| DuckDB spill under a tight project quota fails closed | A02/A13 | PASS. Under a 50 MiB project cap DuckDB read the quota-bounded free space via statvfs and refused at `44.7 MiB/44.9 MiB used` with a clear error, exit 1. The raw `EDQUOT` backstop (proven above) covers the case where a library does not self-limit. | `08_duckdb_edquot.log` |
| fscrypt v2 encryption policy on arena | A05 | PASS. Policy v2, AES-256-XTS contents, AES-256-CTS filenames, PAD_32 | `09_fscrypt.log` |
| Encrypted file unreadable without the key | A05 | PASS. After `remove_key`: filenames are ciphertext, `cat` of the plaintext name returns "No such file or directory" | `09_fscrypt.log` |
| Offline ciphertext inspection finds no plaintext | A05 | PASS. One plaintext sentinel was written inside the encrypted dir and one in a non-encrypted control file. After unmount, a raw-device scan finds exactly 1 hit, which is the control. The encrypted copy is not matchable in the raw bytes. | `10_offline.log` |
| Key re-add restores plaintext | A05/A11 | PASS. Re-adding the master key round-trips the sentinel | `11_roundtrip.log` |
| Same-filesystem publication rename preserves policy | A10 | PASS. Stage and final carry the identical v2 policy; `rename(2)` stage to final succeeds and the file stays readable | `12_publication.log` |
| Cross-device rename rejected | A10 | PASS. `rename(2)` arena to `/tmp` returns `errno=18 (Invalid cross-device link)`, so no cross-device copy can masquerade as an atomic publish | `12_publication.log` |
| Non-qualifying filesystems fail closed | A13 | PASS. Image without the `project` feature rejects `setquota -P` (rc 1) and `chattr +P` ("Operation not supported"); image without `encrypt` rejects `add_key`/`set_policy` (rc 1) | `13_failclosed.log` |
| Baseline environment and one measured sample | A14 (baseline only) | RECORDED. Setup (mkfs+mount+quotaon+policy) ~0.055 s; 128 MiB native write ~0.89 s on loopback. This is the environment record, not the full Slice-4 benchmark. | `14_baseline.log` |

The combined picture for the F25 guarantee: a write reaches the kernel through whatever library opens the file, and the project quota charges and caps it regardless of the library or the writing user. DuckDB's native spill files are the concrete library case and they land in the project-tagged directory and respect the cap by two independent mechanisms (statvfs-reported free space and raw EDQUOT).

## 3. Pinned versions (qualification evidence)

From `00_environment.log` and the host install step. These are the versions the qualification holds for. Slice 4 must re-run the encryption and spill acceptance against the engine's own pinned DuckDB, not only the CLI used here.

- Kernel: `7.0.0-1014-gcp`, Ubuntu 24.04 LTS.
- Kernel config: `CONFIG_FS_ENCRYPTION=y`, `CONFIG_FS_ENCRYPTION_INLINE_CRYPT=y`, `CONFIG_QUOTA=y`, `CONFIG_QUOTACTL=y`, `CONFIG_QFMT_V2=m` (loaded), `CONFIG_QUOTA_TREE=m`.
- CPU crypto: AES-NI present (`aes` flag).
- `fscryptctl` v1.3.0.
- `e2fsprogs` / `mke2fs` 1.47.0 (5-Feb-2023).
- quota utilities 4.06 (`setquota`, `quotaon`, `repquota`).
- DuckDB CLI 1.1.3; Python `duckdb` 2.0.0; `pyarrow` 26.0.0 (the env log line for the Python versions failed only because it resolved `$HOME` to `/root` under sudo; the versions were captured directly at install time).
- ext4 filesystem features on the qualifying image: `has_journal ext_attr resize_inode dir_index filetype extent 64bit flex_bg encrypt sparse_super large_file huge_file dir_nlink extra_isize quota metadata_csum project`.
- fscrypt policy observed: v2, contents AES-256-XTS, filenames AES-256-CTS, PAD_32, default data-unit size.

Engine dependency pins at HEAD (`pyproject.toml`): `pyarrow>=14.0.0` (`:76`), `duckdb>=0.10.0` (`:88`), `cryptography>=42` (`:157`). F25 reuses the already-present `cryptography` stack for key envelopes (plan §1.6) and adds no new crypto dependency.

## 4. Inventory reconciliation against HEAD

The plan's §1.3 T01-T17 inventory and the Slice-0 anchors were opened at HEAD `33ef629b`. The named seams exist; the Slice-0 relevant ones:

- `src/decoy_engine/execution/out_of_core/_duckdb.py`: `connect_duckdb(temp_dir, memory_limit)` at `:27`, `temp_dir.mkdir(...)` at `:37`, `os.chmod(temp_dir, 0o700)` at `:38`, `config["temp_directory"]` at `:41`, `memory_limit` at `:55`. `max_temp_directory_size` is not set, confirming the plan's note that it is a future second backstop, not current behavior.
- `src/decoy_engine/data_discovery.py`: `duckdb.connect(":memory:")` at `:179` with no `temp_directory` configured, confirming the unbounded-spill risk the plan flags as T17.
- `src/decoy_engine/connectors/postgres_snapshot.py`: `temp_directory` spill config at `:224`, `.stage-{run_id}` at `:447`, `snapshot-{run_id}` at `:448`, `spill-{run_id}` at `:449` (T13).
- `src/decoy_engine/execution/_transactional_sink.py`: `os.replace`/rename(2) publication semantics documented at `:10`-`:27`, `_decoy_stage_` `tempfile.mkdtemp` at `:266` (T09).
- `src/decoy_engine/execution/out_of_core/_budget.py`: the budget walk skips evicted files at `:510`-`:511`, confirming it is a point-in-time diagnostic, not a hard write-time quota.
- `src/decoy_engine/scratch/` does not exist, which is correct: the ScratchManager package is a Slice 1 deliverable.

No fabricated engine anchor was found. The plan's Slice-0 anchor claims hold at HEAD.

## 5. What remains asserted (not proven here)

- The platform host-lease authority is verified as real source (§anchors in `ledger-subordination-contract.md`) but is flag-gated off and unwired; its runtime behavior under an enabled flag is not exercised by this gate.
- F13, F23/F24, F26, F45 carrier-feature contracts are read at the level of the ROADMAP status lines and the named modules. Their exact runtime interfaces are to be resolved at each consuming slice, per the plan's instruction not to invent prerequisite implementations.
- The benchmark overhead (plan A14 full protocol, Slice 4) is not measured here. Only the baseline environment and a single setup/throughput sample are recorded.
- The prototype used loop-mounted ext4 images, not a dedicated block device or managed pool. The mechanics qualify; the setup-latency and throughput samples are loopback figures and are not representative of production storage.
- The qualification is against the designated GCP baseline. Customer self-hosted deployments must provision an equivalent ext4 + fscrypt v2 + project-quota pool or receive `scratch_backend_unavailable`. The CLI reduced `encrypted-volume` profile (handoff amendment) is out of Slice-0 scope and is not qualified here.

## 6. Conditions carried into the build

1. Writers must never run as root in production for the quota guarantee to hold. Root is exempt from quota enforcement (demonstrated: a root writer blew past both the byte and inode caps while a non-root writer hit EDQUOT at the exact cap). The worker lifecycle (see `lifecycle-and-spool-contracts.md`) must run engine execution under an unprivileged principal, and the deployment adapter does the privileged mkfs/mount/quotaon/setquota/policy setup.
2. `max_temp_directory_size` on DuckDB connections is a second backstop to add only after testing the engine's pinned DuckDB version (plan §1.5 item 4). The statvfs-bounded self-limit observed here is favorable but version-dependent.
3. Slice 4 must re-run A05/A14 against the engine's actually pinned `duckdb` and `pyarrow`, not the CLI used in this gate.
4. The engine ledger binds under the platform authority per `ledger-subordination-contract.md`; the engine must not make independent lease or deletion decisions.
5. No release flips the default write path onto a managed-but-unencrypted arena. Encryption is not claimed before Slice 4 (plan Slice 4 gate; handoff hard constraint).

## 7. Provenance

Prototype run 2026-10-29 by the F25 Slice-0 builder on the designated GCP host. Platform anchors verified first-hand against `/home/cam/vscode/decoy-platform` at the local checkout. Engine anchors verified first-hand at worktree HEAD `33ef629b`. No engine production code was modified. No plan branch was touched. No merge or push performed.

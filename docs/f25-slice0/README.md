Status: record

# F25 Slice 0 deliverables

The hard gate for F25 (protected scratch, quotas, orphan recovery). Plan: `docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md` §3 Slice 0. Nothing from Slices 1-6 is implemented here.

- `dependency-verification-report.md` - the hard gate. What was proven, pinned versions, inventory reconciliation, what remains asserted, and the go/no-go (GO with conditions).
- `ledger-subordination-contract.md` - the engine local SQLite ledger subordinated to the platform durable ownership authority, grounded in platform source read first-hand.
- `lifecycle-and-spool-contracts.md` - worker/CLI lifecycle hooks (provision, secret injection, lease maintenance, sweep) and the cloud-to-local spool contract.
- `evidence/20261029T161416Z/` - raw prototype evidence from the GCP qualifying host: per-probe logs, strace traces, mkfs/mount/repquota output, the archived prototype script (`_prototype_script.sh`), `RESULTS.txt`, and `MANIFEST.txt`.

Qualifying host: GCP `34.74.130.221`, Ubuntu 24.04, kernel `7.0.0-1014-gcp`. Synthetic sentinels only; no real data. The raw bundle also lives on the host at `/root/f25/evidence/20261029T161416Z/`.

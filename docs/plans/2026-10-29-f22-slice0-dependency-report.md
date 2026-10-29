Status: report

Summary: Slice 0 dependency-verification report for F22 (atomic and idempotent publication): every "asserted, verify before build" item, classified and located.

Baseline: plan baseline `2a44723f`; verified against engine `main` at `1b3a922` (two later commits, neither touches publication). Plan: [2026-10-29-f22-atomic-idempotent-publication.md](2026-10-29-f22-atomic-idempotent-publication.md).

# F22 Slice 0: dependency-verification report

## Verdict

**GO for the engine core (Slices 1-5).** Nothing REQUIRED is absent. The F33 shared codec is OPTIONAL reuse and its absence is not a blocker; F22 ships its own canonical codec.

## REQUIRED and non-substitutable (engine core)

| Item | Location (verified on `main`) | Status |
|---|---|---|
| Pre-publication evidence carrier: `ExecutionResult` (outputs, timings, `quality_metrics`) | `src/decoy_engine/execution/_adapter.py:42` | present, no receipt field yet (Slice 1 adds one) |
| Failed/inconclusive check carrier: `QualitySummary` | `src/decoy_engine/validation/post/_types.py:61` | present |
| Failed-check report preserved as evidence, never as approval | `tests/unit/validation/test_failed_evidence_manifest.py` | present |
| Commit site 1: `ParquetTransactionalSink.commit` | `src/decoy_engine/execution/_transactional_sink.py` | present |
| Commit site 2: `atomic_commit` | `src/decoy_engine/execution/_isolated_commit.py` | present, called from `_isolated_run.py` (in-process fallback and isolated path) |
| Out-of-core cancellation gap (`except Exception` vs sequential `BaseException`) | `src/decoy_engine/execution/out_of_core/_runner.py` | present, closed in Slice 2 |

## OPTIONAL reuse (absence degrades, never blocks)

| Item | Finding |
|---|---|
| F33 shared canonical codec / bundle schema | Absent in this checkout (no `publication/` or evidence-bundle package; `grep -riE "F33|evidence.bundle|offline.verif" src` finds nothing; the only "bundle" file is the unrelated `generation/composite/_bundle_pool.py`). F22 defines its own strict codec in `decoy_engine.publication`. If F33 later lands a shared codec, swap is a pure re-export because the descriptor bytes are the contract, not the helper. |

## Needed only before enabling their own surface (not engine core)

| Item | Finding | Gate |
|---|---|---|
| Engine DB output sink | **Absent.** No DB output sink exists in the engine (`connectors/` holds `S3FileSink`, `GCSFileSink`, `SFTPFileSink`; `postgres_snapshot.py` is a read-only source snapshot). The MASTER audit claim is not backed by engine code. | Owning repo must evidence it; F22 ships only the typed adapter boundary + conformance suite. |
| CLI consumers (job/operation identity persistence, receipt display) | Outside this checkout; not verified here. | CLI grounding before enabling. |
| Platform partial-publication evidence | Outside this checkout; not verified here. | Platform grounding before enabling. |
| F23 recovery/ownership, F25 protected scratch, F26 retention, F45 authorization | No engine API exists. F22 uses local safe defaults (process-scoped advisory lock, 0700/0600 modes) and refuses (never silently weakens) when a required protection is unavailable. | Each seam before its own surface. |

## Environment qualifications recorded at build time

- Python 3.11.17 venv from `uv sync --frozen --all-extras`; pyarrow 24.0.0 (the pinned version for the byte-determinism claim in A2).
- Baseline suite state before any change: focused publication suites + `tests/sentry` = 3017 passed, 5 skipped, 1 failed. The one failure, `tests/sentry/test_proof_manifest.py::test_proof_manifest_is_up_to_date`, **fails on unmodified `main`** and is unrelated to F22.
- Local qualification profile: POSIX local filesystem with `fcntl.flock`. NFS/SMB/FUSE are not presumed equivalent.

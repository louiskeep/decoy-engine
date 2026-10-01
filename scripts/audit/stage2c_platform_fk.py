#!/usr/bin/env python3
"""Stage 2c, scope A (platform path) + scope B (cycle-bug witness).

Runs an FK shape through the platform's real worker entry point,
`api.jobs.v2_orchestrator.run_v2_pipeline_job(job, db, config)` -- the same
function Celery calls -- against a real throwaway Postgres database
(DATABASE_URL env var, set by the driver before this process starts so
`api.database`'s module-level engine binds to it). Also calls the admission
pricing functions in `api/jobs/admission_fk.py` directly (pure, config-only,
no DB) for the same config.

One shape per fresh subprocess (DATABASE_URL points at a persistent Postgres
database that already has the platform's alembic schema at head; each
process opens its own session and creates its own Job row).
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECORD = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "environment.json"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108

sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
import stage2c_fk_shapes as fkshapes  # noqa: E402


def _vmhwm_kb() -> int | None:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _env_record() -> dict[str, Any]:
    if not ENV_RECORD.exists():
        return {}
    return json.loads(ENV_RECORD.read_text())


def _admission_fk_summary(config: dict) -> dict:
    from api.jobs import admission_fk

    bounded, bounded_reason = admission_fk.is_fk_bounded_route_candidate(config)
    ooc_eligible, ooc_reason = admission_fk.out_of_core_admission_eligible(config)
    return {
        "is_fk_bounded_route_candidate": {"result": bounded, "reason": bounded_reason},
        "out_of_core_admission_eligible": {"result": ooc_eligible, "reason": ooc_reason},
    }


def _ensure_user(db) -> int:
    from api.models import User

    existing = db.query(User).filter(User.email == "audit2c@x.local").one_or_none()
    if existing:
        return existing.id
    u = User(email="audit2c@x.local", hashed_password="x", is_active=True)  # noqa: S106 - fake fixture credential
    db.add(u)
    db.commit()
    db.refresh(u)
    return u.id


def run_cell(spec: dict) -> dict:
    import os

    os.environ.setdefault("DECOY_MASTER_KEY", base64.b64encode(b"\x24" * 32).decode())

    import api.models  # noqa: F401 -- register every ORM table before first query
    from api.database import SessionLocal
    from api.jobs.v2_runner import run_v2_pipeline_job  # re-exported from v2_orchestrator
    from api.models import Job, JobStatus

    shape = spec["shape"]
    sizes = spec.get("sizes", [200, 1000])
    force_ooc_threshold = spec.get("force_ooc_threshold_rows")
    builder = fkshapes._SHAPES[shape]
    config_raw, _sources = builder(sizes)

    patched_constants = None
    if force_ooc_threshold is not None:
        # Route-equivalent large-tier: override BOTH the admission-pricing
        # threshold (admission_fk.py) and the runtime-dispatch threshold
        # (v2_out_of_core.py) to the SAME value, together, so a tiny fixture
        # crosses both -- the plan's explicit "assert admission and runtime
        # agree" check. Both read decoy_engine.execution.OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT
        # by default; patching the derived module constants directly (not the
        # engine default) is what each call site actually reads at runtime.
        import api.jobs.admission_fk as admission_fk_mod
        import api.jobs.v2_out_of_core as v2_ooc_mod

        patched_constants = {
            "admission_fk.OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS": (
                admission_fk_mod.OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS
            ),
            "v2_out_of_core._OUT_OF_CORE_THRESHOLD_ROWS": (v2_ooc_mod._OUT_OF_CORE_THRESHOLD_ROWS),
        }
        admission_fk_mod.OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS = force_ooc_threshold
        v2_ooc_mod._OUT_OF_CORE_THRESHOLD_ROWS = force_ooc_threshold

    admission_summary = _admission_fk_summary(config_raw)

    from decoy_engine.config import PipelineConfig

    config = PipelineConfig.model_validate(config_raw).model_dump()

    db = SessionLocal()
    status = "ok"
    error = None
    job_id = None
    job_status_after = None
    job_row_count = None
    node_runs = []
    t0 = time.time()
    try:
        owner_id = _ensure_user(db)
        job = Job(owner_id=owner_id, mode="mask", status=JobStatus.pending)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        try:
            run_v2_pipeline_job(job, db, config)
        except Exception as exc:  # the platform path itself may raise
            status = "raised"
            error = f"{type(exc).__name__}: {exc}"
        db.refresh(job)
        job_status_after = job.status.value if hasattr(job.status, "value") else str(job.status)
        job_row_count = job.row_count
        try:
            from api.models import JobNodeRun

            rows = db.query(JobNodeRun).filter(JobNodeRun.job_id == job_id).all()
            node_runs = [
                {
                    "node_id": r.node_id,
                    "kind": r.kind,
                    "status": r.status,
                    "row_count": r.row_count,
                    "exports": r.exports,
                }
                for r in rows
            ]
        except Exception:
            node_runs = []
    finally:
        db.close()
    wall = time.time() - t0

    env_record = _env_record()
    record: dict[str, Any] = {
        "cell_id": spec["id"],
        "kind": "stage2c_platform_fk",
        "description": spec.get("description"),
        "shape": shape,
        "params": {
            "sizes": sizes,
            "entry_point": (
                "platform worker (api.jobs.v2_orchestrator.run_v2_pipeline_job), "
                "real Postgres-backed Job row, throwaway DB decoy_audit_20260930"
            ),
        },
        "admission_fk": admission_summary,
        "force_ooc_threshold_rows": force_ooc_threshold,
        "patched_constants_before_override": patched_constants,
        "outcome": {
            "harness_status": status,
            "harness_error": error,
            "job_id": job_id,
            "job_status_after": job_status_after,
            "job_row_count": job_row_count,
            "wall_seconds": round(wall, 4),
        },
        "job_node_runs": node_runs,
        "peak_memory_vmhwm_kb": _vmhwm_kb(),
        "commits": {
            "engine_commit": env_record.get("engine", {}).get("commit"),
            "platform_commit": env_record.get("platform", {}).get("commit"),
        },
    }
    return record


def main() -> None:
    spec = json.loads(sys.argv[1]) if len(sys.argv) > 1 else json.loads(sys.stdin.read())
    record = run_cell(spec)
    print(json.dumps(record, default=str))


if __name__ == "__main__":
    main()

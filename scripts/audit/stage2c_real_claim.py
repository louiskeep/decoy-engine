#!/usr/bin/env python3
"""Stage 2c, scope C: a real Postgres-backed claim for entry point 3.

Creates a Job row in the throwaway Postgres database (DATABASE_URL env var),
lowers `settings.streaming_min_input_mb` so a small single-table job
qualifies, runs the REAL claim function (`queue_worker._claim_next_job`,
flag off -- the legacy scan; the adaptive-scheduler flag-on path needs the
cgroup supervisor this devbox lacks, recorded separately), reads the
persisted `phase1_streaming_tables` plan back from the row, then runs the
real worker path (`run_v2_pipeline_job`) on that same persisted job and
records the per-chunk engine function and backend from the resulting
`JobNodeRun` exports.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECORD = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "environment.json"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108


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


def _write_csv_source(n: int) -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / "real_claim_source.csv"
    lines = ["id,email,amount"] + [f"{i},user{i}@ex.com,{i}" for i in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def main() -> None:
    import os

    os.environ.setdefault("DECOY_MASTER_KEY", base64.b64encode(b"\x11" * 32).decode())

    import api.models  # noqa: F401 -- register every ORM table
    from api.config import settings
    from api.database import SessionLocal
    from api.jobs import queue_worker
    from api.jobs._phase1_eligibility import decode_streaming_plan
    from api.jobs.v2_runner import run_v2_pipeline_job
    from api.models import Job, JobNodeRun, JobStatus, User

    n_rows = 2000
    src_path = _write_csv_source(n_rows)
    out_path = str(SCRATCH / "real_claim_out.csv")

    yaml_snapshot = (
        "version: 1\n"
        "global_settings:\n"
        "  seed: 20260930\n"
        "sources:\n"
        "  people:\n"
        "    type: file\n"
        "    format: csv\n"
        f"    path: {src_path}\n"
        "targets:\n"
        "  people:\n"
        "    type: file\n"
        "    format: csv\n"
        f"    path: {out_path}\n"
        "tables:\n"
        "  - name: people\n"
        "    columns:\n"
        "      - name: id\n"
        "        strategy: passthrough\n"
        "      - name: email\n"
        "        strategy: hash\n"
        "        namespace: ns_claim\n"
        "      - name: amount\n"
        "        strategy: redact\n"
        "        provider_config:\n"
        "          char: '*'\n"
        "relationships: []\n"
    )

    # Lower the streaming size floor so this small (real, ~<1MB) fixture
    # qualifies -- the plan's explicit instruction ("streaming_min_input_mb
    # lowered so a small single-table job gets phase1_streaming_tables
    # stamped").
    settings.streaming_execution_enabled = True
    settings.streaming_min_input_mb = 0.0

    db = SessionLocal()
    result: dict[str, Any] = {}
    try:
        user = db.query(User).filter(User.email == "audit2c-claim@x.local").one_or_none()
        if user is None:
            user = User(email="audit2c-claim@x.local", hashed_password="x", is_active=True)  # noqa: S106 - fake fixture credential
            db.add(user)
            db.commit()
            db.refresh(user)

        job = Job(
            owner_id=user.id,
            mode="mask",
            status=JobStatus.pending,
            yaml_snapshot=yaml_snapshot,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

        t0 = time.time()
        claimed = queue_worker._claim_next_job()
        claim_wall = time.time() - t0

        db.refresh(job)
        plan = decode_streaming_plan(job.phase1_streaming_tables)

        result["claim"] = {
            "claimed_tuple": list(claimed) if claimed else None,
            "job_id_matches_claim": bool(claimed) and claimed[0] == job_id,
            "job_status_after_claim": job.status.value
            if hasattr(job.status, "value")
            else str(job.status),
            "phase1_streaming_tables_raw": job.phase1_streaming_tables,
            "decoded_plan_tables": list(plan.tables) if plan else None,
            "wall_seconds": round(claim_wall, 4),
        }

        # Now run the real worker path on this SAME persisted job (the claim
        # already flipped it to running and stamped the plan; the worker
        # reads that persisted state, not the knobs set here).
        import yaml as _yaml

        config = _yaml.safe_load(job.yaml_snapshot)

        t1 = time.time()
        worker_error = None
        try:
            run_v2_pipeline_job(job, db, config)
        except Exception as exc:  # pragma: no cover - record, don't hide
            worker_error = f"{type(exc).__name__}: {exc}"
        worker_wall = time.time() - t1

        db.refresh(job)
        node_rows = db.query(JobNodeRun).filter(JobNodeRun.job_id == job_id).all()
        result["worker_run"] = {
            "error": worker_error,
            "job_status_after_worker": job.status.value
            if hasattr(job.status, "value")
            else str(job.status),
            "job_row_count": job.row_count,
            "wall_seconds": round(worker_wall, 4),
            "node_runs": [
                {
                    "node_id": r.node_id,
                    "kind": r.kind,
                    "status": r.status,
                    "row_count": r.row_count,
                    "exports": r.exports,
                }
                for r in node_rows
            ],
        }
        result["job_id"] = job_id
    finally:
        db.close()

    env_record = _env_record()
    record = {
        "cell_id": "R068_real_postgres_claim_and_worker",
        "kind": "stage2c_real_claim",
        "description": (
            "Real Postgres-backed claim (queue_worker._claim_next_job, flag off) "
            "with streaming_min_input_mb lowered so a 2000-row CSV job gets "
            "phase1_streaming_tables stamped, then the real worker path "
            "(run_v2_pipeline_job) on the same persisted job."
        ),
        "params": {
            "rows": n_rows,
            "entry_point": "queue_worker._claim_next_job (flag-off legacy scan) then run_v2_pipeline_job",
            "adaptive_scheduler_lease_authority_enabled": False,
        },
        **result,
        "peak_memory_vmhwm_kb": _vmhwm_kb(),
        "commits": {
            "engine_commit": env_record.get("engine", {}).get("commit"),
            "platform_commit": env_record.get("platform", {}).get("commit"),
        },
    }
    print(json.dumps(record, default=str))


if __name__ == "__main__":
    main()

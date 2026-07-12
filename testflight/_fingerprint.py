"""Cross-process determinism fingerprint (TH-2.2 / P1-5a).

The in-process determinism check (`check_determinism` in `_determinism.py`)
calls `run_pipeline` twice inside the SAME Python process
(`_runner.run_pipeline_twice`), so both calls share one PYTHONHASHSEED. Any
nondeterminism that depends on dict/set iteration order under hash
randomization -- the classic silent Python nondeterminism bug class -- is
invisible to that check: both calls see the identical seed and therefore
agree with each other even if a DIFFERENT process (a different hash seed)
would produce different output. That gap only resurfaces later, across CI
runs, when someone happens to notice a nightly diff.

This module gives that gap a mechanical, always-on check: a stable SHA-256
fingerprint of each job's masked output tables (schema + data), compared
against a committed golden fingerprint per job
(`testflight/golden_fingerprints.json`). A mismatch means the SAME seeded
pipeline config produced DIFFERENT output in a DIFFERENT process -- exactly
the cross-process nondeterminism class the in-process double-run cannot see.

Updating the golden file is a deliberate, reviewable action
(`python scripts/test_flight.py --update-fingerprints`), never an automatic
overwrite -- a silent auto-update would defeat the point of a golden check.
A first-time-missing golden file is reported as a NOTE, not a failure, so
adding a new job does not require touching this file by hand before its
first green run; running --update-fingerprints once records it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa

GOLDEN_PATH = Path(__file__).parent / "golden_fingerprints.json"


def fingerprint_outputs(outputs: dict[str, pa.Table]) -> str:
    """Return a stable SHA-256 hex digest of a job's output tables.

    Canonicalizes each table's arrow schema (str) and data (to_pydict()) into
    one JSON document, sorted by table name and by json.dumps(sort_keys=True),
    so the digest depends only on CONTENT -- not on this process's dict/table
    iteration order -- while still catching any content or schema difference
    across processes (a hash-seed-dependent ordering bug would change the
    canonicalized JSON and therefore the digest).

    Args:
        outputs: dict[table_name, pa.Table], e.g. ExecutionResult.outputs.

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    parts: dict[str, Any] = {}
    for name in sorted(outputs):
        tbl = outputs[name]
        parts[name] = {"schema": str(tbl.schema), "data": tbl.to_pydict()}
    canonical = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_golden() -> dict[str, str]:
    """Load the committed golden fingerprint map (job_name -> sha256 hex).

    Returns an empty dict (not an error) when the file does not exist yet --
    callers report that as a bootstrap NOTE, not a suite failure.
    """
    if not GOLDEN_PATH.exists():
        return {}
    loaded: dict[str, str] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return loaded


def write_golden(fingerprints: dict[str, str]) -> None:
    """Write the golden fingerprint map, sorted for a stable, reviewable diff."""
    GOLDEN_PATH.write_text(
        json.dumps(dict(sorted(fingerprints.items())), indent=2) + "\n",
        encoding="utf-8",
    )


def check_fingerprints(current: dict[str, str], golden: dict[str, str]) -> list[str]:
    """Return human-readable problem messages; empty list means all match.

    A job present in `current` but absent from `golden` is reported (a new
    job needs its fingerprint recorded via --update-fingerprints), not
    silently accepted -- a missing golden entry must not read as a pass.

    Args:
        current: job_name -> fingerprint computed by THIS run.
        golden: job_name -> fingerprint committed by a PRIOR (necessarily
            different) process invocation.

    Returns:
        List of mismatch/missing-entry messages, one per problem job.
    """
    problems: list[str] = []
    for job_name, fp in sorted(current.items()):
        golden_fp = golden.get(job_name)
        if golden_fp is None:
            problems.append(
                f"{job_name}: no golden fingerprint on record. Run "
                f"'python scripts/test_flight.py --update-fingerprints' to record one "
                f"deliberately, after confirming this run's output is correct."
            )
        elif golden_fp != fp:
            problems.append(
                f"{job_name}: cross-process fingerprint drift. golden={golden_fp} "
                f"current={fp}. The SAME seeded config produced DIFFERENT output in "
                f"a DIFFERENT process -- e.g. a PYTHONHASHSEED-dependent dict/set "
                f"iteration-order bug that the in-process double-run cannot see. If "
                f"this drift is an intentional fixture/engine change, re-run with "
                f"--update-fingerprints after confirming the new output is correct."
            )
    return problems

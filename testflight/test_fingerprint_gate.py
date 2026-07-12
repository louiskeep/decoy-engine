"""Cross-process determinism fingerprint gate, as a pytest (TH-3.2 MEDIUM).

Gate-topology fix. The cross-process fingerprint check historically lived ONLY
in ``scripts/test_flight.py`` (the manual runner). ``pytest testflight`` -- which
CI runs -- exercised each job in-process via ``run_job`` but never compared a
job's fingerprint computed in a DIFFERENT process against the committed golden.
That is exactly how the TH-3.2 false PASS slipped: pytest was green while the
manual script exited 1, and nobody re-ran the script.

These tests close the gap: they compute a job's fingerprint in a genuinely fresh
subprocess (with a distinct ``PYTHONHASHSEED``) and run the SAME
``check_fingerprints`` logic the script uses against ``golden_fingerprints.json``.
A cross-process determinism regression -- e.g. re-coupling the derived seed to a
per-run temp path, or a hash-seed-dependent iteration-order bug -- now fails
pytest/CI, not just the manual script.

Marked ``testflight`` so the default regression loop never collects them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from testflight._fingerprint import check_fingerprints, load_golden

pytestmark = pytest.mark.testflight

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Worker snippet: run one job by name in THIS (child) process and print its
# fingerprint on a marker line so the parent can parse it unambiguously.
_WORKER = """
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root)); sys.path.insert(0, str(root / "src"))
from testflight._runner import discover_jobs, run_job
name = sys.argv[2]
m = [m for m in discover_jobs() if m.parent.name == name][0]
r = run_job(m)
assert r.passed, f"job {name} did not pass in the subprocess"
print("FINGERPRINT_MARKER", r.job_name, r.fingerprint)
"""


def _fingerprint_in_subprocess(job_name: str) -> str:
    """Compute ``job_name``'s fingerprint in a fresh process with a distinct
    PYTHONHASHSEED, so the value is genuinely cross-process w.r.t. the parent.
    """
    env = dict(os.environ)
    # Force a hash seed that differs from any the parent might use, so a
    # PYTHONHASHSEED-dependent ordering bug would surface as a mismatch.
    env["PYTHONHASHSEED"] = "12345"
    # S603 false positive: the command is sys.executable running our own inline
    # worker with fixed, non-user args (repo root + a job name from disk).
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _WORKER, str(_REPO_ROOT), job_name],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"subprocess fingerprint worker failed for {job_name!r} "
        f"(rc={proc.returncode}):\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "FINGERPRINT_MARKER" and parts[1] == job_name:
            return parts[2]
    raise AssertionError(
        f"no FINGERPRINT_MARKER for {job_name!r} in subprocess output:\n{proc.stdout}"
    )


def test_job_d_fingerprint_matches_golden_cross_process() -> None:
    """Job D's statistical columns are reproducible across processes.

    Regression guard for the TH-3.2 determinism bug: a snapshot_file PATH used
    to leak into the per-column seed, so the SAME config produced DIFFERENT
    output every process (fresh temp dir -> fresh path -> fresh seed). The fix
    fingerprints snapshot_file by CONTENT, so this must now match the golden
    from ANY process.
    """
    golden = load_golden()
    assert golden, (
        "golden_fingerprints.json is empty/missing; record it with "
        "'python scripts/test_flight.py --update-fingerprints' before this gate can bind."
    )
    job = "d_longitudinal_visits"
    assert job in golden, f"{job} has no committed golden fingerprint."
    current = {job: _fingerprint_in_subprocess(job)}
    problems = check_fingerprints(current, golden)
    assert not problems, "cross-process fingerprint drift:\n" + "\n".join(problems)


def test_two_fresh_processes_agree_on_job_d() -> None:
    """Two independent fresh processes produce the SAME Job D fingerprint.

    Directly asserts cross-process reproducibility without reference to the
    committed golden, so it catches a determinism regression even if the golden
    were (wrongly) re-baselined to a non-reproducible value.
    """
    a = _fingerprint_in_subprocess("d_longitudinal_visits")
    b = _fingerprint_in_subprocess("d_longitudinal_visits")
    assert a == b, f"two fresh processes disagree on Job D fingerprint: {a} != {b}"

"""Frozen golden regression test for the regex-detector baseline report (§A.7).

Asserts: same corpus + same seed -> identical report bytes. Any change to the
detector set, fixture data, or metric computation that shifts the output fails
CI with the old and new digests, rather than silently re-baselining.

Gate reference: ml-benchmarking-and-privacy.md §A.7:
    "MUST make evaluation deterministic (seed-pinned) and commit a frozen
    golden baseline of the metrics, so any regression fails CI. Same corpus
    + same seed -> identical report bytes."

Update procedure (intentional detector change):
    1. Run: UPDATE_SNAPSHOTS=1 pytest tests/snapshots/test_ml_baseline_golden.py
    2. Inspect docs/v2/ml/baseline-report.json (human-readable) and confirm
       the numbers moved for the right reason.
    3. Commit both baseline-report.json and baseline.sha256 with an explanation
       of why the baseline legitimately changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from decoy_engine.storm.eval import run_baseline

_GOLDEN_SHA256 = (
    Path(__file__).parent / "golden" / "ml_baseline" / "baseline.sha256"
)
_REPORT_PATH = Path(__file__).parents[2] / "docs" / "v2" / "ml" / "baseline-report.json"


def _canonical_blob(report_dict: dict) -> bytes:
    """Canonical JSON bytes: sort_keys=True, indent=2, None -> 'null'."""
    return json.dumps(report_dict, sort_keys=True, indent=2, default=str).encode("utf-8")


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def test_baseline_report_is_deterministic():
    """Two back-to-back runs produce byte-identical canonical JSON."""
    rep_a = run_baseline()
    rep_b = run_baseline()
    assert _digest(_canonical_blob(rep_a.to_dict())) == _digest(
        _canonical_blob(rep_b.to_dict())
    ), "Baseline report is not deterministic across two runs"


def test_baseline_report_matches_golden():
    """Current run matches the committed golden SHA-256 (regression gate)."""
    rep = run_baseline()
    blob = _canonical_blob(rep.to_dict())
    actual_digest = _digest(blob)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        _GOLDEN_SHA256.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN_SHA256.write_text(actual_digest + "\n", encoding="utf-8")
        _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REPORT_PATH.write_bytes(blob)
        return

    if not _GOLDEN_SHA256.exists():
        raise AssertionError(
            f"Missing golden SHA-256 at {_GOLDEN_SHA256}. "
            "Run with UPDATE_SNAPSHOTS=1 to create it, then inspect "
            f"{_REPORT_PATH} before committing."
        )

    expected = _GOLDEN_SHA256.read_text(encoding="utf-8").strip()
    if actual_digest != expected:
        raise AssertionError(
            f"Baseline report regression detected.\n"
            f"  expected SHA-256: {expected}\n"
            f"  actual   SHA-256: {actual_digest}\n"
            f"  Run with UPDATE_SNAPSHOTS=1 only after confirming the metric "
            f"change is intentional (detector improvement or corpus change).\n"
            f"  Report preview:\n"
            + blob.decode("utf-8")[:2000]
        )

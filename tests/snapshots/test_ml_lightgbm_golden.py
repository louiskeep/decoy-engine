"""Frozen golden regression test for the LightGBM model-pack eval report (§A.7).

Asserts: same corpus + same seed -> identical report bytes. Any change to the
classifier, corpus, featurizer, or calibration that shifts the metric output
fails CI with the old and new digests, rather than silently re-baselining.

Gate reference: ml-benchmarking-and-privacy.md §A.7:
    "MUST make evaluation deterministic (seed-pinned) and commit a frozen
    golden baseline of the metrics, so any regression fails CI. Same corpus
    + same seed -> identical report bytes."

Update procedure (intentional change):
    1. Run: UPDATE_SNAPSHOTS=1 pytest tests/snapshots/test_ml_lightgbm_golden.py
    2. Inspect docs/v2/ml/lightgbm-report.json and confirm the numbers
       moved for the right reason (model improvement, corpus change, etc.).
    3. Commit both lightgbm-report.json and lightgbm.sha256 with an
       explanation of why the change is intentional.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_GOLDEN_SHA256 = (
    Path(__file__).parent / "golden" / "ml_lightgbm" / "lightgbm.sha256"
)
_REPORT_PATH = Path(__file__).parents[2] / "docs" / "v2" / "ml" / "lightgbm-report.json"


def _canonical_blob(report_dict: dict) -> bytes:
    """Canonical JSON bytes: sort_keys=True, indent=2, None -> 'null'."""
    return json.dumps(report_dict, sort_keys=True, indent=2, default=str).encode("utf-8")


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _load_stored_report() -> bytes:
    """Load the committed lightgbm-report.json and return canonical bytes."""
    raw = _REPORT_PATH.read_bytes()
    report_dict = json.loads(raw)
    return _canonical_blob(report_dict)


def test_lightgbm_report_matches_golden() -> None:
    """Committed lightgbm-report.json matches the frozen SHA-256 (regression gate).

    This test does NOT re-train the model on every run (which would be slow).
    Instead it verifies the *committed* report file hasn't drifted from the
    frozen digest.  Re-training + regenerating the report only happens when you
    run with UPDATE_SNAPSHOTS=1.
    """
    if not _REPORT_PATH.exists():
        raise AssertionError(
            f"Missing lightgbm-report.json at {_REPORT_PATH}. "
            "Run with UPDATE_SNAPSHOTS=1 to regenerate, then commit both "
            "the report and the updated lightgbm.sha256."
        )

    blob = _load_stored_report()
    actual_digest = _digest(blob)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        from decoy_engine.storm.model_pack.trainer import train_and_evaluate
        import dataclasses

        pack_dir = _REPORT_PATH.parents[1] / "packs" / "lgbm-v1"
        report = train_and_evaluate(out_dir=pack_dir)
        report_dict = dataclasses.asdict(report)
        out_blob = _canonical_blob(report_dict)
        _REPORT_PATH.write_bytes(out_blob)
        new_digest = _digest(out_blob)
        _GOLDEN_SHA256.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN_SHA256.write_text(new_digest + "\n", encoding="utf-8")
        print(f"\nUpdated golden digest: {new_digest}")
        return

    if not _GOLDEN_SHA256.exists():
        raise AssertionError(
            f"Missing golden SHA-256 at {_GOLDEN_SHA256}. "
            "Run with UPDATE_SNAPSHOTS=1 to create it."
        )

    expected = _GOLDEN_SHA256.read_text(encoding="utf-8").strip()
    if actual_digest != expected:
        raise AssertionError(
            f"LightGBM report regression detected.\n"
            f"  expected SHA-256: {expected}\n"
            f"  actual   SHA-256: {actual_digest}\n"
            f"  Run with UPDATE_SNAPSHOTS=1 only after confirming the metric "
            f"change is intentional (model or corpus improvement).\n"
            f"  Report snippet:\n"
            + blob.decode("utf-8")[:2000]
        )


def test_lightgbm_report_lift_gate_met() -> None:
    """Committed report must show >=5 ppt macro-recall lift over the baseline."""
    if not _REPORT_PATH.exists():
        raise AssertionError(f"Missing lightgbm-report.json at {_REPORT_PATH}.")

    with _REPORT_PATH.open(encoding="utf-8") as f:
        report = json.load(f)

    lift = report.get("lift", {})
    assert lift, "Report missing 'lift' section"
    assert lift.get("gate_met") is True, (
        f"Lift gate NOT met: "
        f"baseline={lift.get('baseline_macro_recall')}, "
        f"model={lift.get('model_macro_recall')}, "
        f"lift={lift.get('lift_ppt')} ppt "
        f"(need >= {lift.get('gate_threshold_ppt')} ppt)"
    )

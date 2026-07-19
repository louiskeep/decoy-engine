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
    3. Commit both lightgbm-report.json, lightgbm.sha256, and the pack
       manifest (docs/v2/ml/packs/lgbm-v1/manifest.json) with an
       explanation of why the change is intentional.

Tests in this file:

test_lightgbm_report_matches_golden
    File-level regression: committed JSON has not drifted from the frozen
    digest (fast; runs in CI without retraining).

test_lightgbm_report_lift_gate_met
    Committed report asserts lift gate is met (>= 5 ppt).

test_manifest_report_hash_matches_committed_report  [§B.7]
    manifest.eval_report_hash == SHA-256(canonical committed report).
    Verifies the provenance binding is verifiable without re-running training.

test_lightgbm_regression_retrain  [§A.7]
    RE-RUNS train_and_evaluate with seed=42 and asserts key metrics match
    the committed golden within tight tolerance.  This is the CI gate that
    detects trainer/featurizer/corpus changes that accidentally shift metrics.
    Runtime: ~8-15 s (acceptable for a model gate).

test_predict_column_band_accuracy  [§A.4]
    Runs predict_column on the held-out test set and asserts the
    band-to-accuracy relationship holds: review-band predictions are >= 70%
    accurate (the band's precision floor), high-band predictions (if any)
    are >= 95% accurate.  Reports PROVISIONAL counts (small held-out n; the
    exact fold size shifts with the pinned scikit-learn version).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

# ml-gate membership (pytest -m ml). The pure file-hash tests here
# (report/manifest/provenance/golden-SHA) need no ml extra and also run in the
# default regression gate; the retrain + band tests importorskip themselves
# when scikit-learn/lightgbm are absent.
pytestmark = pytest.mark.ml

_GOLDEN_SHA256 = Path(__file__).parent / "golden" / "ml_lightgbm" / "lightgbm.sha256"
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
        import dataclasses

        from decoy_engine.storm.model_pack.trainer import train_and_evaluate

        # _REPORT_PATH.parent == docs/v2/ml; packs/ lives alongside the report.
        pack_dir = _REPORT_PATH.parent / "packs" / "lgbm-v1"
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
            f"Missing golden SHA-256 at {_GOLDEN_SHA256}. Run with UPDATE_SNAPSHOTS=1 to create it."
        )

    expected = _GOLDEN_SHA256.read_text(encoding="utf-8").strip()
    if actual_digest != expected:
        raise AssertionError(
            f"LightGBM report regression detected.\n"
            f"  expected SHA-256: {expected}\n"
            f"  actual   SHA-256: {actual_digest}\n"
            f"  Run with UPDATE_SNAPSHOTS=1 only after confirming the metric "
            f"change is intentional (model or corpus improvement).\n"
            f"  Report snippet:\n" + blob.decode("utf-8")[:2000]
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


# ── §B.7 provenance hash binding ──────────────────────────────────────────────


def test_manifest_report_hash_matches_committed_report() -> None:
    """§B.7 provenance binding: manifest.eval_report_hash must equal the
    SHA-256 of the canonical committed lightgbm-report.json.

    This verifies the binding WITHOUT re-running training, so any commit that
    edits the report without updating the manifest is caught in CI immediately.

    The canonical form is: json.dumps(report_dict, sort_keys=True, indent=2,
    default=str) -- the same form trainer.py uses when computing the hash.
    """
    manifest_path = _REPORT_PATH.parent / "packs" / "lgbm-v1" / "manifest.json"

    if not _REPORT_PATH.exists():
        raise AssertionError(f"Missing lightgbm-report.json at {_REPORT_PATH}.")
    if not manifest_path.exists():
        raise AssertionError(f"Missing manifest.json at {manifest_path}.")

    blob = _load_stored_report()
    computed_hash = _digest(blob)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored_hash = manifest.get("eval_report_hash", "")

    assert stored_hash, "manifest.eval_report_hash is empty"
    assert computed_hash == stored_hash, (
        f"§B.7 provenance mismatch:\n"
        f"  manifest.eval_report_hash : {stored_hash}\n"
        f"  SHA-256(committed report) : {computed_hash}\n"
        "The hash and the committed JSON are out of sync.\n"
        "Re-run with UPDATE_SNAPSHOTS=1 to regenerate both, then commit the pair."
    )


# ── §B.7 provenance.json binding (hashes vs committed artifacts) ──────────────


def test_provenance_json_hashes_match_committed_artifacts() -> None:
    """docs/v2/ml/provenance.json is a hand-maintained provenance record whose
    eval.report_sha256 and model.weights_sha256 must match the bytes actually
    committed. Nothing regenerates it automatically (UPDATE_SNAPSHOTS rewrites
    the report/manifest/pack but not this scaffold), so without this test a
    regeneration silently leaves provenance.json pointing at bytes that no
    longer exist. This is a pure file-hash check (no ml extra), so it runs in
    the main regression-gate, not only ml-gate.
    """
    prov_path = _REPORT_PATH.parent / "provenance.json"
    weights_path = _REPORT_PATH.parent / "packs" / "lgbm-v1" / "model.joblib"

    if not prov_path.exists():
        raise AssertionError(f"Missing provenance.json at {prov_path}.")
    if not _REPORT_PATH.exists():
        raise AssertionError(f"Missing lightgbm-report.json at {_REPORT_PATH}.")
    if not weights_path.exists():
        raise AssertionError(f"Missing model.joblib at {weights_path}.")

    prov = json.loads(prov_path.read_text(encoding="utf-8"))

    report_digest = _digest(_REPORT_PATH.read_bytes())
    stored_report_hash = prov.get("eval", {}).get("report_sha256", "")
    assert stored_report_hash == report_digest, (
        "provenance.json eval.report_sha256 is stale:\n"
        f"  provenance : {stored_report_hash}\n"
        f"  actual report bytes: {report_digest}\n"
        "Update docs/v2/ml/provenance.json to match the regenerated report."
    )

    weights_digest = _digest(weights_path.read_bytes())
    stored_weights_hash = prov.get("model", {}).get("weights_sha256", "")
    assert stored_weights_hash == weights_digest, (
        "provenance.json model.weights_sha256 is stale:\n"
        f"  provenance : {stored_weights_hash}\n"
        f"  actual model.joblib bytes: {weights_digest}\n"
        "Update docs/v2/ml/provenance.json to match the regenerated pack."
    )


# ── §A.7 regression gate: re-run training and assert metric stability ─────────


_LIVE_PACK = _REPORT_PATH.parent / "packs" / "lgbm-v1"


def test_lightgbm_regression_retrain(tmp_path: Path) -> None:
    """§A.7: Re-run train_and_evaluate (seed-pinned) and assert key metrics
    match the committed golden within a tight tolerance.

    This is the CI gate that detects accidental regressions in:
      - the training corpus (build_extended_fixtures / build_ood_fixtures)
      - the featurizer (flatten_features / build_column_features)
      - the classifier / calibration parameters

    Any unintentional change to these that shifts macro-F2, macro-recall, or
    per-class recall FAILS CI here.  Intentional changes must update the golden
    (UPDATE_SNAPSHOTS=1) and be committed with a clear explanation.

    Runtime: ~8-15 s (the model is small; acceptable for a metric gate).
    """
    import pytest

    pytest.importorskip("lightgbm")  # ml extra; trainer needs sklearn + lightgbm
    pytest.importorskip("sklearn")

    if not _LIVE_PACK.exists():
        pytest.skip("lgbm-v1 pack not found; run UPDATE_SNAPSHOTS=1 first")
    if not _REPORT_PATH.exists():
        pytest.skip(f"lightgbm-report.json not found at {_REPORT_PATH}")

    from decoy_engine.storm.model_pack.trainer import TRAIN_SEED, train_and_evaluate

    # Re-train to a temp directory (does not touch the committed pack).
    report = train_and_evaluate(out_dir=tmp_path / "lgbm-retrain", random_state=TRAIN_SEED)

    # Load the committed golden values.
    with _REPORT_PATH.open(encoding="utf-8") as f:
        golden = json.load(f)

    tol = 1e-4  # floating-point rounding tolerance

    assert abs(report.macro_f2 - golden["macro_f2"]) <= tol, (
        f"macro_f2 regression: got {report.macro_f2}, golden {golden['macro_f2']}"
    )
    assert abs(report.balanced_accuracy - golden["balanced_accuracy"]) <= tol, (
        f"balanced_accuracy regression: got {report.balanced_accuracy}, golden {golden['balanced_accuracy']}"
    )

    # Lift gate values.
    assert report.lift is not None, "EvalReport.lift is None"
    golden_lift = golden["lift"]
    assert abs(report.lift.lift_ppt - golden_lift["lift_ppt"]) <= 0.01, (
        f"lift_ppt regression: got {report.lift.lift_ppt}, golden {golden_lift['lift_ppt']}"
    )
    assert report.lift.gate_met is True, (
        f"Lift gate NOT met on retrain: {report.lift.lift_ppt} ppt "
        f"(need >= {report.lift.gate_threshold_ppt} ppt)"
    )

    # Per-class recall: every class present in the golden must match within tol.
    golden_per_class = {pc["field_type"]: pc for pc in golden["per_class"]}
    for pc in report.per_class:
        if pc.field_type not in golden_per_class:
            continue
        golden_recall = golden_per_class[pc.field_type]["recall"]
        if golden_recall is None or pc.recall is None:
            continue
        assert abs(pc.recall - golden_recall) <= tol, (
            f"per-class recall regression for {pc.field_type}: "
            f"got {pc.recall}, golden {golden_recall}"
        )


# ── §A.4 band-to-accuracy relationship (held-out test set) ───────────────────


def test_predict_column_band_accuracy() -> None:
    """§A.4 PROVISIONAL: verify band-to-accuracy relationship on the held-out
    test set using predict_column.

    Bands are PROVISIONAL (the held-out fold is too small for tight
    calibration guarantees; its exact size shifts with the pinned
    scikit-learn version).  The test asserts the minimum floor for each band:

      high   (proba >= 0.95): empirical accuracy >= 0.95.
              For lgbm-v1 with isotonic calibration on the expanded corpus,
              this band fires and dominates: measured 541/541 = 100% on the
              held-out fold.
      review (0.70 <= proba < 0.95): empirical accuracy >= 0.70. Measured
              review-band accuracy is 73.5% (25/34) -- a THIN margin over the
              0.70 floor asserted below, so a fold shift could turn this red.
              Treat a future failure here as a signal to investigate, not to
              silently loosen the floor.
      low    (below 0.70, above operating threshold): no floor asserted
              (not a confidence claim; shown for observability only).

    If the high-band sample count is 0, the test passes (band not triggered).
    """
    import pandas as pd
    import pytest

    pytest.importorskip("lightgbm")  # ml extra; trainer needs sklearn + lightgbm
    pytest.importorskip("sklearn")

    if not _LIVE_PACK.exists():
        pytest.skip("lgbm-v1 pack not found; run UPDATE_SNAPSHOTS=1 first")

    from decoy_engine.storm.eval.bands import HIGH_PRECISION_FLOOR, REVIEW_PRECISION_FLOOR
    from decoy_engine.storm.eval.corpus import build_extended_fixtures
    from decoy_engine.storm.eval.fixtures import NO_DETECTOR
    from decoy_engine.storm.eval.split import held_out_split
    from decoy_engine.storm.model_pack.trainer import (
        TRAIN_SEED,
        _build_feature_inputs,
        load_pack,
        predict_column,
    )

    pack = load_pack(_LIVE_PACK)

    # Rebuild the same held-out test split used during training.
    fixtures = build_extended_fixtures()
    X_flat, y, groups = _build_feature_inputs(fixtures)
    split_iter = held_out_split(X_flat, y, groups, n_splits=5, random_state=TRAIN_SEED)
    _train_idx, test_idx = next(split_iter)
    test_set = set(test_idx)

    # Map flat column index -> (fixture, col_name, truth_label).
    col_map: list[tuple[object, str, str]] = []
    for fx in fixtures:
        for col_name, label in fx.labels.items():
            col_map.append((fx, col_name, label))

    high_correct = 0
    high_total = 0
    review_correct = 0
    review_total = 0

    for flat_idx, (fx, col_name, truth) in enumerate(col_map):
        if flat_idx not in test_set:
            continue

        series: pd.Series = fx.df[col_name]  # type: ignore[union-attr]
        result = predict_column(pack, series, col_name)
        band = result["band"]
        predicted = result.get("predicted_type")
        # Convert None predicted_type to NO_DETECTOR for comparison.
        predicted_str = predicted if predicted is not None else NO_DETECTOR
        correct = predicted_str == truth

        if band == "high":
            high_total += 1
            if correct:
                high_correct += 1
        elif band == "review":
            review_total += 1
            if correct:
                review_correct += 1

    # High band: if triggered, must meet the 0.95 precision floor.
    if high_total > 0:
        high_acc = high_correct / high_total
        assert high_acc >= HIGH_PRECISION_FLOOR, (
            f"High-band accuracy {high_acc:.4f} < {HIGH_PRECISION_FLOOR} "
            f"(correct={high_correct}/{high_total}).  "
            "PROVISIONAL (small n); update model-card.md §A.4 if this fails."
        )

    # Review band: must meet the 0.70 precision floor.
    if review_total > 0:
        review_acc = review_correct / review_total
        assert review_acc >= REVIEW_PRECISION_FLOOR, (
            f"Review-band accuracy {review_acc:.4f} < {REVIEW_PRECISION_FLOOR} "
            f"(correct={review_correct}/{review_total}).  "
            "PROVISIONAL (small n); update model-card.md §A.4 if this fails."
        )

    # Observability: print counts for CI logs. Derive n_test from the actual
    # split so the log line cannot drift from the fold (it shifts with the
    # scikit-learn version; see the golden regeneration note in the header).
    n_test = len(test_set)
    print(
        f"\n§A.4 band accuracy (PROVISIONAL, n_test={n_test}):\n"
        f"  high   (>={HIGH_PRECISION_FLOOR}): {high_correct}/{high_total} "
        f"({'N/A - never triggered for lgbm-v1' if high_total == 0 else f'{high_correct / high_total:.3f}'})\n"
        f"  review ({REVIEW_PRECISION_FLOOR}-{HIGH_PRECISION_FLOOR}): "
        f"{review_correct}/{review_total} "
        f"({review_correct / review_total:.3f} >= {REVIEW_PRECISION_FLOOR} floor)"
        if review_total
        else ""
    )

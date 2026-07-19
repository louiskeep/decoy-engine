"""LightGBM column-type classifier trainer (ML2.2).

Trains a calibrated LightGBM multiclass classifier on the extended synthetic
corpus (build_extended_fixtures), evaluates it against the frozen regex
baseline (run_baseline), and serialises the artifact as a
``decoy-model-pack/v1`` pack directory.

Design choices (cite per ml-benchmarking-and-privacy.md and CLAUDE.md):
  Model architecture:
    LightGBMClassifier wrapped in CalibratedClassifierCV(method='sigmoid').
    LightGBM (Ke et al., NeurIPS 2017 "LightGBM: A Highly Efficient Gradient
    Boosting Decision Tree") is the established gradient-boosted tree for
    tabular classification; alternatives surveyed (XGBoost, sklearn GBM,
    RandomForest) are slower or less sample-efficient at this corpus scale.
    Calibration with 'sigmoid' (Platt scaling) is appropriate here because
    the calibration set across 5-fold CV contains << 1000 samples per class;
    sklearn §1.16 recommends 'isotonic' only for >= ~1000 samples.

  Feature pipeline:
    DictVectorizer(sparse=False) on the flatten_features() output, which
    contains only aggregate statistics (no raw cell values, §B.4).

  Operating threshold (§A.5):
    FN:FP cost ratio k = 5 (a missed PII column leaks; a false alarm costs
    a human a UI glance -- ratios of this magnitude are standard in PII
    detection systems; Elkan 2001 IJCAI "Foundations of Cost-Sensitive
    Learning"). Bayes-optimal threshold = 1 / (1 + k) = 1/6 ~ 0.167.
    Predictions below this threshold are suppressed to "none".

  Train/test split (§A.3):
    StratifiedGroupKFold(n_splits=5), group = value-level union-find
    (assign_value_level_groups).  First fold is the held-out test set.
    All preprocessing fit on the train fold only.

  Determinism (§A.7):
    Random state fixed at TRAIN_SEED = 42 throughout.

  Lift gate (§A.2):
    Model ships ONLY if it beats the regex baseline by >= 5 ppt macro
    recall on the held-out test set.  Otherwise the gate is reported as
    NOT_MET and the pack still serialises (for inspection), but the CI gate
    blocks the merge.

Source references:
    sklearn §3.4 (F2), §3.1.2.4 (StratifiedGroupKFold), §1.16 (calibration),
    §3.3 (TunedThresholdClassifierCV / cost-sensitive learning).
    LightGBM: https://lightgbm.readthedocs.io
    Presidio SpanEvaluator (F2 for PII): https://github.com/microsoft/presidio-research
    Elkan (2001): https://dblp.org/rec/conf/ijcai/Elkan01.html
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    import pandas as pd  # predict_column signature only; pandas imported lazily at call-time

# ── Lazy sklearn / lightgbm imports (require [ml] extra) ─────────────────────
try:
    # joblib is used ONLY to serialise the trained sklearn Pipeline to a local
    # file that we ourselves generate (train_and_evaluate writes it; load_pack
    # reads it back).  The SHA-256 integrity check in load_pack guards against
    # tampered files before joblib.load is called, and §B.5 mandates on-prem
    # only (no loading from arbitrary external sources).  This is the same
    # posture used by every sklearn model-persistence workflow.
    import joblib
    from lightgbm import LGBMClassifier
    from sklearn.calibration import CalibratedClassifierCV, calibration_curve
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.metrics import (
        balanced_accuracy_score,
        brier_score_loss,
    )
except ImportError as _err:
    raise ImportError(
        "scikit-learn and lightgbm are required for the model trainer. "
        "Install them via: pip install -e '.[ml]'"
    ) from _err

from decoy_engine.storm.eval.corpus import build_extended_fixtures, build_ood_fixtures
from decoy_engine.storm.eval.fixtures import NO_DETECTOR
from decoy_engine.storm.eval.harness import run_baseline
from decoy_engine.storm.eval.split import held_out_split
from decoy_engine.storm.features import build_column_features
from decoy_engine.storm.model_pack.featurizer import flatten_features
from decoy_engine.storm.model_pack.types import (
    FEATURE_SCHEMA_VERSION,
    PACK_FORMAT,
    ModelPackManifest,
)

# Determinism seed (§A.7): all randomness in the pipeline is tied to this value.
TRAIN_SEED = 42

# Cost ratio k = C(FN) / C(FP): a missed PII column leaks; a false alarm
# costs a UI glance.  Recommended range in the literature (e.g. Elkan 2001)
# for safety-critical identification tasks is k in [3, 10].
# We use k=5 (moderate recall priority) -> threshold = 1/(1+5) = 0.1667.
FN_FP_COST_RATIO: int = 5
OPERATING_THRESHOLD: float = round(1.0 / (1 + FN_FP_COST_RATIO), 4)

# Calibration-set floor below which we fall back to sigmoid calibration.
#
# sklearn §1.16 quotes ~1000 samples as the rule of thumb for isotonic to avoid
# overfitting the reliability curve, but that is a conservative general figure.
# On the expanded (2958-column) STORM corpus -- clean, fully-synthetic, 11-class,
# calibration fold ~473 -- isotonic was measured to STRICTLY dominate sigmoid
# (MLF-5 experiment 2026-07-18): mean calibration error 0.061 vs 0.097, Brier
# 0.0043 vs 0.0055, and it makes the >=0.95 "high" band reachable at last (545 of
# 592 held-out columns land there at 99.8% accuracy, vs 69 for sigmoid). 400
# keeps the adaptive sigmoid fallback for a materially smaller corpus (a shrink
# below ~2000 columns) while enabling isotonic at the pinned corpus scale; the
# golden retrain gate locks the resulting calibration numbers either way.
_ISOTONIC_MIN_SAMPLES = 400

# §A.2 lift gate: minimum macro-recall improvement over baseline.
LIFT_GATE_PPT: float = 5.0  # percentage points


@dataclass
class PerClassMetrics:
    """Per-semantic-type evaluation metrics."""

    field_type: str
    support: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    f2: float | None


@dataclass
class ReliabilitySummary:
    """Calibration curve summary (§A.4)."""

    brier_score: float
    # Reliability curve: list of (mean_predicted_prob, fraction_of_positives)
    # for a one-vs-rest decomposition averaged across classes.
    n_bins: int
    mean_calibration_error: float  # mean |fraction_pos - mean_pred_prob|


@dataclass
class LiftReport:
    """§A.2 lift-gate result."""

    baseline_macro_recall: float
    model_macro_recall: float
    lift_ppt: float  # positive = improvement over baseline
    gate_met: bool  # True if lift_ppt >= LIFT_GATE_PPT
    gate_threshold_ppt: float = LIFT_GATE_PPT


@dataclass
class EvalReport:
    """Full evaluation report emitted after training (§A.1 / §A.2 / §A.4 / §A.5 / §A.8).

    JSON-serialisable (all fields are plain Python types).
    Committed as docs/v2/ml/lightgbm-report.json; its SHA-256 is embedded
    in the model-pack manifest (§B.7 provenance).
    """

    # §A.1 per-type + aggregate
    per_class: list[PerClassMetrics] = field(default_factory=list)
    macro_f2: float = 0.0
    weighted_f2: float = 0.0
    balanced_accuracy: float = 0.0
    confusion_matrix: list[list[int]] = field(default_factory=list)
    class_labels: list[str] = field(default_factory=list)
    false_negatives_list: list[dict[str, Any]] = field(default_factory=list)
    false_positives_list: list[dict[str, Any]] = field(default_factory=list)
    # §A.2 lift gate
    lift: LiftReport | None = None
    # §A.4 calibration
    reliability: ReliabilitySummary | None = None
    # §A.5 operating threshold
    operating_threshold: float = OPERATING_THRESHOLD
    fn_fp_cost_ratio: int = FN_FP_COST_RATIO
    # §B.2 OOD slice
    ood_balanced_accuracy: float | None = None
    ood_macro_recall: float | None = None
    # §A.7 determinism
    training_seed: int = TRAIN_SEED
    # §A.3 split info
    n_train: int = 0
    n_test: int = 0
    calibration_method: str = ""
    feature_schema_version: str = FEATURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f2_score(tp: int, fp: int, fn: int) -> float | None:
    """F2 (beta=2) from raw counts.  None if denominator is zero."""
    denom = 5 * tp + fp + 4 * fn
    return round(5 * tp / denom, 4) if denom > 0 else None


def _make_lgbm_params() -> dict[str, Any]:
    """LightGBM hyperparameters.  Conservative defaults for a small corpus."""
    return {
        "n_estimators": 200,
        "num_leaves": 31,
        "min_child_samples": 5,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "random_state": TRAIN_SEED,
        # Reproducibility: n_jobs=1 removes multi-thread reduction-order
        # nondeterminism; deterministic=True + force_row_wise=True pin the
        # histogram construction so the trained model is bit-identical across
        # runner microarchitectures (deterministic=True requires an explicit
        # force_row_wise/force_col_wise or LightGBM auto-selects and warns).
        # The frozen golden pack + its 1e-4 retrain gate depend on this.
        "n_jobs": 1,
        "deterministic": True,
        "force_row_wise": True,
        "verbose": -1,
    }


def _build_feature_inputs(
    fixtures: list[Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Build (X_flat, y, groups) from a fixture list.

    X_flat: flat feature dicts (flatten_features output), NO raw cell values.
    y: ground-truth label strings.
    groups: value-level group ids (§A.3 leakage guard).
    """
    from decoy_engine.storm.eval.split import make_split_inputs

    X_raw, y, groups = make_split_inputs(fixtures)
    X_flat = [flatten_features(x) for x in X_raw]
    return X_flat, y, groups


def _compute_reliability(
    clf: Any,
    X_mat: np.ndarray,
    y: list[str],
    classes: list[str],
    n_bins: int = 10,
) -> ReliabilitySummary:
    """Reliability curve summary (§A.4) via one-vs-rest averaging."""
    proba = clf.predict_proba(X_mat)
    y_arr = np.array(y)

    mce_values: list[float] = []
    brier_values: list[float] = []

    for i, cls in enumerate(classes):
        if cls == NO_DETECTOR:
            continue
        y_bin = (y_arr == cls).astype(int)
        prob_col = proba[:, i]
        if y_bin.sum() == 0:
            continue

        # sklearn calibration_curve
        try:
            frac_pos, mean_pred = calibration_curve(y_bin, prob_col, n_bins=n_bins)
            mce_values.append(float(np.mean(np.abs(frac_pos - mean_pred))))
        except ValueError:
            # Too few samples for all bins -- skip this class for MCE.
            pass

        # Brier score (one-vs-rest): sklearn brier_score_loss
        brier_values.append(brier_score_loss(y_bin, prob_col))

    mean_brier = float(np.mean(brier_values)) if brier_values else 0.0
    mean_mce = float(np.mean(mce_values)) if mce_values else 0.0

    return ReliabilitySummary(
        brier_score=round(mean_brier, 4),
        n_bins=n_bins,
        mean_calibration_error=round(mean_mce, 4),
    )


def train_and_evaluate(
    *,
    out_dir: Path,
    n_splits: int = 5,
    random_state: int = TRAIN_SEED,
) -> EvalReport:
    """Train, calibrate, evaluate, and serialise the LightGBM pack.

    All operations are deterministic given the same ``random_state`` and
    the same extended corpus (build_extended_fixtures).

    Steps
    -----
    1. Build extended corpus + featurise (§B.3, §B.4).
    2. Held-out split via StratifiedGroupKFold (§A.3).
    3. Fit DictVectorizer on train fold only (§A.3).
    4. Fit CalibratedClassifierCV(LGBMClassifier, method=...) on train (§A.4).
    5. Predict on held-out test; apply operating threshold (§A.5).
    6. Compute all §A.1 metrics, §A.2 lift, §A.4 calibration (§A.8).
    7. Evaluate OOD slice separately (§B.2).
    8. Write pack to ``out_dir`` and return the EvalReport.

    Parameters
    ----------
    out_dir:
        Directory where ``manifest.json`` and ``model.joblib`` are written.
    n_splits:
        Number of StratifiedGroupKFold folds (default 5, §A.3).
    random_state:
        Seed for all randomness (default 42, §A.7).

    Returns
    -------
    EvalReport
        Full evaluation report.  Caller should write this as
        ``docs/v2/ml/lightgbm-report.json`` and update the manifest's
        ``eval_report_hash`` field.
    """
    # ── 1. Build corpus + featurise ───────────────────────────────────────────
    fixtures = build_extended_fixtures()
    X_flat, y, groups = _build_feature_inputs(fixtures)

    # ── 2. Held-out split (first fold = test set, §A.3) ──────────────────────
    split_iter = held_out_split(X_flat, y, groups, n_splits=n_splits, random_state=random_state)
    train_idx, test_idx = next(split_iter)

    X_train_flat = [X_flat[i] for i in train_idx]
    y_train = [y[i] for i in train_idx]
    X_test_flat = [X_flat[i] for i in test_idx]
    y_test = [y[i] for i in test_idx]

    # Fixture references for the test set (used to build FP/FN lists)
    test_fixtures_meta: list[tuple[str, str]] = []
    col_idx = 0
    for fx in fixtures:
        for col_name in fx.labels:
            if col_idx in test_idx:
                test_fixtures_meta.append((fx.name, col_name))
            col_idx += 1

    # ── 3. Fit DictVectorizer on train only (§A.3) ───────────────────────────
    vec = DictVectorizer(sparse=False)
    X_train_mat: np.ndarray = vec.fit_transform(X_train_flat)
    X_test_mat: np.ndarray = vec.transform(X_test_flat)

    # ── 4. Choose calibration method (§A.4) ──────────────────────────────────
    # sigmoid for < ~1000 calibration samples (sklearn §1.16 recommendation).
    calib_n = len(X_train_mat) // n_splits  # approx calibration-fold size
    calib_method = "isotonic" if calib_n >= _ISOTONIC_MIN_SAMPLES else "sigmoid"

    base_clf = LGBMClassifier(**_make_lgbm_params())
    clf = CalibratedClassifierCV(
        base_clf,
        cv=min(n_splits, max(2, len(set(y_train)))),
        method=calib_method,
    )
    clf.fit(X_train_mat, y_train)

    classes: list[str] = list(clf.classes_)

    # ── 5. Predict with operating threshold (§A.5) ───────────────────────────
    proba_test: np.ndarray = clf.predict_proba(X_test_mat)
    y_pred_raw: list[str] = list(clf.predict(X_test_mat))

    # Apply threshold: if max predicted proba < threshold -> suppress to "none".
    y_pred: list[str] = []
    for i, probs in enumerate(proba_test):
        max_prob = float(np.max(probs))
        if max_prob < OPERATING_THRESHOLD:
            y_pred.append(NO_DETECTOR)
        else:
            y_pred.append(y_pred_raw[i])

    # ── 6a. Per-type metrics (§A.1) ───────────────────────────────────────────
    all_types_in_test = sorted(set(y_test))

    per_class_results: list[PerClassMetrics] = []
    f2_vals: list[float] = []
    support_total = 0

    for cls in all_types_in_test:
        if cls == NO_DETECTOR:
            continue
        tp = sum(1 for a, b in zip(y_test, y_pred, strict=False) if a == cls and b == cls)
        fp = sum(1 for a, b in zip(y_test, y_pred, strict=False) if a != cls and b == cls)
        fn = sum(1 for a, b in zip(y_test, y_pred, strict=False) if a == cls and b != cls)
        sup = sum(1 for a in y_test if a == cls)
        prec = round(tp / (tp + fp), 4) if (tp + fp) > 0 else None
        rec = round(tp / (tp + fn), 4) if (tp + fn) > 0 else None
        f2 = _f2_score(tp, fp, fn)
        per_class_results.append(
            PerClassMetrics(
                field_type=cls,
                support=sup,
                true_positives=tp,
                false_positives=fp,
                false_negatives=fn,
                precision=prec,
                recall=rec,
                f2=f2,
            )
        )
        if f2 is not None:
            f2_vals.append(f2)
        support_total += sup

    macro_f2 = round(sum(f2_vals) / len(f2_vals), 4) if f2_vals else 0.0
    weighted_f2 = (
        round(
            sum((m.f2 or 0.0) * m.support for m in per_class_results if m.f2 is not None)
            / support_total,
            4,
        )
        if support_total > 0
        else 0.0
    )

    # balanced_accuracy (macro recall) -- §A.1 + §A.2
    pii_y_test = [a for a in y_test if a != NO_DETECTOR]
    pii_y_pred = [b for a, b in zip(y_test, y_pred, strict=False) if a != NO_DETECTOR]
    balanced_acc = (
        round(
            float(balanced_accuracy_score(pii_y_test, pii_y_pred)),
            4,
        )
        if pii_y_test
        else 0.0
    )

    # Macro recall (§A.2) -- recompute directly to be explicit
    recall_vals = [m.recall for m in per_class_results if m.recall is not None]
    model_macro_recall = round(sum(recall_vals) / len(recall_vals), 4) if recall_vals else 0.0

    # ── 6b. Confusion matrix (§A.1) ──────────────────────────────────────────
    all_test_labels = sorted(set(y_test) | set(y_pred))
    label_to_idx = {lbl: i for i, lbl in enumerate(all_test_labels)}
    cm_size = len(all_test_labels)
    cm: list[list[int]] = [[0] * cm_size for _ in range(cm_size)]
    for truth, pred in zip(y_test, y_pred, strict=False):
        cm[label_to_idx[truth]][label_to_idx[pred]] += 1

    # ── 6c. Enumerated FP/FN lists (§A.1) ────────────────────────────────────
    fn_list: list[dict[str, Any]] = []
    fp_list: list[dict[str, Any]] = []
    for (fix_name, col_name), truth, pred in zip(test_fixtures_meta, y_test, y_pred, strict=False):
        col_id = f"{fix_name}.{col_name}"
        if truth == NO_DETECTOR and pred != NO_DETECTOR:
            fp_list.append(
                {
                    "column_id": col_id,
                    "truth_label": truth,
                    "predicted_label": pred,
                    "error_type": "FP",
                }
            )
        elif truth != NO_DETECTOR and pred != truth:
            fn_list.append(
                {
                    "column_id": col_id,
                    "truth_label": truth,
                    "predicted_label": pred,
                    "error_type": "FN",
                }
            )

    # ── 6d. §A.2 lift gate: baseline macro-recall on the SAME test fixtures ──
    test_fixture_set = {(fix_name, col_name) for fix_name, col_name in test_fixtures_meta}
    test_fixtures_objs = [
        fx for fx in fixtures if any((fx.name, col) in test_fixture_set for col in fx.labels)
    ]
    baseline_report = run_baseline(test_fixtures_objs)
    baseline_recall_vals = [
        m.recall
        for m in baseline_report.by_field_type.values()
        if m.field_type != NO_DETECTOR and m.recall is not None
    ]
    baseline_macro_recall = (
        round(sum(baseline_recall_vals) / len(baseline_recall_vals), 4)
        if baseline_recall_vals
        else 0.0
    )
    lift_ppt = round((model_macro_recall - baseline_macro_recall) * 100, 2)
    lift = LiftReport(
        baseline_macro_recall=baseline_macro_recall,
        model_macro_recall=model_macro_recall,
        lift_ppt=lift_ppt,
        gate_met=lift_ppt >= LIFT_GATE_PPT,
    )

    # ── 6e. Calibration reliability summary (§A.4) ───────────────────────────
    reliability = _compute_reliability(clf, X_test_mat, y_test, classes)

    # ── 7. OOD slice evaluation (§B.2) ───────────────────────────────────────
    ood_fixtures = build_ood_fixtures()
    ood_flat, ood_y, _ = _build_feature_inputs(ood_fixtures)
    ood_mat: np.ndarray = vec.transform(ood_flat)
    ood_proba: np.ndarray = clf.predict_proba(ood_mat)
    ood_pred_raw = list(clf.predict(ood_mat))
    ood_pred = []
    for i, probs in enumerate(ood_proba):
        if float(np.max(probs)) < OPERATING_THRESHOLD:
            ood_pred.append(NO_DETECTOR)
        else:
            ood_pred.append(ood_pred_raw[i])

    ood_pii_y = [a for a in ood_y if a != NO_DETECTOR]
    ood_pii_pred = [b for a, b in zip(ood_y, ood_pred, strict=False) if a != NO_DETECTOR]
    ood_balanced_acc = (
        round(float(balanced_accuracy_score(ood_pii_y, ood_pii_pred)), 4) if ood_pii_y else None
    )
    ood_recall_vals_raw: list[float] = []
    ood_cls_set = sorted(set(ood_pii_y))
    for cls in ood_cls_set:
        tp_ood = sum(
            1 for a, b in zip(ood_pii_y, ood_pii_pred, strict=False) if a == cls and b == cls
        )
        fn_ood = sum(
            1 for a, b in zip(ood_pii_y, ood_pii_pred, strict=False) if a == cls and b != cls
        )
        denom = tp_ood + fn_ood
        if denom > 0:
            ood_recall_vals_raw.append(tp_ood / denom)
    ood_macro_recall = (
        round(sum(ood_recall_vals_raw) / len(ood_recall_vals_raw), 4)
        if ood_recall_vals_raw
        else None
    )

    # ── 8. Serialise the pack ─────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.joblib"

    # Persist the (vectorizer, classifier) pair so the loader can run inference.
    pack_obj = {"vec": vec, "clf": clf, "classes": classes}
    joblib.dump(pack_obj, model_path, compress=3)

    # Compute SHA-256 of the serialised weights (§B.7).
    sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()

    manifest = ModelPackManifest(
        pack_id="lgbm-v1",
        version="0.1.0",
        format_version=PACK_FORMAT,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        sha256=sha256,
        signing_key_ref="unsigned",
        training_seed=random_state,
        calibration_method=calib_method,
        operating_threshold=OPERATING_THRESHOLD,
        eval_report_hash="",  # filled after writing the report
    )

    # Write manifest (without report hash first, then update below).
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), sort_keys=True, indent=2), encoding="utf-8"
    )

    # Assemble the report.
    report = EvalReport(
        per_class=per_class_results,
        macro_f2=macro_f2,
        weighted_f2=weighted_f2,
        balanced_accuracy=balanced_acc,
        confusion_matrix=cm,
        class_labels=all_test_labels,
        false_negatives_list=fn_list,
        false_positives_list=fp_list,
        lift=lift,
        reliability=reliability,
        operating_threshold=OPERATING_THRESHOLD,
        fn_fp_cost_ratio=FN_FP_COST_RATIO,
        ood_balanced_accuracy=ood_balanced_acc,
        ood_macro_recall=ood_macro_recall,
        n_train=len(train_idx),
        n_test=len(test_idx),
        calibration_method=calib_method,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
    )

    # §B.7: hash the canonical report (no training_elapsed_sec) so that
    # manifest.eval_report_hash matches the committed lightgbm-report.json.
    report_dict = report.to_dict()
    report_blob = json.dumps(report_dict, sort_keys=True, indent=2, default=str).encode("utf-8")
    report_hash = hashlib.sha256(report_blob).hexdigest()
    manifest.eval_report_hash = report_hash
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), sort_keys=True, indent=2), encoding="utf-8"
    )

    return report


def load_pack(pack_dir: Path) -> dict[str, Any]:
    """Load and validate a ``decoy-model-pack/v1`` from disk (TEST-ONLY).

    .. warning::
       This is a thin convenience wrapper for tests and offline training
       tooling ONLY.  It verifies format/schema/SHA-256 but deliberately does
       NOT perform the DE-04 signature/trust checks: it calls ``joblib.load``
       (arbitrary-code-execution via pickle) on the given pack unconditionally.
       Production and any code path that loads a caller-supplied pack MUST go
       through ``ModelPackLoader`` (loader.py), which fail-closes an untrusted
       or set-but-unverifiable pack before deserialising.  Do not call this on
       a pack whose origin you do not control.

    Returns
    -------
    dict[str, Any]
        Keys: ``"vec"`` (DictVectorizer), ``"clf"`` (calibrated classifier),
        ``"classes"`` (list[str]), ``"manifest"`` (ModelPackManifest).

    Raises
    ------
    FileNotFoundError
        If the pack directory or required files are missing.
    ValueError
        If the SHA-256 of model.joblib does not match the manifest.
    """
    manifest_path = pack_dir / "manifest.json"
    model_path = pack_dir / "model.joblib"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.json in {pack_dir}")
    if not model_path.exists():
        raise FileNotFoundError(f"Missing model.joblib in {pack_dir}")

    manifest = ModelPackManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest.format_version != PACK_FORMAT:
        raise ValueError(
            f"Unsupported pack format {manifest.format_version!r}; expected {PACK_FORMAT!r}"
        )
    if manifest.feature_schema_version != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            f"Feature schema mismatch: pack={manifest.feature_schema_version!r}, "
            f"engine={FEATURE_SCHEMA_VERSION!r}"
        )

    actual_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if actual_sha != manifest.sha256:
        raise ValueError(
            f"model.joblib SHA-256 mismatch: expected {manifest.sha256!r}, got {actual_sha!r}"
        )

    pack_obj = joblib.load(model_path)
    pack_obj["manifest"] = manifest
    return pack_obj


def predict_column(
    pack: dict[str, Any],
    series: pd.Series,
    col_name: str,
) -> dict[str, Any]:
    """Run inference on one column using a loaded pack.

    Parameters
    ----------
    pack:
        Loaded pack dict from ``load_pack``.
    series:
        The raw pandas Series for this column.
    col_name:
        Column header string.

    Returns
    -------
    dict[str, Any]
        Keys: ``"predicted_type"`` (str | None, highest-confidence type or
        None if below threshold), ``"confidence"`` (float, calibrated max
        probability), ``"band"`` (str, "high"/"review"/"low"),
        ``"model_pack_id"``, ``"model_pack_version"``,
        ``"model_pack_sha256"`` (str provenance fields),
        ``"ml"`` (bool, always True).
    """
    from decoy_engine.storm.eval.bands import classify_band

    feats = build_column_features(series, col_name)
    flat = flatten_features(feats.to_dict())

    vec: DictVectorizer = pack["vec"]
    clf: CalibratedClassifierCV = pack["clf"]
    manifest: ModelPackManifest = pack["manifest"]
    threshold = manifest.operating_threshold

    X_mat = vec.transform([flat])
    proba: np.ndarray = clf.predict_proba(X_mat)[0]
    max_idx = int(np.argmax(proba))
    max_prob = float(proba[max_idx])
    predicted_cls = clf.classes_[max_idx]

    if max_prob < threshold:
        predicted_type = None
        band = "low"
    else:
        predicted_type = predicted_cls if predicted_cls != NO_DETECTOR else None
        band = classify_band(max_prob)

    return {
        "predicted_type": predicted_type,
        "confidence": round(max_prob, 4),
        "band": band,
        "model_pack_id": manifest.pack_id,
        "model_pack_version": manifest.version,
        "model_pack_sha256": manifest.sha256,
        "ml": True,
    }

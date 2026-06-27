"""Regex-detector baseline harness (BF2 / ML0).

Runs the real, registered detector set (``run_all_detectors``) over the
labeled fixtures and measures, per field type: precision, recall, F2
(β=2, recall-weighted), review-burden (count of medium-confidence matches
a human must confirm), and false negatives. Aggregate metrics: macro-F2,
weighted-F2, balanced_accuracy, an entity-type confusion matrix, and
enumerated FP/FN lists. This is the evidence artifact that PROVES where
the regex detectors miss - the honest baseline a future ML column
classifier has to beat.

F2 formula (sklearn §3.4; Presidio SpanEvaluator applies β=2 for PII
because recall matters more than precision):
    F_β = (1 + β²) · tp / ((1 + β²) · tp + fp + β² · fn)
    For β=2: F2 = 5·tp / (5·tp + fp + 4·fn)
A missed PII column leaks; a false alarm costs a UI glance.

Read-only over the detectors: it introduces no public run-path change and
mutates nothing. The metrics dataclasses follow the pure-dataclass /
JSON-serializable convention in ``storm/types.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from decoy_engine.storm.detectors import run_all_detectors
from decoy_engine.storm.eval.fixtures import NO_DETECTOR, LabeledFixture, build_fixtures

# Sentinel for the predicted-label slot in the confusion matrix when no
# detector fired. Kept out of the public namespace (callers see None in
# ColumnResult.predicted_id); only the confusion_matrix dict uses it.
_PRED_NONE = "none"


@dataclass
class ColumnResult:
    """The detector outcome for one fixture column vs its ground truth."""

    fixture: str
    column: str
    truth_label: str
    predicted_id: str | None  # winning (highest match_rate) detector, or None
    predicted_confidence: str | None
    predicted_match_rate: float | None
    correct: bool
    medium_match_count: int  # matches needing human confirmation
    fired_detector_ids: list[str] = field(default_factory=list)


@dataclass
class FieldTypeMetrics:
    """Precision / recall / F2 aggregated over columns of one ground-truth type.

    F2 (β=2) is the primary metric per ml-benchmarking-and-privacy.md §A.1.
    ``false_positives`` counts columns predicted as this type whose ground
    truth is a different type (including NO_DETECTOR).
    """

    field_type: str
    support: int  # number of columns with this truth label
    true_positives: int  # correctly identified
    false_negatives: int
    recall: float | None  # None for the NO_DETECTOR type (no recall concept)
    review_burden: int  # medium-confidence matches across these columns
    false_negative_columns: list[str] = field(default_factory=list)
    # Extended metrics (ML0 gate §A.1) ── default 0/None so existing callers
    # that inspect only recall/review_burden are not broken.
    false_positives: int = 0
    precision: float | None = None
    f2: float | None = None  # F2 with β=2


@dataclass
class HarnessReport:
    """Full baseline measurement over the fixtures. JSON-serializable.

    Extended for ml-benchmarking-and-privacy.md §A.1 / §A.7:
    - F2 per type + macro-F2 + weighted-F2 + balanced_accuracy
    - Entity-type confusion matrix (truth rows x predicted columns)
    - Enumerated FP/FN list (column_id + predicted/true label)
    """

    columns: list[ColumnResult] = field(default_factory=list)
    by_field_type: dict[str, FieldTypeMetrics] = field(default_factory=dict)
    precision_by_predicted: dict[str, float] = field(default_factory=dict)
    overall_recall: float = 0.0
    false_positive_count: int = 0  # NO_DETECTOR columns where a detector fired
    total_review_burden: int = 0
    # Aggregate F2 metrics (§A.1) ────────────────────────────────────────────
    macro_f2: float = 0.0  # unweighted mean of per-type F2 (equal weight to rare types)
    weighted_f2: float = 0.0  # corpus-prevalence weighted mean of per-type F2
    balanced_accuracy: float = 0.0  # macro-average recall; imbalance sanity check
    # Confusion matrix: truth_label -> predicted_label -> count.
    # Predicted "none" means no detector fired (ColumnResult.predicted_id is None).
    confusion_matrix: dict[str, dict[str, int]] = field(default_factory=dict)
    # Enumerated error lists (§A.1 "inspectable, never an aggregate number alone")
    false_negatives_list: list[dict[str, str | None]] = field(default_factory=list)
    false_positives_list: list[dict[str, str | None]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _evaluate_column(
    fixture: str,
    column: str,
    series: pd.Series,
    truth_label: str,
) -> ColumnResult:
    matches = run_all_detectors(series, column)
    winner = matches[0] if matches else None
    predicted_id = winner.detector_id if winner else None
    if truth_label == NO_DETECTOR:
        correct = predicted_id is None
    else:
        correct = predicted_id == truth_label
    medium = sum(1 for m in matches if m.confidence == "medium")
    return ColumnResult(
        fixture=fixture,
        column=column,
        truth_label=truth_label,
        predicted_id=predicted_id,
        predicted_confidence=winner.confidence if winner else None,
        predicted_match_rate=winner.match_rate if winner else None,
        correct=correct,
        medium_match_count=medium,
        fired_detector_ids=[m.detector_id for m in matches],
    )


def _f2(tp: int, fp: int, fn: int) -> float | None:
    """F2 score (β=2) from raw counts. Returns None if denominator is zero."""
    denom = 5 * tp + fp + 4 * fn
    return round(5 * tp / denom, 4) if denom > 0 else None


def run_baseline(fixtures: list[LabeledFixture] | None = None) -> HarnessReport:
    """Run the registered detectors over the fixtures and build the report.

    The report is the frozen baseline artifact for ml-benchmarking-and-privacy.md
    §A.7: same corpus + same seed → identical report bytes (via to_dict() with
    sorted keys).
    """
    if fixtures is None:
        fixtures = build_fixtures()

    results: list[ColumnResult] = []
    for fx in fixtures:
        for column, truth in fx.labels.items():
            results.append(_evaluate_column(fx.name, column, fx.df[column], truth))

    # ── Per-ground-truth-type aggregation ────────────────────────────────────
    by_type: dict[str, FieldTypeMetrics] = {}
    for r in results:
        m = by_type.get(r.truth_label)
        if m is None:
            m = FieldTypeMetrics(
                field_type=r.truth_label,
                support=0,
                true_positives=0,
                false_negatives=0,
                recall=None,
                review_burden=0,
            )
            by_type[r.truth_label] = m
        m.support += 1
        m.review_burden += r.medium_match_count
        if r.truth_label == NO_DETECTOR:
            # No recall concept; a "miss" here is a false positive instead.
            continue
        if r.correct:
            m.true_positives += 1
        else:
            m.false_negatives += 1
            m.false_negative_columns.append(f"{r.fixture}.{r.column}")
    for m in by_type.values():
        if m.field_type != NO_DETECTOR and m.support > 0:
            m.recall = round(m.true_positives / m.support, 4)

    # ── Precision per predicted detector id ──────────────────────────────────
    # Of columns predicted as D, the fraction whose truth is also D.
    pred_total: dict[str, int] = {}
    pred_correct: dict[str, int] = {}
    for r in results:
        if r.predicted_id is None:
            continue
        pred_total[r.predicted_id] = pred_total.get(r.predicted_id, 0) + 1
        if r.predicted_id == r.truth_label:
            pred_correct[r.predicted_id] = pred_correct.get(r.predicted_id, 0) + 1
    precision_by_predicted = {
        det_id: round(pred_correct.get(det_id, 0) / total, 4)
        for det_id, total in sorted(pred_total.items())
    }

    # ── Per-type false-positives + precision + F2 (§A.1) ────────────────────
    # FP for truth type T = columns predicted as T whose ground truth is not T.
    # This equals pred_total[T] - pred_correct[T].
    for m in by_type.values():
        if m.field_type == NO_DETECTOR:
            continue
        t = m.field_type
        fp = pred_total.get(t, 0) - pred_correct.get(t, 0)
        m.false_positives = fp
        tp = m.true_positives
        fn = m.false_negatives
        prec_denom = tp + fp
        m.precision = round(tp / prec_denom, 4) if prec_denom > 0 else None
        m.f2 = _f2(tp, fp, fn)

    # ── Overall recall ────────────────────────────────────────────────────────
    pii_results = [r for r in results if r.truth_label != NO_DETECTOR]
    overall_recall = (
        round(sum(1 for r in pii_results if r.correct) / len(pii_results), 4)
        if pii_results
        else 0.0
    )
    false_positives = sum(
        1 for r in results if r.truth_label == NO_DETECTOR and r.predicted_id is not None
    )

    # ── Aggregate F2 metrics (§A.1) ───────────────────────────────────────────
    pii_types = [m for m in by_type.values() if m.field_type != NO_DETECTOR]
    f2_vals = [m.f2 for m in pii_types if m.f2 is not None]
    macro_f2 = round(sum(f2_vals) / len(f2_vals), 4) if f2_vals else 0.0

    total_support = sum(m.support for m in pii_types)
    weighted_f2 = (
        round(
            sum((m.f2 or 0.0) * m.support for m in pii_types) / total_support,
            4,
        )
        if total_support > 0
        else 0.0
    )

    recall_vals = [m.recall for m in pii_types if m.recall is not None]
    balanced_accuracy = round(sum(recall_vals) / len(recall_vals), 4) if recall_vals else 0.0

    # ── Confusion matrix (truth x predicted, "none" for no prediction) ────────
    confusion_matrix: dict[str, dict[str, int]] = {}
    for r in results:
        pred_key = r.predicted_id if r.predicted_id is not None else _PRED_NONE
        row = confusion_matrix.setdefault(r.truth_label, {})
        row[pred_key] = row.get(pred_key, 0) + 1

    # ── Enumerated FP/FN lists (§A.1) ─────────────────────────────────────────
    false_negatives_list: list[dict[str, str | None]] = []
    false_positives_list: list[dict[str, str | None]] = []
    for r in results:
        col_id = f"{r.fixture}.{r.column}"
        if r.truth_label == NO_DETECTOR:
            if r.predicted_id is not None:
                false_positives_list.append(
                    {
                        "column_id": col_id,
                        "truth_label": r.truth_label,
                        "predicted_label": r.predicted_id,
                        "error_type": "FP",
                    }
                )
        elif not r.correct:
            false_negatives_list.append(
                {
                    "column_id": col_id,
                    "truth_label": r.truth_label,
                    "predicted_label": r.predicted_id,
                    "error_type": "FN",
                }
            )

    return HarnessReport(
        columns=results,
        by_field_type=by_type,
        precision_by_predicted=precision_by_predicted,
        overall_recall=overall_recall,
        false_positive_count=false_positives,
        total_review_burden=sum(r.medium_match_count for r in results),
        macro_f2=macro_f2,
        weighted_f2=weighted_f2,
        balanced_accuracy=balanced_accuracy,
        confusion_matrix=confusion_matrix,
        false_negatives_list=false_negatives_list,
        false_positives_list=false_positives_list,
    )

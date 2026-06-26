"""Regex-detector baseline harness (BF2 / ML0).

Runs the real, registered detector set (``run_all_detectors``) over the
labeled fixtures and measures, per field type: recall, precision,
review-burden (count of medium-confidence matches a human must confirm),
and false negatives. This is the evidence artifact that PROVES where the
regex detectors miss - the honest baseline a future ML column classifier
has to beat.

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
    """Recall / review-burden aggregated over columns of one ground-truth type."""

    field_type: str
    support: int  # number of columns with this truth label
    true_positives: int  # correctly identified
    false_negatives: int
    recall: float | None  # None for the NO_DETECTOR type (no recall concept)
    review_burden: int  # medium-confidence matches across these columns
    false_negative_columns: list[str] = field(default_factory=list)


@dataclass
class HarnessReport:
    """Full baseline measurement over the fixtures. JSON-serializable."""

    columns: list[ColumnResult] = field(default_factory=list)
    by_field_type: dict[str, FieldTypeMetrics] = field(default_factory=dict)
    precision_by_predicted: dict[str, float] = field(default_factory=dict)
    overall_recall: float = 0.0
    false_positive_count: int = 0  # NO_DETECTOR columns where a detector fired
    total_review_burden: int = 0

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


def run_baseline(fixtures: list[LabeledFixture] | None = None) -> HarnessReport:
    """Run the registered detectors over the fixtures and build the report."""
    if fixtures is None:
        fixtures = build_fixtures()

    results: list[ColumnResult] = []
    for fx in fixtures:
        for column, truth in fx.labels.items():
            results.append(_evaluate_column(fx.name, column, fx.df[column], truth))

    # Aggregate per ground-truth field type.
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

    # Precision per predicted detector id: of columns predicted D, fraction
    # whose truth is also D.
    pred_total: dict[str, int] = {}
    pred_correct: dict[str, int] = {}
    for r in results:
        if r.predicted_id is None:
            continue
        pred_total[r.predicted_id] = pred_total.get(r.predicted_id, 0) + 1
        if r.predicted_id == r.truth_label:
            pred_correct[r.predicted_id] = pred_correct.get(r.predicted_id, 0) + 1
    precision = {
        det_id: round(pred_correct.get(det_id, 0) / total, 4)
        for det_id, total in sorted(pred_total.items())
    }

    pii_results = [r for r in results if r.truth_label != NO_DETECTOR]
    overall_recall = (
        round(sum(1 for r in pii_results if r.correct) / len(pii_results), 4)
        if pii_results
        else 0.0
    )
    false_positives = sum(
        1 for r in results if r.truth_label == NO_DETECTOR and r.predicted_id is not None
    )

    return HarnessReport(
        columns=results,
        by_field_type=by_type,
        precision_by_predicted=precision,
        overall_recall=overall_recall,
        false_positive_count=false_positives,
        total_review_burden=sum(r.medium_match_count for r in results),
    )

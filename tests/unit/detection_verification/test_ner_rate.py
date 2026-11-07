"""SCAN-ML M1: the shared NER cell sample, per-detector rate and rate bucket."""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.detection import _ner_rate as nr
from decoy_engine.detection.detectors import Span


def _spans_for(texts: Any, **_k: Any) -> list[list[Span]]:
    # "P" marks a person span, "L" a location span; the text itself is what must never leak.
    out = []
    for text in texts:
        found = []
        if "P" in text:
            found += [Span("person_name", 0, 1, "SECRET-NAME")] * text.count("P")
        if "L" in text:
            found.append(Span("location", 0, 1, "SECRET-PLACE"))
        out.append(found)
    return out


def test_scan_cells_is_the_plan_value() -> None:
    assert nr.NER_SCAN_CELLS == 2_000


def test_sample_is_the_first_scan_cells_non_null_strings() -> None:
    values = [None, "", float("nan"), 7, "a", None, "b"] + ["c"] * 5000
    sample = nr.sample_cells(values)
    assert len(sample) == nr.NER_SCAN_CELLS
    assert sample[:3] == ["a", "b", "c"]
    assert nr.sample_cells([None, 3, ""]) == []


def test_rate_counts_cells_with_at_least_one_span_of_that_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nr, "iter_ner_spans_batch", _spans_for)
    rates = nr.ner_rates(["PP", "P", "L", "x", "PL", "y", "z", "w", "v", "u"])
    assert set(rates) == {"person_name", "location"}
    assert (rates["person_name"].hit_cells, rates["person_name"].scanned_cells) == (3, 10)
    assert (rates["location"].hit_cells, rates["location"].scanned_cells) == (2, 10)
    assert rates["person_name"].rate == pytest.approx(0.3)


def test_the_helper_returns_counts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nr, "iter_ner_spans_batch", _spans_for)
    shown = repr(nr.ner_rates(["PL"] * 5))
    assert "SECRET" not in shown


def test_an_empty_sample_has_zero_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nr, "iter_ner_spans_batch", _spans_for)
    rates = nr.ner_rates([None, ""])
    assert all(r.rate == 0.0 and r.scanned_cells == 0 for r in rates.values())


@pytest.mark.parametrize(
    ("rate", "bucket"),
    [
        (0.0, "<10%"),
        (0.0999, "<10%"),
        (0.1, "10-50%"),
        (0.4999, "10-50%"),
        (0.5, ">=50%"),
        (1.0, ">=50%"),
    ],
)
def test_rate_buckets(rate: float, bucket: str) -> None:
    assert nr.rate_bucket(rate) == bucket

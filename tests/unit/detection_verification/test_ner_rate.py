"""The shared NER cell sample, per-detector rate and rate bucket."""

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


def _prose(markers: list[str]) -> list[str]:
    """Cells with three or more tokens, so the column is free text and gets scored."""
    return [f"{m} then one two" for m in markers]


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
    rates = nr.ner_rates(_prose(["PP", "P", "L", "x", "PL", "y", "z", "w", "v", "u"]))
    assert set(rates) == {"person_name", "location"}
    assert (rates["person_name"].hit_cells, rates["person_name"].scanned_cells) == (3, 10)
    assert (rates["location"].hit_cells, rates["location"].scanned_cells) == (2, 10)
    assert rates["person_name"].rate == pytest.approx(0.3)


def test_the_helper_returns_counts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nr, "iter_ner_spans_batch", _spans_for)
    shown = repr(nr.ner_rates(_prose(["PL"] * 5)))
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


def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("a cell reached the NER pipeline")


def test_a_column_of_short_person_names_is_not_scored_and_reaches_no_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nr, "iter_ner_spans_batch", _boom)
    names = [f"Given{i} Family{i}" for i in range(300)]  # two tokens each
    rates = nr.ner_rates(names)
    assert set(rates) == {"person_name", "location"}
    for rate in rates.values():
        assert (rate.hit_cells, rate.scanned_cells, rate.rate) == (0, 0, 0.0)


def test_a_free_text_column_does_reach_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nr, "iter_ner_spans_batch", _spans_for)
    rates = nr.ner_rates(["P went to the shop today"] * 4)
    assert (rates["person_name"].hit_cells, rates["person_name"].scanned_cells) == (4, 4)


def test_the_free_text_threshold_is_three_tokens() -> None:
    assert nr.FREE_TEXT_MIN_TOKENS == 3


@pytest.mark.parametrize(
    ("token_counts", "expected"),
    [
        ([3], True),  # a median of exactly 3
        ([2], False),
        ([2, 3], False),  # even sample: median 2.5
        ([3, 3], True),
        ([1, 3, 5], True),
        ([1, 1, 5], False),
        ([1, 1, 7], False),  # mean 3, median 1: the rule is the median
        ([0, 3, 3], True),  # mean 2, median 3
        ([], False),  # an empty sample is not free text
    ],
)
def test_is_free_text_boundary(token_counts: list[int], expected: bool) -> None:
    cells = ["w " * n for n in token_counts]
    assert nr.is_free_text([c.strip() for c in cells]) is expected


def test_tokens_are_counted_by_whitespace_split() -> None:
    assert nr.is_free_text(["a   b\tc", "d\ne f"])  # runs of whitespace count once
    assert not nr.is_free_text(["a-b-c-d", "e/f/g"])  # no whitespace, one token each


def test_the_threshold_is_the_accepted_value() -> None:
    assert nr.NER_MIN_RATE == 0.211


@pytest.mark.parametrize(
    ("hits", "cells", "expected"),
    [
        (211, 1000, True),  # exactly at the threshold
        (210, 1000, False),
        (212, 1000, True),
        (1000, 1000, True),
        (0, 1000, False),
        (0, 0, False),  # nothing scanned
        (5, 0, False),
    ],
)
def test_hit_is_at_or_over_the_threshold_and_needs_scanned_cells(
    hits: int, cells: int, expected: bool
) -> None:
    assert nr.NerRate("person_name", hits, cells).hit is expected


def test_a_column_that_was_not_scored_has_no_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nr, "iter_ner_spans_batch", _boom)
    assert not any(r.hit for r in nr.ner_rates([f"Given{i} Family{i}" for i in range(50)]).values())

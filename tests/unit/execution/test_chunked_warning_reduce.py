"""The cross-chunk warning fold: count codes sum, everything else equality-dedups."""

from __future__ import annotations

from decoy_engine.execution._chunked_warning_reduce import fold_warning
from decoy_engine.generation.pool import QualityWarning


def _fpe(affected: int, total: int, column: str = "c") -> QualityWarning:
    return QualityWarning(
        "fpe_partial_plaintext_disclosure",
        "fpe",
        column,
        {"affected_values": affected, "total_values": total, "note": "n"},
    )


def _fold(*warnings: QualityWarning) -> list[QualityWarning]:
    seen: list[QualityWarning] = []
    for w in warnings:
        fold_warning(seen, w)
    return seen


def test_equal_count_warnings_from_two_chunks_are_summed_not_deduplicated() -> None:
    assert _fold(_fpe(7, 7), _fpe(7, 7)) == [_fpe(14, 14)]


def test_position_of_the_first_occurrence_is_kept() -> None:
    other = QualityWarning("undeclared_output_columns", "x", None, {})
    assert _fold(_fpe(7, 7), other, _fpe(6, 6), other) == [_fpe(13, 13), other]


def test_different_columns_stay_separate() -> None:
    assert _fold(_fpe(1, 1, "a"), _fpe(2, 2, "b")) == [_fpe(1, 1, "a"), _fpe(2, 2, "b")]


def test_text_mask_by_detector_counts_sum_per_key() -> None:
    def tm(by: dict[str, int]) -> QualityWarning:
        return QualityWarning(
            "text_mask_sub_floor_span_handled",
            "text_mask",
            "c",
            {"policy": "redact", "by_detector": by, "total": sum(by.values()), "note": "n"},
        )

    merged = _fold(tm({"us_zip": 2}), tm({"us_zip": 1, "ssn": 4}))
    assert merged == [tm({"us_zip": 3, "ssn": 4})]


def test_an_unexpected_detail_shape_falls_back_to_plain_dedup() -> None:
    odd = QualityWarning("fpe_partial_plaintext_disclosure", "fpe", "c", {"affected_values": "x"})
    assert _fold(odd, odd) == [odd]


def test_a_differing_non_count_field_is_not_fused() -> None:
    a = QualityWarning(
        "fpe_partial_plaintext_disclosure",
        "fpe",
        "c",
        {"affected_values": 1, "total_values": 1, "note": "a"},
    )
    b = QualityWarning(
        "fpe_partial_plaintext_disclosure",
        "fpe",
        "c",
        {"affected_values": 1, "total_values": 1, "note": "b"},
    )
    assert _fold(a, b) == [a, b]


def test_other_codes_dedup_by_equality() -> None:
    w = QualityWarning("top_code_generalized", "top_code", "c", {"over_count": 1})
    assert _fold(w, w) == [w]

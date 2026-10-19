"""OBS-2c: the bounded, value-free row-error summary (plan rev 3 section C, rev 4 R4.2-R4.4,
rev 5 R5.2-R5.3).

Unit tests drive `summarize_row_errors` / `summarize_observed_by_table` with synthetic
records; the pipeline tests drive the full-frame finalizer and both chunked legs, which
stop at the first error-bearing chunk.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError, ValidatorFailedError
from decoy_engine.execution._pipeline_finalize import finalize_validators_and_quarantine
from decoy_engine.execution._row_error_summary import (
    DEFAULT_SAMPLE_LIMIT,
    MAX_SAMPLE_LIMIT,
    RowErrorCompleteness,
    RowErrorSummary,
    chunk_row_error_summary,
    summarize_observed_by_table,
    summarize_row_errors,
)
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.validators._types import ValidationReport, ValidatorFinding

SUMMARY_KEYS = {
    "table",
    "by_column",
    "total_observed",
    "covered",
    "uncovered",
    "sample",
    "sample_limit",
    "omitted",
    "completeness",
}
SAMPLE_KEYS = {"column", "trigger", "row_index", "chunk_index", "chunk_row_offset"}
HOSTILE_REASON = "SECRET-VALUE-543-21-9876 raised ZeroDivisionError in eval('x')"


def _rec(
    table: str = "t", column: str = "c", row: int = 0, trigger: str = "format_error"
) -> RowErrorRecord:
    return RowErrorRecord(
        table=table, column=column, row_index=row, trigger=trigger, reason=HOSTILE_REASON
    )


def _summ(records: list[RowErrorRecord], **kw: Any) -> RowErrorSummary:
    n = len(records)
    args: dict[str, Any] = {
        "table": "t",
        "columns": frozenset({"c", "d"}),
        "completeness": RowErrorCompleteness.COMPLETE,
        "total_observed": n,
        "covered": 0,
        "uncovered": n,
    }
    args.update(kw)
    return summarize_row_errors(records, **args)


def test_frozen_key_sets_and_no_reason() -> None:
    s = _summ([_rec(row=3)], chunk_index=2, chunk_row_offset=40)
    d = s.to_dict()
    assert set(d) == SUMMARY_KEYS
    assert set(d["sample"][0]) == SAMPLE_KEYS
    assert d["sample"][0] == {
        "column": "c",
        "trigger": "format_error",
        "row_index": 3,
        "chunk_index": 2,
        "chunk_row_offset": 40,
    }
    assert d["by_column"] == {"c": {"format_error": 1}}
    assert d["completeness"] == "complete"
    blob = json.dumps(d)
    assert "SECRET" not in blob and "ZeroDivisionError" not in blob and "eval" not in blob


def test_unknown_trigger_maps_to_other() -> None:
    s = _summ([_rec(trigger="boom: SECRET")])
    assert s.by_column == {"c": {"other": 1}}
    assert s.sample[0].trigger == "other"


def test_repeated_chunk_local_positions_are_disambiguated_by_chunk_identity() -> None:
    # Both legs stop at the first error-bearing chunk, so cross-chunk aggregation is a
    # unit fixture: the same local row index in two chunks keeps both identities.
    a = _summ([_rec(row=1)], chunk_index=0, chunk_row_offset=0)
    b = _summ([_rec(row=1)], chunk_index=3, chunk_row_offset=300)
    merged = sorted(a.sample + b.sample, key=lambda s: (s.column, s.chunk_index, s.row_index))
    assert [(s.chunk_index, s.chunk_row_offset, s.row_index) for s in merged] == [
        (0, 0, 1),
        (3, 300, 1),
    ]


def test_truncation_keeps_exact_total_and_counts_omitted() -> None:
    recs = [_rec(row=i) for i in range(10)]
    s = _summ(recs, sample_limit=4)
    assert len(s.sample) == 4 and s.sample_limit == 4
    assert s.omitted == 6
    assert s.total_observed == 10
    assert [x.row_index for x in s.sample] == [0, 1, 2, 3]


def test_sample_order_is_column_chunk_row() -> None:
    recs = [_rec(column="d", row=0), _rec(column="c", row=9), _rec(column="c", row=2)]
    s = _summ(recs)
    assert [(x.column, x.row_index) for x in s.sample] == [("c", 2), ("c", 9), ("d", 0)]


def test_limit_bounds() -> None:
    assert _summ([_rec()]).sample_limit == DEFAULT_SAMPLE_LIMIT == 50
    assert _summ([_rec()], sample_limit=10**9).sample_limit == MAX_SAMPLE_LIMIT == 1000
    for bad in (0, -1):
        with pytest.raises(ValueError):
            _summ([_rec()], sample_limit=bad)


def test_record_outside_table_or_schema_raises() -> None:
    with pytest.raises(ValueError):
        _summ([_rec(table="other")])
    with pytest.raises(ValueError):
        _summ([_rec(column="not_in_schema")])


def test_invariants_enforced() -> None:
    with pytest.raises(ValueError):
        _summ([_rec()], total_observed=5, covered=1, uncovered=1)
    with pytest.raises(ValueError):
        _summ([_rec()], covered=-1, uncovered=2, total_observed=1)


def test_complete_with_zero_records_is_distinct_from_unavailable() -> None:
    complete = _summ([], completeness=RowErrorCompleteness.COMPLETE)
    unavailable = _summ([], completeness=RowErrorCompleteness.UNAVAILABLE)
    assert complete != unavailable
    assert complete.total_observed == 0 and complete.completeness is RowErrorCompleteness.COMPLETE
    assert unavailable.completeness is RowErrorCompleteness.UNAVAILABLE


def test_total_observed_includes_quarantine_covered() -> None:
    recs = [_rec(row=0, trigger="format_error"), _rec(row=1, trigger="mask_error")]
    out = summarize_observed_by_table(
        recs, [recs[1]], columns_by_table={"t": ["c"]}, sample_limit=10
    )
    s = out["t"]
    assert (s.total_observed, s.covered, s.uncovered) == (2, 1, 1)
    assert s.completeness is RowErrorCompleteness.COMPLETE
    assert len(s.sample) == 2


def test_chunk_summary_is_partial_before_abort() -> None:
    s = chunk_row_error_summary(
        [_rec(row=2)], table="t", columns=["c"], chunk_index=1, chunk_row_offset=10
    )
    assert s.completeness is RowErrorCompleteness.PARTIAL_BEFORE_ABORT
    assert (s.total_observed, s.covered, s.uncovered) == (1, 0, 1)
    assert (s.sample[0].chunk_index, s.sample[0].chunk_row_offset) == (1, 10)


# ---- full-frame finalizer -----------------------------------------------------------


def _outputs() -> dict[str, pa.Table]:
    return {
        "a": pa.table({"x": ["1", "2", "3"], "y": ["u", "v", "w"]}),
        "b": pa.table({"z": ["1", "2"]}),
    }


def _finalize(
    records: tuple[RowErrorRecord, ...], config: dict[str, Any], outputs: dict[str, pa.Table]
) -> tuple[Any, dict[str, Any]]:
    qm: dict[str, Any] = {}
    result = finalize_validators_and_quarantine(
        outputs, config=config, caller_sources={}, mask_row_errors=records, quality_metrics=qm
    )
    return result, qm


def test_two_failing_tables_yield_one_summary_each_on_the_error() -> None:
    recs = (
        _rec("a", "x", 0),
        _rec("a", "y", 2, "mask_error"),
        _rec("b", "z", 1),
    )
    with pytest.raises(RowErrorsFailedError) as ei:
        _finalize(recs, {}, _outputs())
    err = ei.value
    assert err.records == recs  # unchanged: the uncovered subset (all of them here)
    sums = err.summaries_by_table
    assert sums is not None and set(sums) == {"a", "b"}
    assert sums["a"].total_observed == 2 and sums["b"].total_observed == 1
    assert sums["a"].by_column == {"x": {"format_error": 1}, "y": {"mask_error": 1}}
    assert all(s.completeness is RowErrorCompleteness.COMPLETE for s in sums.values())
    assert err.summary is None


def test_post_mask_failure_summary_counts_covered_errors_too(tmp_path: Any) -> None:
    covered = _rec("a", "x", 0, "format_error")
    uncovered = _rec("a", "y", 1, "mask_error")
    cfg = {
        "quarantine": {
            "enabled": True,
            "output_path": str(tmp_path / "q.jsonl"),
            "triggers": ["format_error"],
        }
    }
    with pytest.raises(RowErrorsFailedError) as ei:
        _finalize((covered, uncovered), cfg, _outputs())
    err = ei.value
    assert err.records == (uncovered,)
    s = err.summaries_by_table["a"]  # type: ignore[index]
    assert (s.total_observed, s.covered, s.uncovered) == (2, 1, 1)


def test_validator_failure_preempting_row_errors_carries_the_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = ValidationReport(
        passed=False,
        validators_run=("luhn",),
        findings=(
            ValidatorFinding(
                validator="luhn", table="a", column="x", failing_row_indices=(0,), detail="bad"
            ),
        ),
        elapsed_ms=0.0,
    )
    import decoy_engine.validators._registry as registry

    monkeypatch.setattr(registry, "validate", lambda *a, **k: report)
    recs = (_rec("a", "x", 0), _rec("b", "z", 1))
    with pytest.raises(ValidatorFailedError) as ei:
        _finalize(recs, {"validators": [{"type": "luhn"}]}, _outputs())
    sums = ei.value.summaries_by_table
    assert sums is not None and set(sums) == {"a", "b"}
    assert sums["b"].total_observed == 1


def test_validator_failure_without_row_errors_has_no_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = ValidationReport(
        passed=False,
        validators_run=("luhn",),
        findings=(
            ValidatorFinding(
                validator="luhn", table="a", column="x", failing_row_indices=(0,), detail="bad"
            ),
        ),
        elapsed_ms=0.0,
    )
    import decoy_engine.validators._registry as registry

    monkeypatch.setattr(registry, "validate", lambda *a, **k: report)
    with pytest.raises(ValidatorFailedError) as ei:
        _finalize((), {"validators": [{"type": "luhn"}]}, _outputs())
    assert ei.value.summaries_by_table is None


def test_completed_single_table_run_exposes_json_safe_summary(tmp_path: Any) -> None:
    rec = _rec("a", "x", 1, "format_error")
    cfg = {
        "quarantine": {
            "enabled": True,
            "output_path": str(tmp_path / "q.jsonl"),
            "triggers": ["format_error"],
        }
    }
    _, qm = _finalize((rec,), cfg, {"a": _outputs()["a"]})
    d = qm["row_error_summary"]
    assert set(d) == SUMMARY_KEYS
    assert (d["total_observed"], d["covered"], d["uncovered"]) == (1, 1, 0)
    assert "row_error_summary_by_table" not in qm
    blob = json.dumps(qm["row_error_summary"])
    assert "SECRET" not in blob


def test_completed_multi_table_run_exposes_by_table(tmp_path: Any) -> None:
    recs = (_rec("a", "x", 0), _rec("b", "z", 1))
    cfg = {
        "quarantine": {
            "enabled": True,
            "output_path": str(tmp_path / "q.jsonl"),
            "triggers": ["format_error"],
        }
    }
    _, qm = _finalize(recs, cfg, _outputs())
    by_table = qm["row_error_summary_by_table"]
    assert set(by_table) == {"a", "b"}
    for v in by_table.values():
        assert set(v) == SUMMARY_KEYS
        assert "SECRET" not in json.dumps(v)
    assert "row_error_summary" not in qm


def test_clean_run_has_no_summary_key() -> None:
    _, qm = _finalize((), {}, _outputs())
    assert "row_error_summary" not in qm and "row_error_summary_by_table" not in qm


def test_by_column_uses_record_identifiers_not_the_row_errors_count_keys() -> None:
    # A column containing the count-key delimiters would be ambiguous in
    # quality_metrics["row_errors"]; the summary keys it verbatim.
    tricky = "x[format_error]"
    outputs = {"a.b": pa.table({tricky: ["1"]})}
    with pytest.raises(RowErrorsFailedError) as ei:
        _finalize((_rec("a.b", tricky, 0),), {}, outputs)
    s = ei.value.summaries_by_table["a.b"]  # type: ignore[index]
    assert s.by_column == {tricky: {"format_error": 1}}

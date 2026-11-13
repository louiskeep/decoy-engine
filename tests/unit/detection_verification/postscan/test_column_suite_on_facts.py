"""The runner computes residual PII and policy validation on column facts and gets what the
resident frame checks get, whatever the batch layout."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.fileaccess import FileAccessReadError
from decoy_engine.verification.postmask import (
    TableBatches,
    from_table,
    run_post_mask,
    run_post_mask_tables,
)
from decoy_engine.verification.postmask import _column_facts as column_facts_module
from decoy_engine.verification.postmask._column_facts import column_facts
from decoy_engine.verification.postmask.policy_validation import (
    check_policy_validation,
    policy_findings,
)
from decoy_engine.verification.postmask.residual_pii import (
    check_residual_pii,
    residual_pii_findings,
)

CANARY = "CANARY-7f3a"
LIMIT = "256MB"
EMAILS = [f"user{i}@realmail.com" for i in range(100)]
FAKE = [f"fake{i}@masked.example" for i in range(100)]


def _cfg(*columns: tuple[str, str]) -> dict[str, Any]:
    return {
        "version": 2,
        "tables": [{"name": "t", "columns": [{"name": c, "strategy": s} for c, s in columns]}],
    }


def _small_batches(table: TableBatches, size: int) -> TableBatches:
    """The same table, read in batches of `size` rows."""

    def read(columns: Sequence[str], _batch_rows: int) -> Iterator[pa.RecordBatch]:
        return table.read(columns, size)

    return replace(table, read=read)


def _facts(src: pd.DataFrame | None, out: pd.DataFrame, size: int, scratch: Path):
    src_t = (
        None if src is None else _small_batches(from_table("t", pa.Table.from_pandas(src)), size)
    )
    out_t = _small_batches(from_table("t", pa.Table.from_pandas(out)), size)
    facts = {
        "t": column_facts(src_t, out_t, out_t.schema.names, scratch=scratch, memory_limit=LIMIT)
    }
    return {"t": src_t} if src_t is not None else {}, {"t": out_t}, facts


# (source, output, config): every shape the decision tables branch on.
FIXTURES = {
    "identical_faker": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": EMAILS}),
        _cfg(("email", "faker")),
    ),
    "partly_identical_faker": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": EMAILS[:8] + FAKE[8:]}),
        _cfg(("email", "faker")),
    ),
    "synthetic_faker": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": FAKE}),
        _cfg(("email", "faker")),
    ),
    "unconfigured": (pd.DataFrame({"email": EMAILS}), pd.DataFrame({"email": EMAILS}), _cfg()),
    "passthrough": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": EMAILS}),
        _cfg(("email", "passthrough")),
    ),
    "hash_survivor": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": FAKE}),
        _cfg(("email", "hash")),
    ),
    "shuffle_full_identity": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": EMAILS}),
        _cfg(("email", "shuffle")),
    ),
    "constant_source": (
        pd.DataFrame({"email": ["a@b.com"] * 60}),
        pd.DataFrame({"email": ["a@b.com"] * 60}),
        _cfg(("email", "faker")),
    ),
    "nulls": (
        pd.DataFrame({"email": EMAILS[:50] + [None] * 50}),
        pd.DataFrame({"email": [None] * 10 + EMAILS[10:50] + [None] * 50}),
        _cfg(("email", "faker")),
    ),
    "row_count_mismatch": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": EMAILS[:60]}),
        _cfg(("email", "faker")),
    ),
    "configured_column_missing": (
        pd.DataFrame({"email": EMAILS}),
        pd.DataFrame({"email": EMAILS}),
        _cfg(("email", "faker"), ("ghost", "hash")),
    ),
    "plain_noop_mask": (
        pd.DataFrame({"code": [f"c{i}" for i in range(40)]}),
        pd.DataFrame({"code": [f"c{i}" for i in range(40)]}),
        _cfg(("code", "hash")),
    ),
}


@pytest.mark.parametrize("size", [7, 1000])
@pytest.mark.parametrize("name", list(FIXTURES))
def test_the_facts_checks_equal_the_resident_frame_checks(name: str, size: int, tmp_path: Path):
    src, out, config = FIXTURES[name]
    src_t, out_t, facts = _facts(src, out, size, tmp_path)
    assert residual_pii_findings(facts, config) == check_residual_pii(
        {"t": out}, config, source_frames={"t": src}
    )
    assert policy_findings(config, src_t, out_t, facts) == check_policy_validation(
        {"t": src}, {"t": out}, config
    )


@pytest.mark.parametrize("name", list(FIXTURES))
def test_run_post_mask_reports_what_the_frame_checks_report(name: str):
    src, out, config = FIXTURES[name]
    report = run_post_mask({"t": src}, {"t": out}, config=config)
    residual = check_residual_pii({"t": out}, config, source_frames={"t": src})
    policy = check_policy_validation({"t": src}, {"t": out}, config)
    assert [f["severity"] for f in report["residual_pii"]] == [f.severity for f in residual]
    assert [f["message"] for f in report["residual_pii"]] == [f.message for f in residual]
    assert [f["severity"] for f in report["policy_validation"]] == [f.severity for f in policy]
    assert [f["message"] for f in report["policy_validation"]] == [f.message for f in policy]


def test_a_table_without_a_source_gets_the_detector_findings_only(tmp_path: Path):
    _src, out, config = FIXTURES["unconfigured"]
    _s, _o, facts = _facts(None, out, 7, tmp_path)
    (finding,) = residual_pii_findings(facts, config)
    assert finding.source_compared is False and finding.severity == "warning"
    assert (
        policy_findings(config, {}, {"t": from_table("t", pa.Table.from_pandas(out))}, facts) == []
    )


def test_a_column_near_min_rate_escalates_as_on_the_resident_path(tmp_path: Path):
    # 6 of 100 rows keep their source value: the 5% floor warns on a substitution strategy.
    out = EMAILS[:6] + FAKE[6:]
    src_df, out_df = pd.DataFrame({"email": EMAILS}), pd.DataFrame({"email": out})
    config = _cfg(("email", "faker"))
    src_t, out_t, facts = _facts(src_df, out_df, 7, tmp_path)
    (finding,) = residual_pii_findings(facts, config)
    assert finding.severity == "warning"
    assert finding.source_identity_rate == 0.06
    assert [finding] == check_residual_pii({"t": out_df}, config, source_frames={"t": src_df})
    # A detector rate near its floor is a sum over batches, not an average of them.
    assert policy_findings(config, src_t, out_t, facts)[0].severity == "info"


def test_an_integer_column_holding_a_null_is_read_as_whole_numbers(tmp_path: Path):
    """The one named difference from the old runner: it read such a column as floats ("10005.0"),
    so a digit detector never matched. From Arrow the values stay whole numbers and match."""
    zips = [10000 + i for i in range(20)]
    out = pa.table({"zip": pa.array([None, *zips[1:]], pa.int64())})
    src = pa.table({"zip": pa.array(zips, pa.int64())})
    config = _cfg(("zip", "faker"))
    src_t, out_t = from_table("t", src), from_table("t", out)
    facts = {"t": column_facts(src_t, out_t, ["zip"], scratch=tmp_path, memory_limit=LIMIT)}
    moved = residual_pii_findings(facts, config)
    old = check_residual_pii({"t": out.to_pandas()}, config, source_frames={"t": src.to_pandas()})
    assert [f.detector_id for f in moved] == ["us_zip"]
    assert old == []


def test_the_runner_tables_entry_equals_the_frame_entry(tmp_path: Path):
    src, out, config = FIXTURES["partly_identical_faker"]
    from_frames = run_post_mask({"t": src}, {"t": out}, config=config)
    from_tables = run_post_mask_tables(
        {"t": from_table("t", pa.Table.from_pandas(src))},
        {"t": from_table("t", pa.Table.from_pandas(out))},
        config=config,
        keys=(),
        scratch=tmp_path,
        memory_limit=LIMIT,
    )
    for report in (from_frames, from_tables):
        report.pop("generated_at")
    assert from_frames == from_tables


def test_a_coded_error_in_the_facts_pass_propagates_as_the_same_object(
    monkeypatch: pytest.MonkeyPatch,
):
    error = FileAccessReadError("sources.t.path")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(column_facts_module, "column_distinct", boom)
    src, out, config = FIXTURES["synthetic_faker"]
    with pytest.raises(FileAccessReadError) as info:
        run_post_mask({"t": src}, {"t": out}, config=config)
    assert info.value is error


def test_no_value_reaches_the_report_or_a_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    hits = [f"{CANARY}{i}@realmail.com" for i in range(40)]
    src, out = pd.DataFrame({"email": hits}), pd.DataFrame({"email": hits})
    config = _cfg(("email", "faker"))
    with caplog.at_level(logging.DEBUG):
        report = run_post_mask({"t": src}, {"t": out}, config=config)
    assert report["residual_pii"] and CANARY not in repr(report) and CANARY not in caplog.text

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"duckdb failed on {CANARY}")

    monkeypatch.setattr(column_facts_module, "column_distinct", boom)
    with caplog.at_level(logging.DEBUG):
        report = run_post_mask({"t": src}, {"t": out}, config=config)
    assert [f["severity"] for f in report["residual_pii"]] == ["error"]
    assert [f["severity"] for f in report["policy_validation"]] == ["error"]
    assert "RuntimeError" in report["residual_pii"][0]["message"]
    assert CANARY not in repr(report) and CANARY not in caplog.text

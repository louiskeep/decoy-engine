"""The runner computes residual PII and policy validation on column facts and gets what the
resident frame checks get, whatever the batch layout."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict, replace
from decimal import Decimal
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
from decoy_engine.verification.postmask import runner as runner_module
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

    monkeypatch.setattr(runner_module, "column_facts", boom)
    with caplog.at_level(logging.DEBUG):
        report = run_post_mask({"t": src}, {"t": out}, config=config)
    assert [(f["table"], f["severity"]) for f in report["residual_pii"]] == [("t", "error")]
    assert [f["severity"] for f in report["policy_validation"]] == ["error"]
    assert "RuntimeError" in report["residual_pii"][0]["message"]
    assert CANARY not in repr(report) and CANARY not in caplog.text


SSN = [f"{100 + i:03d}-45-{6000 + i}" for i in range(30)]


def _oracle(src: dict[str, pd.DataFrame], out: dict[str, pd.DataFrame], config: dict[str, Any]):
    return (
        [asdict(f) for f in check_residual_pii(out, config, source_frames=src)],
        [asdict(f) for f in check_policy_validation(src, out, config)],
    )


def _parity(src: dict[str, pd.DataFrame], out: dict[str, pd.DataFrame], config: dict[str, Any]):
    report = run_post_mask(src, out, config=config)
    residual, policy = _oracle(src, out, config)
    assert report["residual_pii"] == residual
    assert report["policy_validation"] == policy
    return report


def _two_tables(bad: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    clean = pd.DataFrame({"email": EMAILS[:40], "ssn": SSN + [None] * 10})
    frames = {"bad": bad, "clean": clean.iloc[:40].reset_index(drop=True)}
    config = {
        "version": 2,
        "tables": [
            {"name": "bad", "columns": [{"name": "ssn", "strategy": "faker"}]},
            {"name": "clean", "columns": [{"name": "email", "strategy": "faker"}]},
        ],
    }
    return frames, config


BAD_COLUMNS = {
    "mixed_int_and_str": [10001, "10002", 10003, "x"] * 7 + [10005, 10006],
    "decimal_with_float": [Decimal("1.5"), 2.5, Decimal("3.5"), 4.5] * 7 + [1.0, 2.0],
    "set_cells": [{1, 2}, {3}] * 15,
}


@pytest.mark.parametrize("name", list(BAD_COLUMNS))
def test_one_unconvertible_column_leaves_every_other_finding_as_on_the_frame_checks(name: str):
    bad = pd.DataFrame({"ssn": SSN, "odd": BAD_COLUMNS[name]})
    frames, config = _two_tables(bad)
    report = _parity(frames, frames, config)
    # The unmasked ssn and the clean table's email are both reported, not an error row.
    assert {(f["table"], f["column"], f["severity"]) for f in report["residual_pii"]} >= {
        ("bad", "ssn", "fail"),
        ("clean", "email", "fail"),
    }
    assert "error" not in {f["severity"] for f in report["residual_pii"]}


def test_a_zip_column_of_mixed_int_and_str_is_compared_as_on_the_frame_checks():
    zips = [10000 + i if i % 3 else str(10000 + i) for i in range(60)]
    frame = pd.DataFrame({"email": EMAILS[:60], "zip": zips})
    config = _cfg(("email", "faker"), ("zip", "faker"))
    report = _parity({"t": frame}, {"t": frame}, config)
    assert ("zip", "fail") in {(f["column"], f["severity"]) for f in report["residual_pii"]}


@pytest.mark.parametrize(
    "cells",
    [
        pa.array([[1, 2], [3]] * 15, pa.list_(pa.int64())),
        pa.array([[("a", 1)], [("b", 2)]] * 15, pa.map_(pa.string(), pa.int64())),
    ],
    ids=["list", "map"],
)
def test_an_unconfigured_list_or_map_column_leaves_the_other_columns_findings(cells: pa.Array):
    frame = pa.table({"ssn": SSN, "cells": cells}).to_pandas()
    config = _cfg()
    report = _parity({"t": frame}, {"t": frame}, config)
    assert ("ssn", "fail") in {(f["column"], f["severity"]) for f in report["residual_pii"]}


def test_a_configured_list_column_gets_its_own_policy_error_only():
    frame = pa.table(
        {"ssn": SSN, "cells": pa.array([[1, 2], [3]] * 15, pa.list_(pa.int64()))}
    ).to_pandas()
    config = _cfg(("ssn", "faker"), ("cells", "hash"))
    report = run_post_mask({"t": frame}, {"t": frame}, config=config)
    by_column = {f["column"]: f for f in report["policy_validation"]}
    assert by_column["cells"]["severity"] == "error"
    assert "ValueError" in by_column["cells"]["message"]
    assert by_column["ssn"]["severity"] == "fail"
    assert ("ssn", "fail") in {(f["column"], f["severity"]) for f in report["residual_pii"]}


def test_a_table_whose_facts_fail_does_not_fail_the_other_table(monkeypatch: pytest.MonkeyPatch):
    frames, config = _two_tables(pd.DataFrame({"ssn": SSN}))
    real = runner_module.column_facts

    def facts(source: Any, output: Any, *args: Any, **kwargs: Any) -> Any:
        if output.name == "bad":
            raise RuntimeError(f"broke on {CANARY}")
        return real(source, output, *args, **kwargs)

    monkeypatch.setattr(runner_module, "column_facts", facts)
    report = run_post_mask(frames, frames, config=config)
    fails = {(f["table"], f["column"]) for f in report["residual_pii"] if f["severity"] == "fail"}
    assert ("clean", "email") in fails and {t for t, _ in fails} == {"clean"}
    errors = [f for f in report["residual_pii"] if f["severity"] == "error"]
    assert [f["table"] for f in errors] == ["bad"] and "RuntimeError" in errors[0]["message"]
    assert [(f["table"], f["severity"]) for f in report["policy_validation"]] == [
        ("bad", "error"),
        ("clean", "fail"),
    ]
    assert CANARY not in repr(report)


def test_a_mixed_dtype_two_table_run_equals_the_frame_checks():
    n = 60
    src = {
        "a": pd.DataFrame(
            {
                "email": EMAILS[:n],
                "age": pd.array(list(range(n)), "Int64"),
                "score": [float(i) if i % 7 else float("nan") for i in range(n)],
                "flag": [bool(i % 2) for i in range(n)],
                "seen": pd.date_range("2026-01-01", periods=n, tz="UTC"),
                "tier": pd.Categorical([f"t{i % 3}" for i in range(n)]),
            }
        ),
        "b": pd.DataFrame({"code": [f"c{i}" for i in range(n)], "n": list(range(n))}),
    }
    out = {k: v.copy() for k, v in src.items()}
    out["a"]["email"] = FAKE[:n]
    out["b"]["n"] = [i + 1 for i in range(n)]
    config = {
        "version": 2,
        "tables": [
            {"name": "a", "columns": [{"name": c, "strategy": "faker"} for c in src["a"].columns]},
            {"name": "b", "columns": [{"name": c, "strategy": "hash"} for c in src["b"].columns]},
        ],
    }
    _parity(src, out, config)


def test_run_post_mask_tables_refuses_a_memory_limit_above_the_cap(tmp_path: Path):
    t = {"t": from_table("t", pa.table({"c": ["a", "b"]}))}
    for limit in ("513MB", "2GB"):
        with pytest.raises(ValueError):
            run_post_mask_tables(t, t, config=_cfg(), keys=(), scratch=tmp_path, memory_limit=limit)
    run_post_mask_tables(t, t, config=_cfg(), keys=(), scratch=tmp_path, memory_limit="512MB")


def test_a_failed_source_comparison_is_an_error_finding_not_a_quiet_downgrade(
    monkeypatch: pytest.MonkeyPatch,
):
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"compare failed on {CANARY}")

    monkeypatch.setattr(column_facts_module, "_add_identity", boom)
    frame = pd.DataFrame({"email": EMAILS})
    report = run_post_mask({"t": frame}, {"t": frame}, config=_cfg(("email", "faker")))
    (finding,) = report["residual_pii"]
    assert finding["severity"] == "error" and finding["source_compared"] is False
    assert "RuntimeError" in finding["message"] and CANARY not in repr(report)


def test_a_failed_distinct_count_keeps_the_valid_identity_rate(monkeypatch: pytest.MonkeyPatch):
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("distinct failed")

    monkeypatch.setattr(column_facts_module, "column_distinct", boom)
    frame = pd.DataFrame({"email": EMAILS})
    report = run_post_mask({"t": frame}, {"t": frame}, config=_cfg(("email", "faker")))
    (finding,) = report["residual_pii"]
    assert finding["severity"] == "fail" and finding["source_compared"] is True
    assert finding["source_identity_rate"] == 1.0


def test_a_column_converted_natively_on_one_side_only_still_compares_equal_values():
    zips = [10000 + i for i in range(60)]
    src = pd.DataFrame({"zip": zips})
    out = pd.DataFrame({"zip": pd.Series([*zips[:-1], "x"], dtype=object)})
    report = _parity({"t": src}, {"t": out}, _cfg(("zip", "faker")))
    (finding,) = report["residual_pii"]
    assert finding["severity"] == "fail" and finding["source_identity_rate"] > 0.9

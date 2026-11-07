"""ip_prefix on every route: same output where it runs, a coded decline where it cannot."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.disguises.loader import load_disguises
from decoy_engine.errors import ConfigError, RowErrorsFailedError
from decoy_engine.execution import run_mask_pipeline_chunked
from decoy_engine.execution._strategies import strategy_catalog
from decoy_engine.execution.native._chunked_entry import run_mask_chunked
from decoy_engine.execution.native._requirements import NATIVE_KERNEL_STRATEGIES
from decoy_engine.execution.out_of_core._compat import _SUPPORTED_WORK_STRATEGIES
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profiling.recommendations import DEFAULT_STRATEGY_BY_DETECTOR
from decoy_engine.transforms.text_mask import DETECTOR_DEFAULTS
from tests.unit.transforms.ip_prefix import _support as s

_IPS = [f"10.1.{i // 256}.{i % 256}" for i in range(1200)] + ["2001:db8::1", "::ffff:10.1.0.5"]


def _single(tmp_path: Path, **kwargs: Any) -> Any:
    return s.pipeline(tmp_path, {"t": pa.table({"ip": _IPS})}, {"t": [s.column()]}, **kwargs)


def test_full_frame_and_auto_agree(tmp_path: Path) -> None:
    full = _single(tmp_path / "a", execution_mode="full_frame")
    auto = _single(tmp_path / "b")
    out = full.outputs["t"].column("ip").to_pylist()
    assert out == auto.outputs["t"].column("ip").to_pylist()
    assert all(o.startswith("10.1.") for o in out[:1200])
    assert len(set(out)) == len(_IPS)
    assert out[-1] == "::ffff:" + out[5]


def test_auto_chunk_declines_with_its_coded_reason_and_runs_full_frame(tmp_path: Path) -> None:
    chunked = _single(
        tmp_path / "a", auto_chunk=True, auto_chunk_threshold_rows=100, chunk_size_rows=300
    )
    evidence = chunked.quality_metrics["auto_chunk"]
    assert evidence["mode"] == "full_frame"
    assert evidence["reason"].startswith("strategy_not_chunk_safe")
    full = _single(tmp_path / "b", execution_mode="full_frame")
    assert chunked.outputs["t"].equals(full.outputs["t"])


@pytest.mark.parametrize("entry", [run_mask_chunked, run_mask_pipeline_chunked])
def test_the_chunked_entry_points_refuse_with_their_coded_reason(
    entry: Any, tmp_path: Path
) -> None:
    table = pa.table({"ip": _IPS[:10]})
    cfg = s.config(("t", [s.column()]))
    cfg.update(s.write_sources(tmp_path, {"t": table}))
    with pytest.raises(PlanCompileError) as caught:
        list(
            entry(
                cfg,
                [table],
                table="t",
                engine_version=s.ENGINE_VERSION,
                key_provider=s.KEY_PROVIDER,
            )
        )
    assert caught.value.code == "strategy_not_chunk_safe"


def test_no_native_or_out_of_core_kernel_admits_it() -> None:
    assert "ip_prefix" not in NATIVE_KERNEL_STRATEGIES
    assert "ip_prefix" not in _SUPPORTED_WORK_STRATEGIES


def _fk_job(tmp_path: Path, bad: str | None = None, **kwargs: Any) -> Any:
    ids = [f"p{i}" for i in range(8)]
    ips = [f"192.0.2.{i * 9}" for i in range(8)]
    if bad is not None:
        ips[5] = bad
    parent = pa.table({"id": ids, "ip": ips})
    child = pa.table({"id": [f"c{i}" for i in range(8)], "parent_id": ids, "ip": ips})
    key = {"name": "id", "strategy": "hash", "namespace": "pk"}
    cols = {"parent": [key, s.column()], "child": [dict(key, name="parent_id"), s.column()]}
    rel = {
        "parent": {"table": "parent", "columns": ["id"]},
        "children": [{"table": "child", "columns": ["parent_id"]}],
        "orphan_policy": "preserve",
        "namespace": "pk",
    }
    extra = {"relationships": [rel], **kwargs.pop("extra", {})}
    return s.pipeline(
        tmp_path,
        {"parent": parent, "child": child},
        cols,
        extra=extra,
        use_byte_estimate_routing=False,
        **kwargs,
    )


def test_sequential_and_full_frame_agree_and_shared_namespace_columns_join(tmp_path: Path) -> None:
    seq = _fk_job(tmp_path / "a")
    full = _fk_job(tmp_path / "b", execution_mode="full_frame")
    assert seq.quality_metrics["execution"]["execution_mode"] == "sequential"
    for table in ("parent", "child"):
        assert seq.outputs[table].column("ip").equals(full.outputs[table].column("ip"))
    assert seq.outputs["parent"].column("ip").equals(seq.outputs["child"].column("ip"))


def test_out_of_core_refuses_with_the_row_error_code(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        _fk_job(tmp_path, execution_mode="out_of_core")
    assert "out_of_core_row_error_strategy_unsupported" in str(caught.value)


@pytest.mark.parametrize("mode", ["full_frame", "auto"])
def test_a_malformed_cell_fails_the_run_or_is_quarantined(mode: str, tmp_path: Path) -> None:
    with pytest.raises(RowErrorsFailedError) as caught:
        _fk_job(tmp_path / "fail", bad=s.SENTINEL_BAD, execution_mode=mode)
    # The sequential route stops at the first table with a row error; full-frame sees both.
    failed = {(r.table, r.row_index) for r in caught.value.records}
    assert ("parent", 5) in failed and failed <= {("parent", 5), ("child", 5)}
    assert s.SENTINEL_BAD not in str(caught.value)

    qpath = tmp_path / "q.jsonl"
    quarantine = {"enabled": True, "output_path": str(qpath), "triggers": ["format_error"]}
    result = _fk_job(
        tmp_path / "ok", bad=s.SENTINEL_BAD, execution_mode=mode, extra={"quarantine": quarantine}
    )
    for table in ("parent", "child"):
        out = result.outputs[table].column("ip").to_pylist()
        assert s.SENTINEL_BAD not in out and len(out) == 7
    records = [json.loads(line) for line in qpath.read_text().splitlines()]
    assert {r["_quarantine_trigger"] for r in records} == {"format_error"}


def test_post_validation_passes_on_a_whole_slash24(tmp_path: Path) -> None:
    # Every output is some row's source address, which a set-membership leak check would flag.
    ips = [f"192.0.2.{i}" for i in range(256)]
    result = s.pipeline(
        tmp_path,
        {"t": pa.table({"ip": ips})},
        {"t": [s.column()]},
        post_validation=True,
        post_validation_enforce=True,
    )
    out = result.outputs["t"].column("ip").to_pylist()
    assert sorted(out) == sorted(ips)
    assert out != ips
    summary = result.quality_metrics["quality_summary"]
    assert summary["failed_checks"] == ()
    assert {w["code"] for w in summary["quality_warnings"]} <= {"value_reuse_fixed_point"}


def test_no_value_reaches_messages_warnings_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RowErrorsFailedError) as caught:
        s.pipeline(
            tmp_path,
            {"t": pa.table({"ip": [s.SENTINEL_V4, s.SENTINEL_BAD]})},
            {"t": [s.column()]},
            execution_mode="full_frame",
        )
    rendered = [str(caught.value), repr(caught.value.records), caplog.text]
    for text in rendered:
        assert s.SENTINEL_V4 not in text
        assert s.SENTINEL_BAD not in text
    ok = s.pipeline(tmp_path / "ok", {"t": pa.table({"ip": [s.SENTINEL_V4]})}, {"t": [s.column()]})
    assert s.SENTINEL_V4 not in repr(ok.warnings) + json.dumps(ok.quality_metrics, default=str)


def test_dark_no_default_names_it() -> None:
    assert all(strategy != "ip_prefix" for strategy, _ in DEFAULT_STRATEGY_BY_DETECTOR.values())
    assert "ip_prefix" not in DETECTOR_DEFAULTS.values()
    for disguise in load_disguises():
        assert "ip_prefix" not in disguise.model_dump_json()
    (row,) = [r for r in strategy_catalog() if r.name == "ip_prefix"]
    assert row.offer_in_wizard is False
    assert row.namespace_required is True

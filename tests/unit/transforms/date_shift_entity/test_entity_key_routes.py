"""entity_key through real jobs: one offset per entity on every route, or a coded decline."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution.native._operator_config_rejections import date_shift_config_rejection
from tests.unit.transforms.date_shift_entity._support import (
    ENGINE_VERSION,
    NS,
    config,
    date_col,
    passthrough,
    run,
)

N = 41  # 5 chunks of 7 plus a partial one


def _days(src: str, out: str) -> int:
    return (dt.date.fromisoformat(out) - dt.date.fromisoformat(src)).days


def _admissions() -> pa.Table:
    return pa.table(
        {
            # Patients repeat every 3 rows, so a chunk of 7 splits most of them; every tenth
            # row has no patient and every fourteenth a blank one.
            "pid": [
                None if i % 10 == 0 else ("  " if i % 14 == 0 else f"p{i // 3}") for i in range(N)
            ],
            "admitted": [f"2020-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(N)],
            "discharged": [f"2020-{1 + i % 12:02d}-{2 + i % 27:02d}" for i in range(N)],
        }
    )


def _admissions_cfg(tmp: Path) -> dict[str, Any]:
    return config(
        tmp,
        {
            "t": [
                {"name": "pid", "strategy": "hash", "namespace": "pid_ns"},
                date_col("admitted", entity_key="pid", min_days=-200, max_days=200),
                date_col("discharged", entity_key="pid", min_days=-200, max_days=200),
            ]
        },
    )


def test_length_of_stay_and_per_patient_offset(tmp_path: Path) -> None:
    src = _admissions()
    out = run(tmp_path, _admissions_cfg(tmp_path), {"t": src}).outputs["t"].to_pydict()
    offsets: dict[str, int] = {}
    for i, pid in enumerate(src.column("pid").to_pylist()):
        a = _days(src["admitted"][i].as_py(), out["admitted"][i])
        d = _days(src["discharged"][i].as_py(), out["discharged"][i])
        if pid is None or not pid.strip():
            continue
        assert a == d, "admission and discharge of one row move together"
        assert offsets.setdefault(pid, a) == a, "every row of one patient moves together"
    assert len(set(offsets.values())) > 1


def test_full_frame_auto_chunk_and_chunked_entry_are_identical(tmp_path: Path) -> None:
    src = _admissions()
    cfg = _admissions_cfg(tmp_path)
    full = run(tmp_path, cfg, {"t": src}, execution_mode="full_frame")
    auto = run(tmp_path, cfg, {"t": src}, auto_chunk_threshold_rows=10, chunk_size_rows=7)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert auto.outputs["t"].equals(full.outputs["t"])
    assert [str(w) for w in auto.warnings] == [str(w) for w in full.warnings]

    want = full.quality_metrics["date_shift_entity_fallback"]
    assert want == [
        {"table": "t", "column": "admitted", "fallback_rows": 7},
        {"table": "t", "column": "discharged", "fallback_rows": 7},
    ]
    chunked = {
        c["column"]: c["entity_fallback_rows"]
        for c in auto.quality_metrics["chunked_route"]["columns"]
        if "entity_fallback_rows" in c
    }
    assert chunked == {e["column"]: e["fallback_rows"] for e in want}

    assert auto.quality_metrics["date_shift_entity_fallback"] == want
    legacy = run(
        tmp_path,
        cfg,
        {"t": src},
        auto_chunk_threshold_rows=10,
        chunk_size_rows=7,
        chunked_dispatcher_enabled=False,
    )
    assert legacy.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert legacy.outputs["t"].equals(full.outputs["t"])
    assert legacy.quality_metrics["date_shift_entity_fallback"] == want

    for size in (1, 7, 33):
        chunks = [src.slice(i, size) for i in range(0, N, size)]
        got = [
            row
            for part in run_mask_pipeline_chunked(
                cfg, chunks, table="t", engine_version=ENGINE_VERSION
            )
            for row in part.to_pylist()
        ]
        assert got == full.outputs["t"].to_pylist(), size


def _fk_job(tmp: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    tables = {
        "patients": [
            {"name": "id", "strategy": "hash", "namespace": "pid_ns"},
            date_col("born", entity_key="id"),
        ],
        "encounters": [
            {"name": "patient_id", "strategy": "hash", "namespace": "pid_ns"},
            date_col("admit", entity_key="patient_id"),
        ],
    }
    rel = [
        {
            "parent": {"table": "patients", "columns": ["id"]},
            "children": [{"table": "encounters", "columns": ["patient_id"]}],
            "orphan_policy": "remap",
            "namespace": "pid_ns",
        }
    ]
    sources = {
        "patients": pa.table(
            {
                "id": pa.array([1, 2, 3, 4], pa.int64()),
                "born": ["1980-01-01", "1981-02-02", "1982-03-03", "1983-04-04"],
            }
        ),
        "encounters": pa.table(
            {
                "patient_id": pa.array([1, 1, 2, 3, 3, 3, 4, 2], pa.int64()),
                "admit": [f"2020-0{1 + i}-1{i}" for i in range(8)],
            }
        ),
    }
    return config(tmp, tables, relationships=rel), sources


def test_sequential_equals_full_frame_and_tables_share_the_offset(tmp_path: Path) -> None:
    cfg, sources = _fk_job(tmp_path)
    full = run(tmp_path, cfg, sources, execution_mode="full_frame")
    seq = run(tmp_path, cfg, sources, execution_mode="sequential")
    assert seq.quality_metrics["execution"]["execution_mode"] == "sequential"
    for name in sources:
        assert seq.outputs[name].equals(full.outputs[name]), name
    born = {
        pid: _days(
            sources["patients"]["born"][i].as_py(), seq.outputs["patients"]["born"][i].as_py()
        )
        for i, pid in enumerate(sources["patients"]["id"].to_pylist())
    }
    for i, pid in enumerate(sources["encounters"]["patient_id"].to_pylist()):
        src = sources["encounters"]["admit"][i].as_py()
        assert _days(src, seq.outputs["encounters"]["admit"][i].as_py()) == born[pid]


def test_out_of_core_declines_with_its_existing_code(tmp_path: Path) -> None:
    cfg, sources = _fk_job(tmp_path)
    with pytest.raises(ConfigError, match="out_of_core_row_error_strategy_unsupported"):
        run(tmp_path, cfg, sources, execution_mode="out_of_core")
    fell_back = run(tmp_path, cfg, sources, out_of_core_threshold_rows=1)
    oracle = run(tmp_path, cfg, sources, execution_mode="full_frame")
    for name in sources:
        assert fell_back.outputs[name].equals(oracle.outputs[name])


def test_keys_of_different_storage_types_share_one_offset(tmp_path: Path) -> None:
    tables = {
        name: [passthrough("k"), date_col("d", entity_key="k")]
        for name in ("ints", "strings", "decimals", "decimals_scaled", "nullable_ints")
    }
    day = ["2020-06-01"]
    sources = {
        "ints": pa.table({"k": pa.array([123], pa.int64()), "d": day}),
        "strings": pa.table({"k": ["123"], "d": day}),
        "decimals": pa.table({"k": pa.array([Decimal("123")], pa.decimal128(10, 0)), "d": day}),
        "decimals_scaled": pa.table(
            {"k": pa.array([Decimal("123.0")], pa.decimal128(10, 1)), "d": day}
        ),
        "nullable_ints": pa.table(
            {"k": pa.array([123, None], pa.int64()), "d": [*day, "2020-06-01"]}
        ),
    }
    outs = run(tmp_path, config(tmp_path, tables), sources).outputs
    first = {name: outs[name]["d"][0].as_py() for name in sources}
    assert len(set(first.values())) == 1, first


def test_published_hash_and_group_key_do_not_reveal_the_offset(tmp_path: Path) -> None:
    """The V1 group_by probe, rerun with entity_key: the hash cell no longer gives the date."""
    cols: list[dict[str, Any]] = [
        {"name": "pid", "strategy": "hash", "namespace": NS},
        {
            "name": "fam",
            "strategy": "group_key",
            "namespace": NS,
            "provider_config": {"group_by": "pid", "length": 64},
        },
        date_col("d", entity_key="pid", min_days=-365, max_days=365),
    ]
    src = pa.table(
        {
            "pid": [f"p{i}" for i in range(12)],
            "fam": ["x"] * 12,
            "d": [f"2020-0{1 + i % 9}-1{i % 9}" for i in range(12)],
        }
    )
    out = run(tmp_path, config(tmp_path, {"t": cols}), {"t": src}).outputs["t"].to_pydict()
    hits = 0
    for i in range(12):
        true = _days(src["d"][i].as_py(), out["d"][i])
        for published in (out["pid"][i], out["fam"][i]):
            digest = bytes.fromhex(published[:64])
            if -365 + int.from_bytes(digest[:8], "big") % 731 == true:
                hits += 1
    assert hits == 0


def test_two_namespaces_give_different_offsets(tmp_path: Path) -> None:
    tables = {
        "a": [passthrough("pid"), date_col("d", entity_key="pid")],
        "b": [passthrough("pid"), date_col("d", entity_key="pid", namespace="other_dates")],
    }
    src = pa.table({"pid": [f"p{i}" for i in range(10)], "d": ["2020-01-01"] * 10})
    out = run(tmp_path, config(tmp_path, tables), {"a": src, "b": src}).outputs
    assert out["a"]["d"].to_pylist() != out["b"]["d"].to_pylist()


@pytest.mark.parametrize("value", ["pid", 123, "", False, "  "])
def test_native_gate_declines_on_presence(value: object) -> None:
    cfg = {"date_format": "%Y-%m-%d", "entity_key": value}
    assert date_shift_config_rejection("d", "t", None, namespace=NS, provider_config=cfg) == (
        "date_shift_entity_key_not_native:d"
    )


def test_native_gate_unchanged_without_entity_key() -> None:
    base = {"date_format": "%Y-%m-%d"}
    gate = date_shift_config_rejection
    assert gate("d", "t", None, namespace=NS, provider_config={**base, "entity_key": None}) is None
    assert gate("d", "t", None, namespace=NS, provider_config=base) is None
    assert gate("d", "t", None, namespace=NS, provider_config={**base, "group_by": "g"}) == (
        "date_shift_group_by_not_native:d"
    )


def test_mixed_generate_and_mask_job_equals_the_oracle(tmp_path: Path) -> None:
    from tests.parity._f2_mixed_support import (
        CHILD,
        FK,
        assert_outputs_identical,
        mixed_config,
        oracle,
    )
    from tests.parity._f2_mixed_support import run as run_mixed

    child = pa.table(
        {
            FK: pa.array([i // 3 for i in range(N)], pa.int64()),
            "seen": [f"2021-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(N)],
        }
    )
    cfg = mixed_config(
        tmp_path,
        child,
        child_columns=[date_col("seen", entity_key=FK, min_days=-60, max_days=60)],
        coupled=False,
    )
    got = run_mixed(cfg, {CHILD: child})
    want = oracle(cfg, {CHILD: child})
    assert_outputs_identical(got.outputs, want.outputs)
    out = got.outputs[CHILD].to_pydict()
    offsets: dict[int, int] = {}
    for i, key in enumerate(child[FK].to_pylist()):
        delta = _days(child["seen"][i].as_py(), out["seen"][i])
        assert offsets.setdefault(key, delta) == delta
    assert CHILD in got.quality_metrics["mixed_route"]["mask_tables"]

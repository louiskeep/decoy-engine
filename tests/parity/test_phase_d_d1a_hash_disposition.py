"""Phase D / D1a: caller-visible per-column hash disposition on the OOC route.

Each hash column the route masks is reported in
`ExecutionResult.quality_metrics["out_of_core_hash"]`, keyed by table and column,
with the backend that executed it and (for a declined column) the reason. The entry
must survive both drivers and both return paths, and reach the caller of
`run_pipeline`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._operator_registry import PANDAS_ORACLE, PYTHON_SCALAR
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import _runner
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import column_seed, hash_seeds, run_chain
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.unit.execution.test_out_of_core_routing import _fk_ooc_config, _sources

KEY = "out_of_core_hash"
_PAYLOAD = pa.table(
    {
        "a": pa.array(["x", "y", None], pa.string()),
        "b": pa.array([1, 2, 3], pa.int64()),
    }
)
_SEEDS = hash_seeds(["a", "b"])


def _by_column(run: Any) -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["table"], e["column"]): e for e in run.result.quality_metrics[KEY]}


def test_python_scalar_is_a_distinct_label() -> None:
    assert PYTHON_SCALAR == "python_scalar"
    assert PYTHON_SCALAR != PANDAS_ORACLE


@pytest.mark.parametrize("mode", ["resident", "sink-batch", "sink-reorder"])
def test_every_masked_hash_column_is_dispositioned(mode: str, tmp_path: Path) -> None:
    run = run_chain(_PAYLOAD, _SEEDS, **mode_kwargs(mode, tmp_path))
    entries = _by_column(run)
    # Payload columns and the parent-key column are masked by mask_batch; the FK
    # child columns (t.fk, child.ck) are replaced by the join and are not hashed.
    assert set(entries) == {("t", "a"), ("t", "b"), ("t", "hk")}
    for entry in entries.values():
        assert entry["executed_backend"] == PYTHON_SCALAR
        assert entry["executed_backend"] != PANDAS_ORACLE


def test_the_reorder_mode_really_uses_the_reorder_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = _runner.stream_table

    def spy(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs["table_name"])
        return real(*args, **kwargs)

    monkeypatch.setattr(_runner, "stream_table", spy)
    run_chain(_PAYLOAD, _SEEDS, **mode_kwargs("sink-reorder", tmp_path))
    assert "t" in calls


def test_zero_row_table_reports_no_kernel_executed() -> None:
    empty = _PAYLOAD.slice(0, 0)
    entries = _by_column(run_chain(empty, _SEEDS))
    assert {e["executed_backend"] for e in entries.values()} == {"none"}


@pytest.mark.parametrize(
    ("mode", "site"), [("sink-batch", "batch_join"), ("sink-reorder", "stream_join")]
)
def test_remap_sites_report_the_deferred_reason(mode: str, site: str, tmp_path: Path) -> None:
    run = run_chain(_PAYLOAD, _SEEDS, hk_policy=OrphanPolicy.REMAP, **mode_kwargs(mode, tmp_path))
    remap = [e for e in run.result.quality_metrics[KEY] if e["path"] == "fk_remap"]
    assert remap == [
        {
            "table": "child",
            "column": "ck",
            "path": "fk_remap",
            "executed_backend": PYTHON_SCALAR,
            "reason": f"fk_key_hash_remap_native_deferred:{site}",
        }
    ]


def test_disposition_is_visible_through_run_pipeline(tmp_path: Path) -> None:
    config = _fk_ooc_config(tmp_path)
    result = run_pipeline(
        config, _sources(config), engine_version="0.1.0", execution_mode="out_of_core"
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    entries = {(e["table"], e["column"]): e for e in result.quality_metrics[KEY]}
    # parent.id is a hash parent key, child.cid a hash payload; child.parent_id is the FK.
    assert set(entries) == {("parent", "id"), ("child", "cid")}
    assert {e["executed_backend"] for e in entries.values()} == {PYTHON_SCALAR}


def test_non_hash_payload_columns_are_not_dispositioned() -> None:
    seeds = (("a", column_seed("redact", namespace=None)),)
    run = run_chain(_PAYLOAD.select(["a"]), seeds)
    assert [e["column"] for e in run.result.quality_metrics[KEY]] == ["hk"]

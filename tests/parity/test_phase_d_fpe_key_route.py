"""Phase D / FK-key-native: where an fpe-key FK job runs, asserted through production routing.

Two layers, neither a direct adapter call, mirroring the Dcomp route harness. `decide_execution_route`
is fed the fpe-key shape's REAL out-of-core admission, so the (shape, size tier) -> (route, reason)
table follows from the shipped gate: an fpe parent key is not an out-of-core parent strategy, so the
bounded route for this job is always the sequential one. `run_pipeline` then runs a real fpe-key
config and reads the route, the visible reason and the fpe backend a caller would see.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc
from tests.parity._d2c_fpe_data import fpe_key_shape

_OOC_THRESHOLD = 5_000_000
_FPE_PARENT_CODE = "out_of_core_parent_strategy_unsupported"
_SEQUENTIAL = ("sequential", "pure_mask_fk")


def _companion_loads() -> bool:
    try:
        load_compiled_fpe_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


# The backend a sequential fpe key reports: the companion when it loads, else the Python handler.
_SEQ_BACKEND = "rust_companion" if _companion_loads() else "python_scalar"


def _shape() -> s.Shape:
    return fpe_key_shape(OrphanPolicy.PRESERVE)


def _route(shape: s.Shape, **kw: Any) -> tuple[str, str]:
    compatible, code = out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph)
    return decide_execution_route(
        SimpleNamespace(relationships=(object(),), tables=()),
        has_generate_table=False,
        has_mask_table=True,
        validators=[],
        fidelity_report=False,
        vault_writer=None,
        execution_mode=kw.pop("execution_mode", "auto"),
        graph=shape.graph,
        out_of_core_compatible=compatible,
        out_of_core_reject_code=code,
        largest_table_rows=kw.pop("largest_table_rows", 10),
        **kw,
    )


def test_the_real_gate_declines_an_fpe_parent_key_for_the_out_of_core_route() -> None:
    shape = _shape()
    assert out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph) == (
        False,
        _FPE_PARENT_CODE,
    )


# A fit no longer pulls a bounded-eligible job onto full_frame: eligibility alone picks the
# bounded route whether the estimate fits, does not fit, or cannot be priced.
@pytest.mark.parametrize(
    ("estimate", "expected"),
    [
        (True, _SEQUENTIAL),
        (False, _SEQUENTIAL),
        # An unpriceable estimate is treated exactly like "does not fit".
        (None, _SEQUENTIAL),
    ],
    ids=["fits", "does-not-fit", "unpriceable"],
)
def test_byte_estimate_routing_for_an_fpe_key_job(
    estimate: bool | None, expected: tuple[str, str]
) -> None:
    assert (
        _route(
            _shape(),
            use_byte_estimate_routing=True,
            use_probe_routing=False,
            full_frame_fits_estimate=estimate,
        )
        == expected
    )


@pytest.mark.parametrize("rows", [_OOC_THRESHOLD - 1, _OOC_THRESHOLD], ids=["below", "at"])
def test_row_threshold_never_sends_an_fpe_key_job_out_of_core(rows: int) -> None:
    # The same boundary sends a hash composite out of core at the threshold; an fpe key cannot.
    assert (
        _route(
            _shape(),
            use_byte_estimate_routing=False,
            full_frame_fits_estimate=True,  # must be ignored with the flag off
            largest_table_rows=rows,
            out_of_core_threshold_rows=_OOC_THRESHOLD,
        )
        == _SEQUENTIAL
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("sequential", _SEQUENTIAL),
        ("full_frame", ("full_frame", "override_full_frame")),
    ],
)
def test_explicit_execution_mode_overrides_the_estimate(
    mode: str, expected: tuple[str, str]
) -> None:
    assert _route(_shape(), execution_mode=mode, full_frame_fits_estimate=True) == expected


def test_explicit_out_of_core_on_an_fpe_key_job_fails_closed() -> None:
    with pytest.raises(ConfigError, match=_FPE_PARENT_CODE):
        _route(_shape(), execution_mode="out_of_core")


# End to end through run_pipeline on a real fpe-key config.

_PARENT = ["1000000001", "1000000002", "1000000003"]
_CHILD = ["1000000001", "1000000002", "9999999999"]  # last row: a genuine orphan


def _config(tmp_path: Path, policy: str) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table({"k": _PARENT})
    child = pa.table({"k": _CHILD})
    for name, table in (("P", parent), ("C", child)):
        pq.write_table(table, tmp_path / f"{name}.parquet")
    config: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "fpe-key-route", "seed": 7},
        "sources": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "targets": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.out.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "tables": [
            {"name": "P", "columns": [{"name": "k", "strategy": "fpe", "namespace": "ns_k"}]},
            {"name": "C", "columns": [{"name": "k", "strategy": "fpe", "namespace": "ns_k"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "P", "columns": ["k"]},
                "children": [{"table": "C", "columns": ["k"]}],
                "orphan_policy": policy,
                "namespace": "ns_k",
            }
        ],
    }
    return config, {"P": parent, "C": child}


def _run(tmp_path: Path, policy: str = "preserve", **kw: Any) -> Any:
    config, sources = _config(tmp_path, policy)
    return run_pipeline(
        config, sources, engine_version="0.1.0", out_of_core_budget_bytes=1 << 30, **kw
    )


def _visible(result: Any) -> tuple[str, str]:
    execution = result.quality_metrics["execution"]
    return execution["execution_mode"], execution["route_reason"]


def _fpe_backends(result: Any) -> dict[tuple[str, str, str], str]:
    return {
        (e["table"], e["column"], e["path"]): e["executed_backend"]
        for e in result.quality_metrics.get("sequential_fpe", [])
    }


def test_default_routing_runs_a_small_fpe_key_job_sequential(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert _visible(result) == _SEQUENTIAL
    assert _fpe_backends(result)  # the sequential seam ran, not the full-frame oracle


@pytest.mark.parametrize(
    "kw",
    [{"use_byte_estimate_routing": False}, {"execution_mode": "sequential"}],
    ids=["flag-off", "explicit-sequential"],
)
def test_sequential_route_reports_its_reason_and_its_fpe_backend(
    tmp_path: Path, kw: dict[str, Any]
) -> None:
    result = _run(tmp_path, "remap", **kw)
    assert _visible(result) == _SEQUENTIAL
    assert _fpe_backends(result) == {
        ("P", "k", "scalar"): _SEQ_BACKEND,
        ("P", "k", "fk_remap"): _SEQ_BACKEND,
        ("C", "k", "fk_inherited"): "inherited",
    }


@pytest.mark.parametrize("threshold", [3, 4], ids=["at", "below"])
def test_flag_off_threshold_still_runs_an_fpe_key_job_sequentially(
    tmp_path: Path, threshold: int
) -> None:
    result = _run(tmp_path, use_byte_estimate_routing=False, out_of_core_threshold_rows=threshold)
    assert _visible(result) == _SEQUENTIAL


def test_explicit_full_frame_is_reported_and_out_of_core_is_refused(tmp_path: Path) -> None:
    assert _visible(_run(tmp_path, execution_mode="full_frame")) == (
        "full_frame",
        "override_full_frame",
    )
    with pytest.raises(ConfigError, match=_FPE_PARENT_CODE):
        _run(tmp_path, execution_mode="out_of_core")


@pytest.mark.parametrize("policy", ["preserve", "remap"])
def test_full_frame_and_sequential_routes_return_strictly_equal_fpe_key_tables(
    tmp_path: Path, policy: str
) -> None:
    full = _run(tmp_path, policy, execution_mode="full_frame")
    sequential = _run(tmp_path, policy, execution_mode="sequential")
    for table in ("P", "C"):
        assert sc.compare_strict(full.outputs[table], sequential.outputs[table]) == dict.fromkeys(
            sc.DIMENSIONS, True
        )

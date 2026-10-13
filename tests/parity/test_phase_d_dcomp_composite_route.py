"""Phase D / Dcomp: where a composite-FK job runs, asserted through production routing.

Two layers, neither a direct adapter call. `decide_execution_route` is fed the composite shape's
REAL out-of-core admission (the static compatibility gate, not a hand-set flag), so the
(strategy, size tier) -> (route, reason) table follows from the shipped gate. `run_pipeline` then
runs a real composite config and reads the route, the visible reason and the hash backend the
caller would see.

The two composite shapes differ only in how the child is masked. The canonical shape (a child
group) is admitted to the out-of-core route. The scalar-child shape is declined by the gate and so
is always bounded by the sequential route; that decline is pinned elsewhere and only used here.
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
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_shapes as d
from tests.parity import _strict_compare as sc

_OOC_THRESHOLD = 5_000_000
_SCALAR_CHILD_CODE = "out_of_core_composite_fk_scalar_child_unsupported"


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


# The backend a sequential hash column reports: the companion when it loads, else the Python handler.
_SEQ_BACKEND = "rust_companion" if _companion_loads() else "python_scalar"


def _canonical_group_shape() -> s.Shape:
    return s.composite_shape(OrphanPolicy.PRESERVE)


def _scalar_child_shape() -> s.Shape:
    """The canonical shape's parent with the child columns masked as independent scalar seeds."""
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": s.hashed("ns_pk"), "j": s.hashed("ns_pj")}),
            "C": s.table_seed({"k": s.hashed("ns_ck"), "j": s.hashed("ns_cj")}),
        }
    )
    base = s.composite_shape(OrphanPolicy.PRESERVE)
    return s.Shape(plan, base.graph, base.sources)


def _admission(shape: s.Shape) -> tuple[bool, str | None]:
    return out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph)


def _route(shape: s.Shape, **kw: Any) -> tuple[str, str]:
    compatible, code = _admission(shape)
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


def test_the_real_gate_admits_the_canonical_group_and_declines_the_scalar_child() -> None:
    assert _admission(_canonical_group_shape()) == (True, None)
    assert _admission(_scalar_child_shape()) == (False, _SCALAR_CHILD_CODE)


# estimate-FIT -> full_frame; no fit or no price -> the bounded route eligibility picks.
@pytest.mark.parametrize(
    ("estimate", "canonical", "scalar_child"),
    [
        (
            True,
            ("full_frame", "byte_estimate_full_frame_fits"),
            ("full_frame", "byte_estimate_full_frame_fits"),
        ),
        (
            False,
            ("out_of_core", "byte_estimate_bounded_out_of_core"),
            ("sequential", "pure_mask_fk"),
        ),
        # An unpriceable estimate is treated exactly like "does not fit": never admitted to full_frame.
        (
            None,
            ("out_of_core", "byte_estimate_bounded_out_of_core"),
            ("sequential", "pure_mask_fk"),
        ),
    ],
    ids=["fits", "does-not-fit", "unpriceable"],
)
def test_byte_estimate_routing_for_a_composite_job(
    estimate: bool | None, canonical: tuple[str, str], scalar_child: tuple[str, str]
) -> None:
    kw: dict[str, Any] = {
        "use_byte_estimate_routing": True,
        "use_probe_routing": False,
        "full_frame_fits_estimate": estimate,
    }
    assert _route(_canonical_group_shape(), **kw) == canonical
    assert _route(_scalar_child_shape(), **kw) == scalar_child


@pytest.mark.parametrize(
    ("rows", "canonical", "scalar_child"),
    [
        (_OOC_THRESHOLD - 1, ("sequential", "pure_mask_fk"), ("sequential", "pure_mask_fk")),
        (_OOC_THRESHOLD, ("out_of_core", "out_of_core_large_fk"), ("sequential", "pure_mask_fk")),
    ],
    ids=["below-threshold", "at-threshold"],
)
def test_row_threshold_routing_when_byte_estimate_routing_is_off(
    rows: int, canonical: tuple[str, str], scalar_child: tuple[str, str]
) -> None:
    kw: dict[str, Any] = {
        "use_byte_estimate_routing": False,
        "full_frame_fits_estimate": True,  # must be ignored with the flag off
        "largest_table_rows": rows,
        "out_of_core_threshold_rows": _OOC_THRESHOLD,
    }
    assert _route(_canonical_group_shape(), **kw) == canonical
    assert _route(_scalar_child_shape(), **kw) == scalar_child


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("sequential", ("sequential", "pure_mask_fk")),
        ("full_frame", ("full_frame", "override_full_frame")),
        ("out_of_core", ("out_of_core", "override_out_of_core")),
    ],
)
def test_explicit_execution_mode_overrides_the_estimate(
    mode: str, expected: tuple[str, str]
) -> None:
    # A fitting estimate must not turn an explicit request into full_frame.
    assert (
        _route(_canonical_group_shape(), execution_mode=mode, full_frame_fits_estimate=True)
        == expected
    )


def test_explicit_out_of_core_on_the_declined_scalar_child_fails_closed() -> None:
    with pytest.raises(ConfigError, match=_SCALAR_CHILD_CODE):
        _route(_scalar_child_shape(), execution_mode="out_of_core")
    # The declined shape still runs bounded when asked for sequential.
    assert _route(_scalar_child_shape(), execution_mode="sequential") == (
        "sequential",
        "pure_mask_fk",
    )


# End to end through run_pipeline on a real composite config.


def _composite_config(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table({"k": ["a", "b", "c"], "j": ["x", "y", "z"]})
    child = pa.table({"k": ["a", "b", "q"], "j": ["x", "y", "w"]})  # last row: a genuine orphan
    for name, table in (("P", parent), ("C", child)):
        pq.write_table(table, tmp_path / f"{name}.parquet")

    def hashed(column: str) -> dict[str, Any]:
        return {"name": column, "strategy": "hash", "namespace": f"ns_{column}"}

    config: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "dcomp-route", "seed": 7},
        "sources": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "targets": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.out.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "tables": [
            {"name": "P", "columns": [hashed("k"), hashed("j")]},
            {"name": "C", "columns": []},
        ],
        "relationships": [
            {
                "parent": {"table": "P", "columns": ["k", "j"]},
                "children": [{"table": "C", "columns": ["k", "j"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_cg",
            }
        ],
    }
    return config, {"P": parent, "C": child}


def _run(tmp_path: Path, **kw: Any) -> Any:
    config, sources = _composite_config(tmp_path)
    return run_pipeline(
        config, sources, engine_version="0.1.0", out_of_core_budget_bytes=1 << 30, **kw
    )


def _visible(result: Any) -> tuple[str, str]:
    execution = result.quality_metrics["execution"]
    return execution["execution_mode"], execution["route_reason"]


def _hash_backends(result: Any) -> dict[tuple[str, str, str], str]:
    return {
        (e["table"], e["column"], e["path"]): e["executed_backend"]
        for e in result.quality_metrics.get("sequential_hash", [])
    }


def test_default_routing_runs_a_small_composite_job_full_frame(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert _visible(result) == ("full_frame", "byte_estimate_full_frame_fits")
    assert _hash_backends(result) == {}  # the full-frame oracle, not the sequential native seam


@pytest.mark.parametrize(
    "kw",
    [{"use_byte_estimate_routing": False}, {"execution_mode": "sequential"}],
    ids=["flag-off", "explicit-sequential"],
)
def test_sequential_route_reports_its_reason_and_its_hash_backend(
    tmp_path: Path, kw: dict[str, Any]
) -> None:
    result = _run(tmp_path, **kw)
    assert _visible(result) == ("sequential", "pure_mask_fk")
    assert _hash_backends(result) == {
        ("P", "k", "scalar"): _SEQ_BACKEND,
        ("P", "j", "scalar"): _SEQ_BACKEND,
        ("C", "k", "fk_inherited"): "inherited",
        ("C", "j", "fk_inherited"): "inherited",
    }


def test_flag_off_row_threshold_decides_sequential_versus_out_of_core(tmp_path: Path) -> None:
    # Three-row tables with the threshold moved to the row count, so the at/below boundary is
    # exercised end to end without 5M rows.
    at = _run(tmp_path, use_byte_estimate_routing=False, out_of_core_threshold_rows=3)
    below = _run(tmp_path, use_byte_estimate_routing=False, out_of_core_threshold_rows=4)
    assert _visible(at) == ("out_of_core", "out_of_core_large_fk")
    assert _visible(below) == ("sequential", "pure_mask_fk")


def test_explicit_full_frame_and_out_of_core_overrides_are_reported(tmp_path: Path) -> None:
    assert _visible(_run(tmp_path, execution_mode="full_frame")) == (
        "full_frame",
        "override_full_frame",
    )
    assert _visible(_run(tmp_path, execution_mode="out_of_core")) == (
        "out_of_core",
        "override_out_of_core",
    )


def test_full_frame_and_sequential_routes_return_strictly_equal_composite_tables(
    tmp_path: Path,
) -> None:
    # Same job through two production routes: the route is a performance choice, never a data one.
    full = _run(tmp_path, execution_mode="full_frame")
    sequential = _run(tmp_path, execution_mode="sequential")
    for table in ("P", "C"):
        assert sc.compare_strict(full.outputs[table], sequential.outputs[table]) == d.ALL_TRUE

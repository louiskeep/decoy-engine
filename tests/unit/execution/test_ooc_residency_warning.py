"""P4-A (Option A): the OOC route's caller-managed residency warning.

The out-of-core route's memory bound -- engine-controlled peak residency bounded
with respect to table row cardinality -- holds only for the structural bounded
shape: every source a ``LazySource`` plus a sink that consumes ``write_batches``
incrementally without retaining the stream. A resident ``pa.Table`` source, a
missing sink, or a ``source_loader`` is caller-managed: the route runs it
unchanged but emits one best-effort ``CallerManagedResidencyWarning`` and records
``quality_metrics["residency"]``, without altering the result.

Four cross-model plan-gate rounds established that a precise fail-closed byte
guard cannot make the bound absolute for arbitrary in-process callers, so the
route documents and signals the caller-managed shapes rather than policing them.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._residency_warning import (
    CallerManagedResidencyWarning,
    caller_managed_residency_shapes,
    residency_warning_message,
)
from decoy_engine.profile._readers import LazySource
from tests.unit.execution.test_out_of_core_routing import (
    _fk_ooc_config,
    _sources,
    _values,
)


def _lazy(tmp_path: Path, name: str = "lz") -> LazySource:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(pa.table({"id": pa.array(["a", "b"], type=pa.string())}), path)
    return LazySource(path)


def _resident() -> pa.Table:
    return pa.table({"id": pa.array(["a", "b"], type=pa.string())})


# ---------------------------------------------------------------------------
# 1. The shape predicate (the mutation target): residency detected by TYPE.
# ---------------------------------------------------------------------------


def test_bounded_shape_reports_no_caller_managed_shapes(tmp_path: Path) -> None:
    # LazySource sources + a sink + no loader = the guaranteed bounded shape.
    shapes = caller_managed_residency_shapes(
        {"t": _lazy(tmp_path)}, sink=object(), source_loader=None
    )
    assert shapes == ()


def test_resident_source_is_detected_by_type_not_dict_non_emptiness(tmp_path: Path) -> None:
    # A non-empty sources dict of LazySources must NOT read as resident; only a
    # pa.Table value does. This is the exact bug a `bool(sources)` check would have.
    assert caller_managed_residency_shapes(
        {"t": _lazy(tmp_path)}, sink=object(), source_loader=None
    ) == ()
    resident = caller_managed_residency_shapes(
        {"t": _resident()}, sink=object(), source_loader=None
    )
    assert resident == ("a resident pa.Table source",)


def test_missing_sink_is_a_caller_managed_shape(tmp_path: Path) -> None:
    shapes = caller_managed_residency_shapes(
        {"t": _lazy(tmp_path)}, sink=None, source_loader=None
    )
    assert shapes == ("no sink (the whole output is held resident)",)


def test_source_loader_is_a_caller_managed_shape(tmp_path: Path) -> None:
    shapes = caller_managed_residency_shapes(
        {"t": _lazy(tmp_path)}, sink=object(), source_loader=lambda name: _resident()
    )
    assert shapes == ("a source_loader (returns an unbounded resident table)",)


def test_all_three_shapes_reported_together(tmp_path: Path) -> None:
    shapes = caller_managed_residency_shapes(
        {"t": _resident()}, sink=None, source_loader=lambda name: _resident()
    )
    assert "a resident pa.Table source" in shapes
    assert any(s.startswith("no sink") for s in shapes)
    assert any(s.startswith("a source_loader") for s in shapes)
    assert len(shapes) == 3


def test_mixed_residency_reports_resident(tmp_path: Path) -> None:
    # One lazy + one resident source: the resident one is caller-managed.
    shapes = caller_managed_residency_shapes(
        {"lazy": _lazy(tmp_path), "res": _resident()}, sink=object(), source_loader=None
    )
    assert shapes == ("a resident pa.Table source",)


def test_warning_message_names_shape_and_bounded_alternative() -> None:
    msg = residency_warning_message(("a resident pa.Table source",))
    assert "a resident pa.Table source" in msg
    assert "LazySource" in msg
    assert "write_batches" in msg
    assert "full_frame" in msg


# ---------------------------------------------------------------------------
# 2. Emission through run_pipeline (integration) + the structured record.
# ---------------------------------------------------------------------------


def test_forced_ooc_resident_no_sink_warns_and_records(tmp_path: Path) -> None:
    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)  # resident pa.Tables, no sink passed
    with pytest.warns(CallerManagedResidencyWarning) as caught:
        result = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode="out_of_core"
        )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    message = str(caught[0].message)
    assert "a resident pa.Table source" in message
    assert "no sink" in message
    residency = result.quality_metrics["residency"]
    assert residency["caller_managed"] is True
    assert "a resident pa.Table source" in residency["shapes"]
    assert any(s.startswith("no sink") for s in residency["shapes"])


def test_forced_ooc_resident_with_sink_still_warns_but_not_for_sink(tmp_path: Path) -> None:
    from decoy_engine.execution import ParquetTransactionalSink

    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)
    sink = ParquetTransactionalSink(tmp_path / "ooc_out")
    with pytest.warns(CallerManagedResidencyWarning) as caught:
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            execution_mode="out_of_core",
            sink=sink,
        )
    message = str(caught[0].message)
    assert "a resident pa.Table source" in message
    assert "no sink" not in message  # a sink is present
    assert "no sink" not in " ".join(result.quality_metrics["residency"]["shapes"])


def test_forced_ooc_byte_parity_preserved_with_warning(tmp_path: Path) -> None:
    # The warning is additive: the masked output still matches the full-frame oracle.
    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)
    full = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    with pytest.warns(CallerManagedResidencyWarning):
        forced = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode="out_of_core"
        )
    assert _values(forced.outputs) == _values(full.outputs)


def test_full_frame_route_does_not_warn(tmp_path: Path) -> None:
    # The warning is scoped to the managed OOC route; a full-frame job (guard
    # never consulted) is silent even with resident sources.
    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)
    with warnings.catch_warnings():
        warnings.simplefilter("error", CallerManagedResidencyWarning)
        result = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode="full_frame"
        )
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert "residency" not in result.quality_metrics


def test_run_fk_out_of_core_documents_the_precondition() -> None:
    from decoy_engine.execution.out_of_core import run_fk_out_of_core

    doc = run_fk_out_of_core.__doc__ or ""
    assert "Residency precondition" in doc
    assert "caller-managed" in doc
    assert "LazySource" in doc

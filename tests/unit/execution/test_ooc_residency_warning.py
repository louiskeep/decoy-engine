"""P4-A (Option A): the OOC route's caller-managed residency warning.

The out-of-core route's memory bound -- engine-controlled peak residency bounded
with respect to table row cardinality -- holds only for the structural bounded
shape: every source a ``LazySource`` plus a sink that consumes ``write_batches``
incrementally without retaining the stream. A resident ``pa.Table`` source, a
missing sink, or a ``source_loader`` is caller-managed: the route runs it
unchanged but attaches a structured ``QualityWarning`` (code
``out_of_core_caller_managed_residency``) to ``ExecutionResult.warnings`` and
records ``quality_metrics["residency"]``, without altering the result.

The warning is structured, NOT a stdlib ``warnings.warn``: it rides in the
returned result, so it is control-flow-neutral -- a caller's ``-W error`` cannot
escalate it into a rejection. Four cross-model plan-gate rounds established that a
precise fail-closed byte guard cannot make the bound absolute for arbitrary
in-process callers, so the route signals the caller-managed shapes rather than
policing them.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._residency_warning import (
    RESIDENCY_WARNING_CODE,
    caller_managed_residency_shapes,
    residency_quality_warning,
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


def _residency_qws(result: object) -> list:
    return [
        w
        for w in getattr(result, "warnings", ())
        if getattr(w, "code", None) == RESIDENCY_WARNING_CODE
    ]


def _file_loader(config: dict):
    paths = {name: spec["path"] for name, spec in config["sources"].items()}
    return lambda name: pq.read_table(paths[name])


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
    assert (
        caller_managed_residency_shapes({"t": _lazy(tmp_path)}, sink=object(), source_loader=None)
        == ()
    )
    resident = caller_managed_residency_shapes(
        {"t": _resident()}, sink=object(), source_loader=None
    )
    assert resident == ("a resident pa.Table source",)


def test_missing_sink_is_a_caller_managed_shape(tmp_path: Path) -> None:
    shapes = caller_managed_residency_shapes({"t": _lazy(tmp_path)}, sink=None, source_loader=None)
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


def test_empty_shapes_message_is_empty() -> None:
    # A direct caller cannot build a malformed "...for this call: ." message.
    assert residency_warning_message(()) == ""


def test_residency_quality_warning_shape() -> None:
    qw = residency_quality_warning(("a resident pa.Table source",))
    assert qw.code == RESIDENCY_WARNING_CODE
    assert qw.provider == ""  # route-level, not provider-attributed
    assert qw.column is None  # not column-attributed
    assert qw.detail["shapes"] == ["a resident pa.Table source"]
    assert "a resident pa.Table source" in qw.detail["message"]


# ---------------------------------------------------------------------------
# 2. Emission through run_pipeline (integration): a structured .warnings entry.
# ---------------------------------------------------------------------------


def test_forced_ooc_resident_no_sink_warns_and_records(tmp_path: Path) -> None:
    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)  # resident pa.Tables, no sink passed
    result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    qws = _residency_qws(result)
    assert len(qws) == 1
    shapes = qws[0].detail["shapes"]
    assert "a resident pa.Table source" in shapes
    assert any(s.startswith("no sink") for s in shapes)
    residency = result.quality_metrics["residency"]
    assert residency["caller_managed"] is True
    assert "a resident pa.Table source" in residency["shapes"]


def test_control_flow_neutral_under_warnings_as_error(tmp_path: Path) -> None:
    # The HIGH fix: the residency signal is a structured result entry, not a
    # stdlib warnings.warn, so a caller's -W error cannot escalate it into a
    # rejection. Under simplefilter("error") the job still completes and returns
    # the structured warning.
    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert len(_residency_qws(result)) == 1


def test_forced_ooc_resident_with_sink_warns_but_not_for_sink(tmp_path: Path) -> None:
    from decoy_engine.execution import ParquetTransactionalSink

    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)
    sink = ParquetTransactionalSink(tmp_path / "ooc_out")
    result = run_pipeline(
        config, sources, engine_version="0.1.0", execution_mode="out_of_core", sink=sink
    )
    shapes = _residency_qws(result)[0].detail["shapes"]
    assert "a resident pa.Table source" in shapes
    assert not any(s.startswith("no sink") for s in shapes)  # a sink is present


def test_forced_ooc_loader_job_records_the_source_loader_shape(tmp_path: Path) -> None:
    # MEDIUM fix: an end-to-end loader job (sources={} + source_loader) routed to
    # OOC records the residency warning naming the source_loader shape. The shape
    # is classified on the pre-resolution sources, so it is present regardless of
    # what the loader returns.
    config = _fk_ooc_config(tmp_path)
    result = run_pipeline(
        config,
        {},
        engine_version="0.1.0",
        execution_mode="out_of_core",
        source_loader=_file_loader(config),
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    shapes = _residency_qws(result)[0].detail["shapes"]
    assert any(s.startswith("a source_loader") for s in shapes)


def test_forced_ooc_byte_parity_preserved_with_warning(tmp_path: Path) -> None:
    # The warning is additive: the masked output still matches the full-frame oracle.
    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)
    full = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    forced = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")
    assert _residency_qws(forced)  # warning present
    assert _values(forced.outputs) == _values(full.outputs)


def test_guaranteed_lazysource_sink_shape_is_silent_end_to_end(tmp_path: Path) -> None:
    # The no-false-positive property, pinned end to end: an all-LazySource + sink
    # OOC job routed through run_pipeline records no residency warning and no
    # residency metric. Locks it against a future change that resolves sources to
    # resident tables before the guard runs.
    from decoy_engine.execution import ParquetTransactionalSink

    config = _fk_ooc_config(tmp_path)
    lazy_sources = {
        name: LazySource(Path(spec["path"])) for name, spec in config["sources"].items()
    }
    sink = ParquetTransactionalSink(tmp_path / "guaranteed_out")
    result = run_pipeline(
        config,
        lazy_sources,
        engine_version="0.1.0",
        execution_mode="out_of_core",
        sink=sink,
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert _residency_qws(result) == []
    assert "residency" not in result.quality_metrics


def test_full_frame_route_does_not_warn(tmp_path: Path) -> None:
    # The warning is scoped to the managed OOC route; a full-frame job (guard
    # never consulted) records no residency warning even with resident sources.
    config = _fk_ooc_config(tmp_path)
    sources = _sources(config)
    result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert _residency_qws(result) == []
    assert "residency" not in result.quality_metrics


def test_run_fk_out_of_core_documents_the_precondition() -> None:
    from decoy_engine.execution.out_of_core import run_fk_out_of_core

    doc = run_fk_out_of_core.__doc__ or ""
    assert "Residency precondition" in doc
    assert "caller-managed" in doc
    assert "LazySource" in doc

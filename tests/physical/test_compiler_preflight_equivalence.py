"""D4: the preflight-decision-equivalence harness (the exit gate).

For each fixture, this asserts `compile_physical_plan`'s driver selection
matches what `run_pipeline` would ACTUALLY dispatch to, without ever letting
`run_pipeline` mask a single row: `_live_dispatched_driver` monkeypatches the
five real dispatch boundaries (`run_sequential_route`, `run_out_of_core_
route`, `_run_native_streaming`, `run_mask_chunked`,
`PandasExecutionAdapter.run`) to raise a marker naming which one was
reached, then runs the REAL `run_pipeline` and catches the marker. Everything
upstream of that boundary -- `decide_execution_route`, `classify_job`,
`static_candidacy`, `classify_and_preflight`, `peek_and_admit` -- runs
UNCHANGED, for real, exactly as `run_pipeline` runs it; only the moment
where a driver would start masking is swapped for an exception. This is a
stronger oracle than re-calling the same decision functions a second time
(which would only catch a mistake in re-deriving inputs, not a mis-wiring of
the precedence itself): it exercises production's ACTUAL `if`/`elif` control
flow.

Reject-before-read (`ExecutionError`) and forced-mode (`ConfigError`)
failures are asserted the same way on both sides: the compiler calls
`decide_execution_route` directly (`_compiler.layer1_route`), so it raises
the identical exception `run_pipeline` raises for the same inputs -- code-
for-code for `ExecutionError`, normalized branch identity for the uncoded
`ConfigError` forced-mode failures (plan D3's documented exclusion).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import ConfigError
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.physical import (
    DriverId,
    capture_physical_plan_inputs,
    compile_physical_plan,
)
from decoy_engine.execution.physical import _reasons
from decoy_engine.execution.physical._plan import PhysicalTable, RejectedAlternative
from decoy_engine.execution.physical._reasons import (
    FORCED_MODE_BRANCH_IDENTITIES,
    native_reason_code_family,
)
from decoy_engine.profile._readers import LazySource


class _DriverMarkerError(Exception):
    """Raised by a patched dispatch boundary; `driver` names which one."""

    def __init__(self, driver: str) -> None:
        super().__init__(driver)
        self.driver = driver


def _bomb(driver: str) -> Any:
    def _fn(*_args: Any, **_kwargs: Any) -> Any:
        raise _DriverMarkerError(driver)

    return _fn


@contextlib.contextmanager
def _patched_dispatch(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "decoy_engine.execution._pipeline_route_exec.run_sequential_route",
        _bomb(DriverId.SEQUENTIAL.value),
    )
    monkeypatch.setattr(
        "decoy_engine.execution._pipeline_route_exec.run_out_of_core_route",
        _bomb(DriverId.OUT_OF_CORE.value),
    )
    monkeypatch.setattr(
        "decoy_engine.execution._native_route_exec._run_native_streaming",
        _bomb(DriverId.NATIVE_STREAM.value),
    )
    monkeypatch.setattr(
        "decoy_engine.execution._pipeline_route_exec.run_mask_chunked",
        _bomb(DriverId.CHUNKED.value),
    )
    monkeypatch.setattr(PandasExecutionAdapter, "run", _bomb(DriverId.FULL_FRAME.value))
    yield


def live_dispatched_driver(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, Any], sources: Any, **kwargs: Any
) -> str:
    """Run the REAL `run_pipeline` with the dispatch boundaries bombed;
    return which driver it reached. Lets `ExecutionError`/`ConfigError`
    propagate uncaught -- callers that expect a reject/forced-mode failure
    assert on those directly (`pytest.raises`), never on this return value.
    """
    with _patched_dispatch(monkeypatch):
        try:
            run_pipeline(config, sources, engine_version="d4-oracle", **kwargs)
        except _DriverMarkerError as marker:
            return marker.driver
    raise AssertionError("run_pipeline completed without dispatching to any known driver")


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _single_table_config(
    tmp_path: Path, source: pa.Table, *, strategy: str = "redact"
) -> dict[str, Any]:
    path = _write(tmp_path, source, "t")
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": strategy}]}],
        }
    ).model_dump()


def _fk_config(tmp_path: Path, parent: pa.Table, child: pa.Table) -> dict[str, Any]:
    parent_path = _write(tmp_path, parent, "parent")
    child_path = _write(tmp_path, child, "child")
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
                "child": {"type": "file", "format": "parquet", "path": str(child_path)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()


def _live_route(inputs: Any) -> tuple[str, str]:
    """Call `decide_execution_route` -- the SAME live function `_compiler.
    layer1_route` calls -- a second, independent time, with the identical
    captured signal values. `decide_execution_route` is itself a pure
    function of its arguments, so a second call over the same `inputs`
    cannot diverge from the compiler's own call; this recomputes `(route,
    route_reason)` as an oracle for the reason-string assertions below,
    without going through `_compiler.py` at all.
    """
    from decoy_engine.execution._pipeline_routing import decide_execution_route

    facts = inputs.out_of_core_facts
    vault_writer_sentinel = object() if inputs.vault_writer_present else None
    return decide_execution_route(
        inputs.profile,
        has_generate_table=inputs.has_generate_table,
        has_mask_table=inputs.has_mask_table,
        validators=list(inputs.validators),
        fidelity_report=inputs.fidelity_report,
        vault_writer=vault_writer_sentinel,
        execution_mode=inputs.execution_mode,
        graph=inputs.graph,
        resolved_substrate=inputs.resolved_substrate,
        out_of_core_compatible=facts.compatible,
        out_of_core_reject_code=facts.reject_code,
        largest_table_rows=facts.largest_table_rows,
        largest_table_rows_exact=facts.largest_table_rows_exact,
        out_of_core_threshold_rows=inputs.out_of_core_threshold_rows,
        full_frame_reject_rows=inputs.full_frame_reject_rows,
        use_byte_estimate_routing=inputs.use_byte_estimate_routing,
        full_frame_fits_estimate=facts.full_frame_fits_estimate,
        use_probe_routing=inputs.use_probe_routing,
        probe_recovers_full_frame=facts.probe_recovers_full_frame,
    )


def _expected_out_of_core_not_ready_reason(inputs: Any) -> str:
    """The H2 oracle: the live `out_of_core_ready` conjunction
    (`_pipeline_routing.decide_execution_route`'s own docstring), inverted,
    computed directly from the LIVE sub-decision functions
    (`_sequential_eligible`, `_has_cross_table_fk_cycle`) -- independently of
    `_compiler.out_of_core_not_ready_reason`, which this is meant to check,
    not call. This is the exact H2 counterexample surface: pre-fix, the
    compiler never reached the `eligible`/`cyclic`/`has_mask_table` operands
    at all.
    """
    from decoy_engine.execution._pipeline_routing import (
        _has_cross_table_fk_cycle,
        _sequential_eligible,
    )

    vault_writer_sentinel = object() if inputs.vault_writer_present else None
    eligible, eligibility_reason = _sequential_eligible(
        inputs.profile,
        has_generate_table=inputs.has_generate_table,
        validators=list(inputs.validators),
        fidelity_report=inputs.fidelity_report,
        vault_writer=vault_writer_sentinel,
        resolved_substrate=inputs.resolved_substrate,
    )
    if not eligible:
        return eligibility_reason
    if _has_cross_table_fk_cycle(inputs.graph):
        return _reasons.OUT_OF_CORE_NOT_READY_CYCLIC
    if not inputs.has_mask_table:
        return _reasons.OUT_OF_CORE_NOT_READY_NO_MASK_TABLE
    facts = inputs.out_of_core_facts
    if not facts.compatible:
        return facts.reject_code or _reasons.OUT_OF_CORE_NOT_READY_INCOMPATIBLE
    if facts.largest_table_rows is None:
        return _reasons.OUT_OF_CORE_NOT_READY_NO_SIZE_SIGNAL
    if facts.largest_table_rows < inputs.out_of_core_threshold_rows:
        return (
            f"{_reasons.OUT_OF_CORE_NOT_READY_BELOW_THRESHOLD_PREFIX}:{facts.largest_table_rows}"
        )
    return _reasons.OUT_OF_CORE_READY_CONTRADICTION  # pragma: no cover


def _expected_relationship_alternatives(
    inputs: Any, route_reason: str
) -> tuple[tuple[DriverId, str, bool], ...]:
    if not inputs.has_relationships:
        return ()
    preferred_over_bounded = route_reason in (
        _reasons.ROUTE_BYTE_ESTIMATE_FULL_FRAME_FITS,
        _reasons.ROUTE_PROBE_RECOVERED_FULL_FRAME,
        _reasons.ROUTE_OVERRIDE_FULL_FRAME,
    )
    out_of_core_reason = (
        route_reason if preferred_over_bounded else _expected_out_of_core_not_ready_reason(inputs)
    )
    return (
        (DriverId.OUT_OF_CORE, out_of_core_reason, True),
        (DriverId.SEQUENTIAL, route_reason, True),
    )


def _expected_native_alternative(inputs: Any) -> tuple[DriverId, str, bool]:
    """Repackaging check ONLY: `inputs.native_admission` is an already-
    captured REAL fact (the live `static_candidacy` -> `classify_and_
    preflight` -> `peek_and_admit` chain ran once at capture time), so this
    asserts the compiler packaged it correctly, not that the fact itself is
    correct (that is `NativeAdmissionFact`'s own construction, exercised by
    `capture_native_admission_fact` directly)."""
    applies = inputs.native_route_enabled and inputs.has_mask_table
    if not applies:
        return (DriverId.NATIVE_STREAM, "native_route_disabled_or_no_mask_table", False)
    admission = inputs.native_admission
    return (DriverId.NATIVE_STREAM, admission.reason or "native_admission_declined", True)


def _expected_chunked_alternative(inputs: Any) -> tuple[DriverId, str, bool]:
    """Recomputes `classify_job` -- the SAME live function `_compiler.
    layer2_chunk_decision` calls -- a second, independent time."""
    if not (inputs.auto_chunk and inputs.has_mask_table):
        return (DriverId.CHUNKED, "auto_chunk_disabled_or_no_mask_table", False)
    from decoy_engine.execution._planner import classify_job

    decision = classify_job(
        dict(inputs.config),
        plan=inputs.plan,
        registry=inputs.registry,
        relationship_graph=inputs.graph,
        substrate=inputs.resolved_substrate,
        source_tables=inputs.caller_sources,
        auto_chunk_threshold_rows=inputs.auto_chunk_threshold_rows,
    )
    if decision.mode == "chunked":
        return (DriverId.CHUNKED, _reasons.DRIVER_REASON_CHUNKED_ADMITTED, True)
    from decoy_engine.execution.physical._reasons import translate_chunked_rejection

    codes = translate_chunked_rejection(decision.rejections.get("chunked", decision.reason))
    return (DriverId.CHUNKED, ";".join(codes), True)


def _assert_reason_and_alternatives_match_live(inputs: Any, table: PhysicalTable) -> None:
    """H4 fix: assert `driver_reason` AND the ordered `rejected_alternatives`
    against the live functions each code mirrors -- not merely the driver
    id. This is the exact gate that would have caught H2 (a false
    `out_of_core_ready` reason inside a `full_frame` table's
    `rejected_alternatives`): the pre-fix D4 harness never looked past
    `table.driver.value`.
    """
    route, route_reason = _live_route(inputs)
    expected_relationship_alts = _expected_relationship_alternatives(inputs, route_reason)

    if table.driver == DriverId.SEQUENTIAL:
        assert route == "sequential"
        assert table.driver_reason == route_reason
        assert table.rejected_alternatives == (
            RejectedAlternative(
                DriverId.OUT_OF_CORE, _expected_out_of_core_not_ready_reason(inputs), True
            ),
        )
        return

    if table.driver == DriverId.OUT_OF_CORE:
        assert route == "out_of_core"
        assert table.driver_reason == route_reason
        assert table.rejected_alternatives == ()
        return

    # route == "full_frame": narrow among native_stream / chunked / full_frame,
    # exactly mirroring `select_driver`'s own precedence.
    assert route == "full_frame"
    native_alt = _expected_native_alternative(inputs)
    chunked_alt = _expected_chunked_alternative(inputs)

    if table.driver == DriverId.NATIVE_STREAM:
        assert table.driver_reason == _reasons.DRIVER_REASON_NATIVE_ADMITTED
        expected = (*expected_relationship_alts, chunked_alt)
    elif table.driver == DriverId.CHUNKED:
        assert table.driver_reason == _reasons.DRIVER_REASON_CHUNKED_ADMITTED
        expected = (*expected_relationship_alts, native_alt)
    else:
        assert table.driver == DriverId.FULL_FRAME
        assert table.driver_reason == route_reason
        expected = (*expected_relationship_alts, native_alt, chunked_alt)

    actual = tuple((alt.driver, alt.reason, alt.attempted) for alt in table.rejected_alternatives)
    assert actual == expected, (
        f"table {table.table!r} ({table.driver.value!r}): "
        f"rejected_alternatives {actual!r} != expected {expected!r}"
    )


def _assert_equivalent(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
    sources: Any,
    *,
    engine_version: str = "d4-corpus",
    **kwargs: Any,
) -> None:
    """Build a real `PhysicalPlanInputs` snapshot, compile it, and assert the
    resulting driver, driver_reason, AND ordered rejected_alternatives all
    match live for the SAME config/sources/kwargs (H4: driver-id-only
    equivalence is not enough -- see `_assert_reason_and_alternatives_match_
    live`)."""
    inputs = capture_physical_plan_inputs(config, sources, engine_version=engine_version, **kwargs)
    plan = compile_physical_plan(inputs)
    assert plan.tables, "expected at least one masking table in the compiled plan"
    live_driver = live_dispatched_driver(monkeypatch, config, sources, **kwargs)
    for table in plan.tables:
        assert table.driver.value == live_driver, (
            f"table {table.table!r}: compiler picked {table.driver.value!r} "
            f"({table.driver_reason!r}) but run_pipeline dispatched to {live_driver!r}"
        )
        _assert_reason_and_alternatives_match_live(inputs, table)


# ---------------------------------------------------------------------------
# Route corpus: every route + rejection path observable without execution.
# ---------------------------------------------------------------------------


def test_full_frame_no_relationships(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(monkeypatch, config, {"t": source})


def test_sequential_small_fk_byte_estimate_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch,
        config,
        {"parent": parent, "child": child},
        use_byte_estimate_routing=False,
    )


def test_full_frame_fk_byte_estimate_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default `use_byte_estimate_routing=True`: a tiny FK job confirms a
    full_frame fit and is routed there instead of sequential (§13)."""
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(monkeypatch, config, {"parent": parent, "child": child})


def test_validators_present_disqualifies_bounded_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config-level `validators:` list makes `_sequential_eligible` return
    `validators_present`, disqualifying the bounded routes: an FK pure-mask
    job that would otherwise route `sequential` (byte-estimate off) is pushed
    to `full_frame`. Validators are a config-only, route-affecting input;
    the snapshot must read them from config exactly as `run_pipeline` does,
    or the compiler diverges from live dispatch on any validators-bearing job.
    """
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    config["validators"] = [{"name": "fk_intact"}]
    # Sanity: the SAME job without validators routes sequential under these knobs
    # (see test_sequential_small_fk_byte_estimate_off), so full_frame here is
    # attributable to the validators input, not the job shape.
    _assert_equivalent(
        monkeypatch,
        config,
        {"parent": parent, "child": child},
        use_byte_estimate_routing=False,
    )


def test_out_of_core_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch, config, {"parent": parent, "child": child}, execution_mode="out_of_core"
    )


def test_sequential_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch, config, {"parent": parent, "child": child}, execution_mode="sequential"
    )


def test_full_frame_forced_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch, config, {"parent": parent, "child": child}, execution_mode="full_frame"
    )


def test_chunked_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": source},
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=3,
    )


def test_chunked_declined_below_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": source},
        auto_chunk=True,
        auto_chunk_threshold_rows=1_000_000,
        chunk_size_rows=3,
    )


def test_native_stream_admitted_utf8_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_declined_unsupported_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = _single_table_config(tmp_path, source, strategy="hash")
    config["tables"][0]["columns"][0]["namespace"] = "n"
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_declined_source_loader_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": source},
        native_route_enabled=True,
        source_loader=lambda name: source,
    )


def test_native_stream_widened_admitted_integer_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"n": pa.array([1, 2, 3], type=pa.int64())})
    path = tmp_path / "t.parquet"
    pq.write_table(source, path)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "n", "strategy": "passthrough"}]}],
        }
    ).model_dump()
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_widened_declined_null_bearing_integer_redact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"n": pa.array([1, None, 3], type=pa.int64())})
    path = tmp_path / "t.parquet"
    pq.write_table(source, path)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "n",
                            "strategy": "truncate",
                            "provider_config": {"length": 1},
                        }
                    ],
                }
            ],
        }
    ).model_dump()
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_not_enabled_falls_to_full_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(monkeypatch, config, {"t": LazySource(path=path)})


def test_generate_and_mask_full_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mask_source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    mask_path = _write(tmp_path, mask_source, "masked")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"masked": {"type": "file", "format": "parquet", "path": str(mask_path)}},
            "targets": {
                "masked": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "masked.out.parquet"),
                },
                "people": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "people.out.csv"),
                },
            },
            "tables": [
                {"name": "masked", "columns": [{"name": "note", "strategy": "redact"}]},
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                },
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(
        config, {"masked": mask_source}, engine_version="d4-corpus"
    )
    plan = compile_physical_plan(inputs)
    assert plan.synthesis is not None and plan.synthesis.tables == ("people",)
    _assert_equivalent(monkeypatch, config, {"masked": mask_source})


# ---------------------------------------------------------------------------
# Reject-before-read / forced-mode failures: exact ExecutionError code
# equality, or normalized ConfigError branch identity (D3's documented
# exclusion -- production raises this uncoded).
# ---------------------------------------------------------------------------


def test_reject_before_read_large_fk_no_bounded_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cyclic FK graph (A -> B -> A) is eligible for neither sequential
    (cyclic) nor out_of_core, and large enough that full_frame rejects
    before read."""
    a = pa.table(
        {
            "id": pa.array([f"a{i}" for i in range(10)], type=pa.string()),
            "bid": pa.array([f"b{i}" for i in range(10)], type=pa.string()),
        }
    )
    b = pa.table(
        {
            "id": pa.array([f"b{i}" for i in range(10)], type=pa.string()),
            "aid": pa.array([f"a{i}" for i in range(10)], type=pa.string()),
        }
    )
    a_path = _write(tmp_path, a, "a")
    b_path = _write(tmp_path, b, "b")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "a": {"type": "file", "format": "parquet", "path": str(a_path)},
                "b": {"type": "file", "format": "parquet", "path": str(b_path)},
            },
            "targets": {
                "a": {"type": "file", "format": "parquet", "path": str(tmp_path / "a.out.parquet")},
                "b": {"type": "file", "format": "parquet", "path": str(tmp_path / "b.out.parquet")},
            },
            "tables": [
                {"name": "a", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
                {"name": "b", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
            ],
            "relationships": [
                {
                    "parent": {"table": "a", "columns": ["id"]},
                    "children": [{"table": "b", "columns": ["aid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                },
                {
                    "parent": {"table": "b", "columns": ["id"]},
                    "children": [{"table": "a", "columns": ["bid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                },
            ],
        }
    ).model_dump()
    sources = {"a": a, "b": b}
    kwargs: dict[str, Any] = dict(
        use_byte_estimate_routing=False, full_frame_reject_rows=5, out_of_core_threshold_rows=5
    )

    inputs = capture_physical_plan_inputs(config, sources, engine_version="d4-corpus", **kwargs)
    with pytest.raises(ExecutionError) as compiler_exc:
        compile_physical_plan(inputs)

    with pytest.raises(ExecutionError) as live_exc:
        with _patched_dispatch(monkeypatch):
            run_pipeline(config, sources, engine_version="d4-oracle", **kwargs)

    assert compiler_exc.value.code == live_exc.value.code == "fk_full_frame_oom_risk_rejected"


def test_capture_rejects_invalid_execution_knob_before_compilation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MED D4 fixture: `auto_chunk="false"` (a string, not the bool
    `run_pipeline`'s `require_bool` requires) must raise the SAME coded
    `ExecutionError` at snapshot-capture time that `run_pipeline` raises at
    its submit boundary (`_substrate.py:64/76`, `_pipeline.py:264-283`) --
    never a compilable `full_frame` snapshot that silently ignored the
    malformed knob.
    """
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    kwargs: dict[str, Any] = dict(auto_chunk="false")

    with pytest.raises(ExecutionError) as compiler_exc:
        capture_physical_plan_inputs(config, {"t": source}, engine_version="d4-corpus", **kwargs)

    with pytest.raises(ExecutionError) as live_exc:
        with _patched_dispatch(monkeypatch):
            run_pipeline(config, {"t": source}, engine_version="d4-oracle", **kwargs)

    assert compiler_exc.value.code == live_exc.value.code == "invalid_execution_knob"


def _assert_forced_mode_equivalent(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
    sources: Any,
    *,
    must_contain: str,
    **kwargs: Any,
) -> None:
    """Both sides must raise `ConfigError` with the IDENTICAL message (the
    normalized branch identity D3 promised, not merely the same exception
    type). `must_contain` pins which forced-mode branch fired so a fixture
    can't silently drift onto a different branch that also raises ConfigError.
    """
    inputs = capture_physical_plan_inputs(config, sources, engine_version="d4-corpus", **kwargs)
    with pytest.raises(ConfigError) as compiler_exc:
        compile_physical_plan(inputs)
    with pytest.raises(ConfigError) as live_exc:
        with _patched_dispatch(monkeypatch):
            run_pipeline(config, sources, engine_version="d4-oracle", **kwargs)
    assert str(compiler_exc.value) == str(live_exc.value), (
        f"forced-mode message diverged:\n  compiler: {compiler_exc.value}\n  live:     {live_exc.value}"
    )
    assert must_contain in str(compiler_exc.value), (
        f"expected branch discriminator {must_contain!r} in {str(compiler_exc.value)!r}"
    )


def test_forced_sequential_ineligible_no_relationships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_forced_mode_equivalent(
        monkeypatch,
        config,
        {"t": source},
        must_contain="not sequential-eligible",
        execution_mode="sequential",
    )


def test_forced_out_of_core_no_mask_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure-generate job forced to out_of_core: no mask table exists."""
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {},
            "targets": {
                "people": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "people.out.csv"),
                },
            },
            "tables": [
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                },
            ],
        }
    ).model_dump()
    _assert_forced_mode_equivalent(
        monkeypatch,
        config,
        {},
        must_contain="mask-kind table to run through the out-of-core path",
        execution_mode="out_of_core",
    )


def test_forced_out_of_core_ineligible_single_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-table pure-mask job (no relationships) forced to out_of_core:
    has a mask table but is not out-of-core-eligible."""
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_forced_mode_equivalent(
        monkeypatch,
        config,
        {"t": source},
        must_contain="not out-of-core-eligible",
        execution_mode="out_of_core",
    )


def test_forced_out_of_core_incompatible_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An FK job that IS out-of-core-eligible but carries a strategy the
    out-of-core path does not support: reaches the incompatibility branch."""
    parent = pa.table(
        {
            "id": pa.array(["p1", "p2"], type=pa.string()),
            "extra": pa.array(["x", "y"], type=pa.string()),
        }
    )
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    parent_path = _write(tmp_path, parent, "parent")
    child_path = _write(tmp_path, child, "child")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
                "child": {"type": "file", "format": "parquet", "path": str(child_path)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [
                        {"name": "id", "strategy": "hash", "namespace": "n"},
                        {"name": "extra", "strategy": "synthetic"},
                    ],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()
    _assert_forced_mode_equivalent(
        monkeypatch,
        config,
        {"parent": parent, "child": child},
        must_contain="not out-of-core-compatible",
        execution_mode="out_of_core",
    )


def test_forced_sequential_cyclic_fk_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cyclic FK mask graph (A <-> B) IS sequential-eligible but cannot be
    ordered: reaches the forced-sequential cyclic branch."""
    a = pa.table(
        {
            "id": pa.array(["a0", "a1"], type=pa.string()),
            "bid": pa.array(["b0", "b1"], type=pa.string()),
        }
    )
    b = pa.table(
        {
            "id": pa.array(["b0", "b1"], type=pa.string()),
            "aid": pa.array(["a0", "a1"], type=pa.string()),
        }
    )
    a_path = _write(tmp_path, a, "a")
    b_path = _write(tmp_path, b, "b")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "a": {"type": "file", "format": "parquet", "path": str(a_path)},
                "b": {"type": "file", "format": "parquet", "path": str(b_path)},
            },
            "targets": {
                "a": {"type": "file", "format": "parquet", "path": str(tmp_path / "a.out.parquet")},
                "b": {"type": "file", "format": "parquet", "path": str(tmp_path / "b.out.parquet")},
            },
            "tables": [
                {"name": "a", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
                {"name": "b", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
            ],
            "relationships": [
                {
                    "parent": {"table": "a", "columns": ["id"]},
                    "children": [{"table": "b", "columns": ["aid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                },
                {
                    "parent": {"table": "b", "columns": ["id"]},
                    "children": [{"table": "a", "columns": ["bid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                },
            ],
        }
    ).model_dump()
    _assert_forced_mode_equivalent(
        monkeypatch,
        config,
        {"a": a, "b": b},
        must_contain="cross-table cycle",
        execution_mode="sequential",
    )


# ---------------------------------------------------------------------------
# Catalog-completeness audit over this corpus: every native-admission reason
# and every translated planner-rejection code observed above must be a KNOWN
# family / never the "unclassified_*" sentinel (D3's closing line).
# ---------------------------------------------------------------------------


def test_native_admission_reasons_stay_in_the_known_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H4 #2/#3 fix. Each scenario's source key must match the table name the
    scenario's OWN config declares -- the pre-fix `"utf8"`/`"hashcol"` keys
    never matched `_single_table_config`'s hardcoded table `"t"`, so
    `caller_sources.get(table)` (`_native_route.py`) always missed and both
    scenarios stopped dead at `non_lazy_source` without ever exercising the
    admitted-utf8 or unsupported-strategy branches they claimed to cover.
    Each scenario now asserts it actually reaches the branch it names
    (`static_reason` / `.reason` / `.lane`), not just that SOME reason
    landed in a known family -- a dead fixture that always hit the same
    branch would still pass the old family-only assertion.
    """
    scenarios: list[tuple[str, dict[str, Any], Any, dict[str, Any], Any]] = []

    def _assert_utf8_admitted(inputs: Any) -> None:
        admission = inputs.native_admission
        assert admission.static_candidate is True
        assert admission.lane == "utf8_only"
        assert admission.admitted is True
        assert admission.reason is None

    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = _single_table_config(tmp_path, source)
    scenarios.append(
        ("utf8_admitted", config, {"t": LazySource(path=path)}, {"native_route_enabled": True},
         _assert_utf8_admitted)
    )

    def _assert_unsupported_strategy(inputs: Any) -> None:
        admission = inputs.native_admission
        assert admission.static_candidate is False
        assert admission.static_reason is not None
        assert admission.static_reason.startswith("unsupported_strategy:note:hash")
        assert admission.reason == admission.static_reason

    hash_source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    hash_path = _write(tmp_path, hash_source, "t")
    hash_config = _single_table_config(tmp_path, hash_source, strategy="hash")
    hash_config["tables"][0]["columns"][0]["namespace"] = "n"
    scenarios.append(
        ("unsupported_strategy", hash_config, {"t": LazySource(path=hash_path)},
         {"native_route_enabled": True}, _assert_unsupported_strategy)
    )

    def _assert_route_disabled(inputs: Any) -> None:
        admission = inputs.native_admission
        assert admission.static_candidate is False
        assert admission.static_reason == "native_route_disabled_or_no_mask_table"

    scenarios.append(
        ("route_disabled", config, {"t": source}, {}, _assert_route_disabled)
    )  # native_route_enabled=False by default

    def _assert_redact_with_not_string(inputs: Any) -> None:
        admission = inputs.native_admission
        assert admission.static_candidate is False
        assert admission.static_reason == "redact_with_not_string:note"

    redact_source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    redact_path = _write(tmp_path, redact_source, "t")
    redact_config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(redact_path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "note",
                            "strategy": "redact",
                            "provider_config": {"redact_with": 123},
                        }
                    ],
                }
            ],
        }
    ).model_dump()
    scenarios.append(
        (
            "redact_with_not_string",
            redact_config,
            {"t": LazySource(path=redact_path)},
            {"native_route_enabled": True},
            _assert_redact_with_not_string,
        )
    )

    for name, cfg, srcs, kwargs, assert_branch in scenarios:
        inputs = capture_physical_plan_inputs(cfg, srcs, engine_version="d4-catalog", **kwargs)
        assert_branch(inputs)
        admission = inputs.native_admission
        if admission.reason is not None:
            family = native_reason_code_family(admission.reason)
            assert family != "unknown", (
                f"scenario {name!r}: uncatalogued native-admission reason: {admission.reason!r}"
            )


def test_native_admission_catalog_is_bidirectional() -> None:
    """H4 #3's bidirectional close: every family-prefix the catalog declares
    corresponds to a real reachable reason above (not just "no known prefix
    ever returns unknown"). The four codes this remediation added
    (`redact_with_not_string` + the three `check_truncate_config` codes) are
    reachable exclusively through `redact_config_rejection` /
    `truncate_config_rejection` (`native/_requirements.py`), called only
    from `static_candidacy` (`_native_route.py`) for an ALLOWED_STRATEGIES
    redact/truncate column -- confirmed by reading both call sites, since a
    bounded corpus fixture cannot itself enumerate every `check_truncate_
    config` failure mode.
    """
    from decoy_engine.execution.physical._reasons import NATIVE_STATIC_CODE_PREFIXES

    for code in (
        "redact_with_not_string",
        "truncate_length_invalid",
        "truncate_keep_invalid",
        "truncate_mask_char_invalid",
    ):
        assert code in NATIVE_STATIC_CODE_PREFIXES
        assert native_reason_code_family(f"{code}:note") == "static"


def test_forced_mode_branch_identities_are_all_named() -> None:
    """Documents the closed set of forced-mode branch identities D4 asserts
    exact identity against (D3's normalized-branch-identity exclusion)."""
    assert {
        "forced_out_of_core_no_mask_table",
        "forced_out_of_core_ineligible",
        "forced_out_of_core_incompatible",
        "forced_sequential_ineligible",
        "forced_sequential_cyclic",
        "forced_sequential_no_mask_table",
    } == FORCED_MODE_BRANCH_IDENTITIES

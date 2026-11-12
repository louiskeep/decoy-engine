"""AT-B1 to AT-B4 and AT-B6: ``build_run_result`` over a real run and over synthetic facts."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from decoy_engine import ExecutionError, run_pipeline
from decoy_engine.execution._pipeline_route_exec import execution_telemetry
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.run_result import (
    ConfigRef,
    EvidenceLink,
    FailureInput,
    Links,
    LogLink,
    NativeRoute,
    RunResultRead,
    build_run_result,
    project_tables,
)
from tests.unit.execution._transform_testkit import (
    ENGINE_VERSION,
    base_table,
    single_table_config,
    validated,
)
from tests.unit.run_result.test_outcome import EXIT_MAP

INV = "0b8e6f0e-6c1d-4c7e-9a3f-2f1f6f0b9d11"
SHA = "sha256:" + "a" * 64
NO_LINKS = Links(log=None, evidence=None, baseline=None)


def cfg(*names: str) -> dict:
    return {"tables": [{"name": n, "columns": []} for n in names]}


def fake(*, warnings=(), **metrics):
    """The part of an ``ExecutionResult`` the builder reads, with no table output."""
    return SimpleNamespace(outputs={}, row_errors=(), quality_metrics=metrics, warnings=warnings)


def build(*, projected=None, result=None, blocking=False, **over):
    projected = projected or project_tables(
        config=cfg("a"), execution_result=None, facts=None, delivery=None, run_failed=False
    )
    args = {
        "tables": projected,
        "execution_result": result,
        "failure": None,
        "config_ref": None,
        "producer": "0.1.0",
        "invocation_id": INV,
        "exit_map": EXIT_MAP,
        "blocking_failed": blocking,
        "native": None,
        "chunked": False,
        "links": NO_LINKS,
        "started_at": "2026-11-12T10:00:00.000000Z",
        "finished_at": "2026-11-12T10:00:01.500000Z",
        "elapsed_s": 1.5,
        "phases": (("resolve", 0.2),),
    }
    return build_run_result(**{**args, **over})


def test_success_two_tables(tmp_path) -> None:
    tbl = base_table()
    config = single_table_config(tmp_path, tbl)
    config["tables"].append(
        {
            "name": "gen",
            "row_count": 5,
            "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
        }
    )
    config["targets"]["gen"] = dict(config["targets"]["t"], path=str(tmp_path / "g.parquet"))
    config = validated(config)
    run = run_pipeline(config, sources={"t": tbl}, engine_version=ENGINE_VERSION)
    projected = project_tables(
        config=config, execution_result=run, facts=None, delivery=None, run_failed=False
    )
    payload = build(projected=projected, result=run).to_payload()
    RunResultRead.model_validate(payload)
    rows = payload["tables"]
    assert [r["name"] for r in rows] == ["t", "gen"]
    assert [r["kind"] for r in rows] == [run.table_kinds["t"], run.table_kinds["gen"]]
    assert [r["rows_out"] for r in rows] == [len(run.outputs["t"]), len(run.outputs["gen"])]
    assert payload["status"] == "succeeded"
    assert payload["exit_code"] == EXIT_MAP[("succeeded", None)]
    assert payload["pipeline_kind"] == "mixed"
    assert payload["route"]["execution_mode"] == "full_frame"
    assert payload["producer"]["cli_version"] == "0.1.0"
    assert payload["timings"]["phases"] == [{"name": "resolve", "elapsed_s": 0.2}]


@pytest.mark.parametrize(
    ("category", "status", "exit_code"),
    [
        ("usage", "failed", 21),
        ("parameter", "failed", 22),
        ("runtime", "failed", 23),
        ("capacity", "failed", 25),
        ("cancelled", "cancelled", 31),
    ],
)
def test_failure_input(category, status, exit_code) -> None:
    exc = ExecutionError(code="out_of_core_insufficient_memory", message="zz_marker_exc")
    record = reason_from_exception(exc)
    failure = FailureInput(category, record, type(exc).__name__)
    payload = build(failure=failure).to_payload()
    assert payload["failure"]["reason"] == record.to_payload()
    assert payload["failure"]["error_class"] == "ExecutionError"
    assert "zz_marker_exc" not in json.dumps(payload)
    assert (payload["status"], payload["exit_code"]) == (status, exit_code)


def test_failure_without_reason_and_hostile_class() -> None:
    payload = build(failure=FailureInput("usage", None, "ZzMarkerError")).to_payload()
    assert payload["failure"] == {"category": "usage", "error_class": "Exception", "reason": None}


def test_checks_value_free() -> None:
    metrics = {
        "quality_summary": {"sampled_values": {"c": ["zz_marker_a"]}},
        "failed_checks": [
            {"code": "dup_scan", "message": "zz_marker_b"},
            {"code": "has space", "message": "zz_marker_b"},
        ],
        "post_validation_enforce": True,
        "post_scan_integrity": {
            "pass_count": 5,
            "warning_count": 1,
            "fail_count": 0,
            "error_count": 0,
            "residual_pii": [{"detail": "zz_marker_a"}],
        },
        "fidelity_reports": {"t": {"detail": "zz_marker_a"}},
    }
    blocked = build(result=fake(**metrics), blocking=True).to_payload()
    checks = {c["id"]: c for c in blocked["checks"]}
    assert checks["post_validation"]["codes"] == ["dup_scan"]
    assert checks["post_validation"]["counts"] == {"pass": 0, "warning": 0, "fail": 1, "error": 0}
    assert checks["post_scan_integrity"]["status"] == "warned"
    assert checks["post_scan_integrity"]["counts"]["pass"] == 5
    assert checks["fidelity"]["status"] == "passed"
    assert checks["post_validation"]["blocking"] is True
    assert (blocked["status"], blocked["exit_code"]) == ("findings", EXIT_MAP[("findings", None)])
    # The caller decides blocking. Enforcement in the metrics does not.
    open_run = build(result=fake(**metrics), blocking=False).to_payload()
    assert all(c["blocking"] is False for c in open_run["checks"])
    assert open_run["status"] == "succeeded"
    assert "zz_marker" not in json.dumps([blocked, open_run])


def test_warnings_fold_and_stay_value_free() -> None:
    def warn(code, column=None, table=None):
        return SimpleNamespace(
            code=code, column=column, detail={"table": table, "value": "zz_marker_a"}
        )

    items = [
        warn("w_one", "email", "t"),
        warn("w_one", "email", "t"),
        warn("bad code"),
        warn("w_two", "a/b"),
    ]
    payload = build(result=fake(warnings=items)).to_payload()
    assert payload["warnings"] == [
        {"code": "w_one", "table": "t", "column": "email", "count": 2},
        {"code": "w_two", "table": None, "column": None, "count": 1},
    ]
    many = [warn(f"w_{i}") for i in range(300)]
    folded = build(result=fake(warnings=many)).to_payload()["warnings"]
    assert len(folded) == 256
    assert folded[-1] == {"code": "truncated", "table": None, "column": None, "count": 45}
    assert "zz_marker" not in json.dumps(payload)


def test_paths_and_native() -> None:
    native = NativeRoute(applicable=True, state="native")
    links = Links(
        log=LogLink(path="out/a.csv", sha256=SHA),
        evidence=EvidenceLink(manifest_hash=SHA),
        baseline=None,
    )
    config_ref = ConfigRef(name="claims.yaml", file_sha256=SHA, semantic_hash=None)
    payload = build(native=native, links=links, config_ref=config_ref).to_payload()
    assert payload["route"]["native"] == {"applicable": True, "state": "native"}
    assert payload["links"]["log"]["path"] == "out/a.csv"
    assert payload["config"]["name"] == "claims.yaml"
    # The model refuses what the builder would otherwise have to rewrite.
    for bad in ("/abs/a.csv", "../x.csv"):
        with pytest.raises(ValidationError):
            LogLink(path=bad, sha256=SHA)
    with pytest.raises(ValidationError):
        NativeRoute(applicable=True, state="native", label="zz_marker_a")


@pytest.mark.parametrize("route", ["full_frame", "sequential", "out_of_core"])
def test_route_values(route) -> None:
    # STEP ZERO: execution_telemetry is called with exactly these three route values.
    telemetry = execution_telemetry(
        route=route, route_reason="size", sink=None, source_loader=None, sources_resident=True
    )
    result = fake(execution=telemetry)
    got = build(result=result).to_payload()["route"]
    assert (got["execution_mode"], got["route_reason"]) == (route, "size")


@pytest.mark.parametrize("mode", ["mixed", "zz_future_route", 7])
def test_unknown_route_reads_other(mode) -> None:
    got = build(result=fake(execution={"execution_mode": mode})).to_payload()["route"]
    assert got["execution_mode"] == "other"


def test_route_absent_and_chunked() -> None:
    assert build().to_payload()["route"]["execution_mode"] is None
    chunked = build(result=fake(execution={"execution_mode": "full_frame"}), chunked=True)
    assert chunked.to_payload()["route"] == {
        "execution_mode": "chunked",
        "route_reason": None,
        "chunked": True,
        "native": None,
    }

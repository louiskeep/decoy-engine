"""AT-B1 to AT-B4 and AT-B6: ``build_run_result`` over a real run and over synthetic facts."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import decoy_engine
from decoy_engine import ExecutionError, reasons, route_summary, run_pipeline
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
    _build,
    build_run_result,
    project_tables,
)
from tests.unit.execution._transform_testkit import (
    ENGINE_VERSION,
    fk_config,
    fk_tables,
)
from tests.unit.run_result.test_outcome import EXIT_MAP

INV = "0b8e6f0e-6c1d-4c7e-9a3f-2f1f6f0b9d11"
SHA = "sha256:" + "a" * 64
NO_LINKS = Links(log=None, evidence=None, baseline=None)
W1 = str(reasons.warning.FORMAT_RULE_VIOLATION)
W2 = str(reasons.warning.CARDINALITY_MATCH_DEVIATION)
UNREGISTERED = str(reasons.decoy.UNREGISTERED_REASON)
ELIGIBLE = str(reasons.route.ELIGIBLE)


def cfg(*names: str) -> dict:
    return {"tables": [{"name": n, "columns": []} for n in names]}


def fake(*, warnings=(), **metrics):
    """The part of an ``ExecutionResult`` the builder reads, with no table output."""
    return SimpleNamespace(outputs={}, row_errors=(), quality_metrics=metrics, warnings=warnings)


def build(*, projected=None, result=None, blocking=False, **over):
    failed = over.get("failure") is not None
    projected = projected or project_tables(
        config=cfg("a"), execution_result=None, facts=None, delivery=None, run_failed=failed
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
        "route": None if result is None else route_summary(result.quality_metrics),
        "links": NO_LINKS,
        "started_at": "2026-11-12T10:00:00.000000Z",
        "finished_at": "2026-11-12T10:00:01.500000Z",
        "elapsed_s": 1.5,
        "phases": (("resolve", 0.2),),
    }
    return build_run_result(**{**args, **over})


def test_success_two_tables(tmp_path) -> None:
    parent, child = fk_tables()
    config = fk_config(tmp_path, parent, child)
    run = run_pipeline(
        config, sources={"parent": parent, "child": child}, engine_version=ENGINE_VERSION
    )
    assert len(config["relationships"]) == 1
    projected = project_tables(
        config=config, execution_result=run, facts=None, delivery=None, run_failed=False
    )
    payload = build(projected=projected, result=run).to_payload()
    RunResultRead.model_validate(payload)
    rows = payload["tables"]
    assert [r["name"] for r in rows] == ["parent", "child"]
    assert [r["kind"] for r in rows] == [run.table_kinds["parent"], run.table_kinds["child"]]
    assert [r["rows_out"] for r in rows] == [len(run.outputs["parent"]), len(run.outputs["child"])]
    assert payload["status"] == "succeeded"
    assert payload["exit_code"] == EXIT_MAP[("succeeded", None)]
    assert payload["pipeline_kind"] == "mask"
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


@pytest.mark.parametrize("run_failed", [True, False])
def test_failure_and_tables_must_agree(run_failed) -> None:
    projected = project_tables(
        config=cfg("a"), execution_result=None, facts=None, delivery=None, run_failed=run_failed
    )
    failure = None if run_failed else FailureInput("runtime", None, None)
    with pytest.raises(ValueError, match="run_failed"):
        build(projected=projected, failure=failure)


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
            "fail_count": 2,
            "error_count": 0,
            "residual_pii": [{"detail": "zz_marker_a"}],
        },
        "fidelity_reports": {"t": {"detail": "zz_marker_a"}},
    }
    blocked = build(result=fake(**metrics), blocking=True).to_payload()
    checks = {c["id"]: c for c in blocked["checks"]}
    assert checks["post_validation"]["codes"] == ["dup_scan"]
    # Codes and counts only: no message, no value.
    assert checks["post_validation"]["counts"]["fail"] == 1
    assert checks["post_scan_integrity"]["status"] == "failed"
    assert checks["post_scan_integrity"]["counts"]["pass"] == 5
    assert checks["post_scan_integrity"]["codes"] == []
    assert checks["fidelity"]["status"] == "passed"
    assert checks["post_validation"]["blocking"] is True
    # Post-scan is report-only on main, so only post_validation can block the run.
    assert checks["post_scan_integrity"]["blocking"] is False
    assert checks["fidelity"]["blocking"] is False
    assert (blocked["status"], blocked["exit_code"]) == ("findings", EXIT_MAP[("findings", None)])
    # The caller decides blocking. Enforcement in the metrics does not.
    open_run = build(result=fake(**metrics), blocking=False).to_payload()
    assert all(c["blocking"] is False for c in open_run["checks"])
    assert open_run["status"] == "succeeded"
    assert "zz_marker" not in json.dumps([blocked, open_run])


def _check_of(check_id, **metrics):
    payload = build(result=fake(**metrics)).to_payload()
    return {c["id"]: c for c in payload["checks"]}.get(check_id)


@pytest.mark.parametrize(
    ("scan", "status"),
    [
        ({"pass_count": 3}, "passed"),
        ({"pass_count": 3, "warning_count": 1}, "warned"),
        ({"pass_count": 3, "warning_count": 1, "fail_count": 1}, "failed"),
        ({"pass_count": 3, "fail_count": 1, "error_count": 1}, "error"),
        ({"pass_count": 3, "pass_failed_with": "RuntimeError"}, "error"),
    ],
)
def test_post_scan_status(scan, status) -> None:
    got = _check_of("post_scan_integrity", post_scan_integrity=scan)
    assert got["status"] == status
    assert "RuntimeError" not in json.dumps(got)


def test_post_validation_passed_and_absent() -> None:
    got = _check_of("post_validation", quality_summary={}, failed_checks=[])
    assert (got["status"], got["codes"]) == ("passed", [])
    assert _check_of("post_validation") is None
    assert _check_of("post_scan_integrity") is None
    assert _check_of("fidelity") is None


def test_failed_checks_non_mapping_row_is_skipped() -> None:
    rows = ["zz_marker_a", {"code": "dup_scan"}]
    got = _check_of("post_validation", quality_summary={}, failed_checks=rows)
    assert (got["status"], got["codes"]) == ("failed", ["dup_scan"])
    assert got["counts"]["fail"] == 2
    assert "zz_marker" not in json.dumps(got)


def test_fidelity_counts_tables() -> None:
    clean = {"grade": "A", "warnings": []}
    reports = {
        "t1": clean,
        "t2": {"grade": "B", "warnings": [{"code": "x", "message": "zz_marker_a"}]},
        "t3": {"grade": "F", "warnings": []},
        "t4": {"grade": "unavailable", "warnings": []},
    }
    got = _check_of("fidelity", fidelity_reports=reports)
    assert got["status"] == "warned"
    assert got["counts"] == {"pass": 1, "warning": 3, "fail": 0, "error": 0}
    assert "zz_marker" not in json.dumps(got)
    ok = _check_of("fidelity", fidelity_reports={"t1": clean, "t2": dict(clean, grade="C")})
    assert (ok["status"], ok["counts"]["pass"], ok["counts"]["warning"]) == ("passed", 2, 0)


def test_warnings_fold_and_stay_value_free() -> None:
    def warn(code, column=None, table=None):
        return SimpleNamespace(
            code=code, column=column, detail={"table": table, "value": "zz_marker_a"}
        )

    items = [
        warn(W1, "email", "t"),
        warn(W1, "email", "t"),
        warn("bad code"),
        warn("zz_not_registered"),
        warn(W2, "a/b"),
    ]
    payload = build(result=fake(warnings=items)).to_payload()
    # An unregistered or malformed code keeps its count under the one fixed code.
    assert payload["warnings"] == [
        {"code": W1, "table": "t", "column": "email", "count": 2},
        {"code": UNREGISTERED, "table": None, "column": None, "count": 2},
        {"code": W2, "table": None, "column": None, "count": 1},
    ]
    assert "zz_marker" not in json.dumps(payload)
    assert "zz_not_registered" not in json.dumps(payload)


def _fold(items):
    payload = build(result=fake(warnings=items)).to_payload()
    return payload["warnings"]


def _warn(code, column=None, table=None):
    return SimpleNamespace(code=code, column=column, detail={"table": table})


def test_fold_levels_keep_every_count() -> None:
    # Level 1: 256 distinct keys fit unchanged.
    fits = [_warn(W1, f"c{i}", "t") for i in range(256)]
    assert len(_fold(fits)) == 256
    # Level 2: over 256 columns of one code and table collapse to one entry. 257 is the edge.
    edge = [_warn(W1, f"c{i}", "t") for i in range(257)]
    assert _fold(edge) == [{"code": W1, "table": "t", "column": None, "count": 257}]
    columns = [_warn(W1, f"c{i}", "t") for i in range(300)]
    assert _fold(columns) == [{"code": W1, "table": "t", "column": None, "count": 300}]
    # Level 3: over 256 tables collapse by code alone.
    tables = [_warn(W1, "c", f"t{i}") for i in range(300)] + [_warn(W2, "c", "t")]
    got = _fold(tables)
    assert got == [
        {"code": W1, "table": None, "column": None, "count": 300},
        {"code": W2, "table": None, "column": None, "count": 1},
    ]
    for items in (fits, edge, columns, tables):
        assert sum(w["count"] for w in _fold(items)) == len(items)


def test_equal_counts_keep_first_seen_order() -> None:
    got = _fold([_warn(W2, "b"), _warn(W1, "a"), _warn(W2, "b"), _warn(W1, "a")])
    assert [(w["code"], w["count"]) for w in got] == [(W2, 2), (W1, 2)]


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
        route=route, route_reason=ELIGIBLE, sink=None, source_loader=None, sources_resident=True
    )
    got = build(result=fake(execution=telemetry)).to_payload()["route"]
    assert (got["execution_mode"], got["route_reason"], got["chunked"]) == (route, ELIGIBLE, False)


@pytest.mark.parametrize("name", ["mixed", "zz_future_route"])
def test_unknown_route_reads_other(name) -> None:
    got = build(result=fake(execution={"execution_mode": name})).to_payload()["route"]
    assert got["execution_mode"] == "other"


def test_route_absent_and_chunked() -> None:
    assert build().to_payload()["route"]["execution_mode"] is None
    # The --chunked flag is a caller fact: that path has no quality_metrics route at all.
    flagged = build(chunked=True).to_payload()["route"]
    assert (flagged["execution_mode"], flagged["chunked"]) == ("chunked", True)
    both = build(result=fake(execution={"execution_mode": "full_frame"}), chunked=True)
    assert both.to_payload()["route"] == {
        "execution_mode": "chunked",
        "route_reason": None,
        "chunked": True,
        "native": None,
    }


def test_unregistered_route_reason_is_replaced() -> None:
    got = build(result=fake(execution={"execution_mode": "full_frame", "route_reason": "zz_x"}))
    assert got.to_payload()["route"]["route_reason"] == UNREGISTERED


# The metrics the engine writes for each route, as `route_summary` reads them.
_CORPUS = [
    (
        {"execution": {"execution_mode": "full_frame", "route_reason": ELIGIBLE}},
        "full_frame",
        False,
    ),
    ({"execution": {"execution_mode": "sequential"}}, "sequential", False),
    ({"execution": {"execution_mode": "out_of_core"}}, "out_of_core", False),
    # The auto-chunk lane streams a run the full-frame telemetry still calls full_frame.
    (
        {
            "execution": {"execution_mode": "full_frame"},
            "auto_chunk": {"lane": "dispatcher", "lane_reason": ELIGIBLE},
        },
        "chunked",
        True,
    ),
    ({"execution": {"execution_mode": "mixed"}}, "other", False),
    ({}, None, False),
]


@pytest.mark.parametrize(("metrics", "mode", "chunked"), _CORPUS)
def test_route_parity_with_route_summary(metrics, mode, chunked) -> None:
    got = build(result=fake(**metrics)).to_payload()["route"]
    assert (got["execution_mode"], got["chunked"]) == (mode, chunked)


def _route_names_the_engine_writes() -> set[str]:
    """Every route name `route_summary` can return: the telemetry routes, mixed, and the two
    names it adds itself."""
    from decoy_engine.execution._pipeline_mixed_route import ROUTE_NAME
    from decoy_engine.execution.pipeline._route_summary import ROUTE_AUTO_CHUNK, ROUTE_UNKNOWN

    names = {ROUTE_NAME, ROUTE_AUTO_CHUNK, ROUTE_UNKNOWN}
    for path in Path(decoy_engine.__file__).parent.joinpath("execution").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "execution_telemetry"
            ):
                for kw in node.keywords:
                    if kw.arg == "route" and isinstance(kw.value, ast.Constant):
                        names.add(str(kw.value.value))
    return names


def test_every_route_name_is_decided_on_purpose() -> None:
    undecided = _route_names_the_engine_writes() - set(_build._ROUTE_MODE)
    assert not undecided, f"route_summary can return a route the builder does not map: {undecided}"

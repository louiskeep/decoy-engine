"""F06 r3: a declared job kept off a route by its contract says so with the registered F29 refusal.

Each probe is a job where the operator could expect a different (bounded) route. The run still
falls back where fallback is the accepted contract, but the result carries the machine-readable,
value-free refusal: ``quality_metrics["execution"]["declared_route_refusals"]`` lists
``{route, namespace, code, missing}`` entries taken from the route's own admission decision.
Forced modes keep refusing outright (see test_f06_route_parity).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance import test_f06_route_parity as parity
from tests.acceptance._f06_support import (
    INT64,
    PROTECTED_QUARANTINE,
    column,
    run,
    text_table,
    write_csv,
    zero_pad,
)

CODE = "declared_cast_route_unsupported"
KEY = "declared_route_refusals"


def _refusals(result: Any) -> dict[str, dict[str, Any]]:
    entries = result.quality_metrics["execution"].get(KEY, [])
    return {e["route"]: e for e in entries}


def _assert_refusal(result: Any, route: str, code: str = CODE) -> dict[str, Any]:
    found = _refusals(result)
    assert route in found, f"no registered refusal for {route!r}: {sorted(found)}"
    entry = found[route]
    assert (entry["namespace"], entry["code"]) == ("route", code)
    assert all(isinstance(m, str) for m in entry["missing"])
    if code == CODE:
        assert entry["missing"], "a capability refusal names what the route lacks"
    from decoy_engine import reasons

    assert reasons.is_registered(entry["code"], ["route"])
    # Value-free: only fixed identifiers, never a cell value or a path.
    text = json.dumps(result.quality_metrics["execution"][KEY])
    for leaked in ("042", "bad", "2026", "/tmp", "pytest"):
        assert leaked not in text
    return entry


def test_mixed_with_row_quarantine_reports_the_refused_mixed_route(tmp_path: Path) -> None:
    from tests.parity import _f2_mixed_support as mx

    cfg, sources = parity._mixed(tmp_path, "quarantine", coupled=True)
    got = mx.run(cfg, sources, quarantine_runtime=parity._runtime(tmp_path / "q-mixed"))
    assert got.quality_metrics["execution"]["route_reason"] == "generate_plus_mask"
    entry = _assert_refusal(got, "mixed")
    assert "row_quarantine" in entry["missing"]


def test_declared_relationship_keys_report_the_declined_unified_slice(tmp_path: Path) -> None:
    got = run(
        parity._fk_cfg(tmp_path, endpoints=True),
        parity._fk_sources(),
        fidelity_report=True,  # keeps the bounded sequential route out, so full-frame runs
    )
    assert got.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert "unified_slice_activation" not in got.quality_metrics
    _assert_refusal(got, "unified_slice")


def test_auto_relationship_routing_reports_the_refused_out_of_core_route(tmp_path: Path) -> None:
    got = run(
        parity._fk_cfg(tmp_path, endpoints=True),
        parity._fk_sources(),
        out_of_core_threshold_rows=1,
    )
    assert got.quality_metrics["execution"]["execution_mode"] == "sequential"
    _assert_refusal(got, "out_of_core")


def _two_tables(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    a = write_csv(tmp_path / "a.csv", ["id", "age"], [["1", "2"]])
    b = write_csv(tmp_path / "b.csv", ["id", "name"], [["1", "x"]])
    age = column(
        "age",
        logical_type="integer",
        parse_format=INT64,
        error_policy="quarantine",
        output_format=zero_pad(3),
    )
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "a": {"type": "file", "format": "csv", "path": a},
            "b": {"type": "file", "format": "csv", "path": b},
        },
        "tables": [
            {"name": "a", "columns": [column("id"), age]},
            {"name": "b", "columns": [column("id"), column("name")]},
        ],
        "targets": {
            "a": {"type": "file", "format": "csv", "path": a + ".o"},
            "b": {"type": "file", "format": "csv", "path": b + ".o"},
        },
        "relationships": [],
        "namespaces": {},
        "protected_quarantine": PROTECTED_QUARANTINE,
    }
    sources = {
        "a": text_table(id=["1", "2", "3", "4"], age=["1", "x", "3", "4"]),
        "b": text_table(id=["1", "2", "3", "4"], name=list("wxyz")),
    }
    return cfg, sources


def test_declared_multi_table_split_exclusion_names_the_registered_refusal(
    tmp_path: Path,
) -> None:
    cfg, sources = _two_tables(tmp_path)
    got = run(
        cfg,
        sources,
        auto_chunk=True,
        chunk_size_rows=2,
        auto_chunk_threshold_rows=1,
        explain_plan=True,
        quarantine_runtime=parity._runtime(tmp_path / "q-multi"),
    )
    assert got.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    _assert_refusal(got, "multi_table_split", "declared_casting_full_frame")
    # The explain block carries the same registered evidence as the route telemetry.
    assert got.quality_metrics["execution_plan"][KEY] == got.quality_metrics["execution"][KEY]


def test_a_single_table_declared_job_claims_no_route_it_was_never_offered(tmp_path: Path) -> None:
    got = run(
        parity._cfg(tmp_path, "quarantine"),
        {"people": parity._source()},
        execution_mode="full_frame",
        auto_chunk=False,
        quarantine_runtime=parity._runtime(tmp_path / "q-single"),
    )
    assert set(_refusals(got)) <= {"unified_slice"}  # only lanes that were actually candidates


def test_an_undeclared_job_keeps_its_telemetry_shape(tmp_path: Path) -> None:
    src = write_csv(tmp_path / "u.csv", ["id"], [["1"]])
    from tests.acceptance._f06_support import config

    got = run(config([column("id")], source_path=src), {"people": text_table(id=["1", "2"])})
    assert KEY not in got.quality_metrics["execution"]


@pytest.mark.parametrize("mode", ["out_of_core"])
def test_forced_modes_still_reject_instead_of_falling_back(tmp_path: Path, mode: str) -> None:
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        run(
            parity._fk_cfg(tmp_path, endpoints=True),
            parity._fk_sources(),
            execution_mode=mode,
        )
    assert exc.value.code == CODE

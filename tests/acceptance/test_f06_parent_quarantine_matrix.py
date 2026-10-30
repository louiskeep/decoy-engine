"""Every relational cell of a quarantined parent row ends in a refusal or ``orphan_fk_violation``.

Axes are generated from the code (``_f06_relational_matrix``): each route lane under ``auto``
and, where the lane has one, its forced mode; every FK shape; every orphan policy the engine
has; both failure stages. A cell may end in exactly one of two ways: the registered refusal
before any route runs, or (parse stage, ``orphan_policy: fail``) ``orphan_fk_violation`` on the
route that actually executed. No cell may publish, and no surface may carry the raw key of the
quarantined parent (the seeded marker).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from tests.acceptance import _f06_relational_matrix as rel
from tests.acceptance._f06_support import run
from tests.acceptance.test_f06_route_parity import _runtime

REQUESTS = rel.requests()
POLICIES = rel.orphan_policies()

# The lane that really executes each request for a relational, quarantining job. Written out,
# not read back: a router change that moves a request to another lane must change this table.
# Unified slice, chunked, mixed and the multi-table split decline relationship jobs, and the
# out-of-core lane cannot quarantine rows, so ``auto`` lands on sequential for all of them.
EXECUTED: dict[str, str | None] = {
    **{f"{lane}-auto": "sequential" for lane in rel.route_lanes()},
    "full_frame-full_frame": "full_frame",
    "sequential-sequential": "sequential",
    "out_of_core-out_of_core": None,  # refused before routing: lacks row_quarantine
}
REFUSAL = "declared_cast_route_unsupported"


def test_axes_are_complete() -> None:
    assert {r.id for r in REQUESTS} == set(EXECUTED)
    assert set(POLICIES) == {"preserve", "remap", "warn", "fail"}  # no cascade in the engine


def test_shapes_express_every_fk_shape(tmp_path: Path) -> None:
    jobs = {}
    for name, shape in rel.SHAPES.items():
        (tmp_path / name).mkdir()
        jobs[name] = shape(tmp_path / name, "parse", "fail", "5", "quarantine")
    assert rel.shape_coverage(jobs) == {
        "single_column",
        "composite",
        "self_referencing",
        "cross_declared",
        "later_chunk",
    }


def _route_events(events: list[Any]) -> list[str]:
    out = []
    for e in events:
        if type(e).__name__ == "RouteEvent":
            out.append("chunked" if e.lane == "chunked" else str(e.route))
    return out


def _attempt(
    root: Path, req: rel.Request, shape: str, stage: str, policy: str, bad: str
) -> tuple[Any, BaseException | None, list[Any], rel.Job]:
    from decoy_engine.progress import CallbackSink

    root.mkdir(parents=True, exist_ok=True)
    job = rel.SHAPES[shape](root, stage, policy, bad, "quarantine")
    events: list[Any] = []
    try:
        result = run(
            job.config,
            job.sources,
            quarantine_runtime=_runtime(root / "q"),
            progress=CallbackSink(events.append),
            **req.kwargs(),
        )
    except Exception as exc:  # every outcome is classified by the caller
        return None, exc, events, job
    return result, None, events, job


def _orphans(result: Any, job: rel.Job) -> int:
    ptab, pcols = job.parent
    ctab, ccols = job.child
    keys = set(zip(*[result.outputs[ptab].column(c).to_pylist() for c in pcols], strict=True))
    refs = zip(*[result.outputs[ctab].column(c).to_pylist() for c in ccols], strict=True)
    return sum(1 for k in refs if None not in k and k not in keys)


@pytest.mark.parametrize("shape", list(rel.SHAPES))
@pytest.mark.parametrize("req", REQUESTS, ids=lambda r: r.id)
def test_valid_control_runs_on_the_recorded_lane(
    tmp_path: Path, req: rel.Request, shape: str
) -> None:
    """Same contract and relationship, no bad cell: pins the lane each request really takes."""
    result, exc, events, _ = _attempt(tmp_path, req, shape, "parse", "fail", "5")
    expected = EXECUTED[req.id]
    if expected is None:
        assert exc is not None and str(getattr(exc, "code", "")) == REFUSAL
        assert _route_events(events) == []
        return
    assert exc is None, exc
    assert _route_events(events) == [expected]
    assert rel.executed_route(result.quality_metrics) == expected


def _expected(req: rel.Request, stage: str, policy: str) -> tuple[str, str | None]:
    """(code, capability named in the refusal) for one cell, from the rule, not the engine."""
    if EXECUTED[req.id] is None:
        return REFUSAL, "row_quarantine"
    if stage == "format":
        return REFUSAL, "post_mask_parent_row_quarantine"
    if policy != "fail":
        return REFUSAL, "pre_mask_parent_row_quarantine"
    return "orphan_fk_violation", None


@pytest.mark.parametrize("stage", rel.STAGES)
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("shape", list(rel.SHAPES))
@pytest.mark.parametrize("req", REQUESTS, ids=lambda r: r.id)
def test_quarantined_parent_cell_refuses_or_raises_orphan(
    tmp_path: Path, req: rel.Request, shape: str, policy: str, stage: str
) -> None:
    from decoy_engine.reasons import is_registered

    result, exc, events, job = _attempt(tmp_path, req, shape, stage, policy, rel.BAD_CELL[stage])
    if exc is None:
        pytest.fail(
            f"published on {_route_events(events)}: {_orphans(result, job)} orphan(s), "
            f"completeness={result.quality_metrics['declared_casting'].get('completeness')}"
        )
    code, capability = _expected(req, stage, policy)
    assert str(getattr(exc, "code", type(exc).__name__)) == code
    assert is_registered(code)
    message = str(exc)
    assert rel.MARKER not in message and rel.BAD_CELL[stage] not in message
    if capability is None:
        # Non-vacuity: the orphan check ran on the lane the control pinned, after data was read.
        assert _route_events(events) == [EXECUTED[req.id]]
    else:
        assert capability in message
        assert _route_events(events) == []  # refused before route selection
    for table in job.config["targets"].values():
        assert not Path(table["path"]).exists()


def _surfaces(
    root: Path, result: Any, exc: BaseException | None, events: list[Any], logs: str, job: rel.Job
) -> dict[str, bytes]:
    from decoy_engine.progress import event_to_dict

    found: dict[str, bytes] = {
        "events": json.dumps([event_to_dict(e) for e in events], default=str).encode(),
        "logs": logs.encode(),
    }
    if exc is not None:
        found["error"] = f"{exc!s}{exc!r}{getattr(exc, '__notes__', '')}".encode()
    if result is not None:
        found["quality_metrics"] = json.dumps(result.quality_metrics, default=str).encode()
        found["warnings"] = repr(result.warnings).encode()
        for name, table in result.outputs.items():
            found[f"output:{name}"] = json.dumps(table.to_pylist(), default=str).encode()
    inputs = {Path(s["path"]) for s in job.config["sources"].values()}
    for path in root.rglob("*"):
        if path.is_file() and path not in inputs:
            found[f"file:{path.relative_to(root)}"] = path.read_bytes()
    return found


def _leaks(found: dict[str, bytes]) -> list[str]:
    return sorted(k for k, v in found.items() if rel.MARKER.encode() in v)


@pytest.mark.parametrize("stage", rel.STAGES)
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("shape", list(rel.SHAPES))
@pytest.mark.parametrize("req", REQUESTS, ids=lambda r: r.id)
def test_no_surface_carries_the_quarantined_parent_key(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    req: rel.Request,
    shape: str,
    policy: str,
    stage: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    result, exc, events, job = _attempt(tmp_path, req, shape, stage, policy, rel.BAD_CELL[stage])
    found = _surfaces(tmp_path, result, exc, events, caplog.text, job)
    assert "events" in found and "logs" in found
    assert _leaks(found) == []


def test_marker_scan_sees_a_published_raw_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-vacuity: a genuine source orphan under ``preserve`` is published verbatim by design,
    so the same scan must find the marker in the child output."""
    from decoy_engine.progress import CallbackSink

    caplog.set_level(logging.DEBUG)
    job = rel.single(tmp_path, "parse", "preserve", "5", "null")
    job.sources["parent"] = job.sources["parent"].slice(1)
    pq.write_table(job.sources["parent"], job.config["sources"]["parent"]["path"])
    events: list[Any] = []
    result = run(job.config, job.sources, progress=CallbackSink(events.append))
    found = _surfaces(tmp_path, result, None, events, caplog.text, job)
    assert "output:child" in _leaks(found)

"""R3 acceptance test 7: Hypothesis routing parity, and stamp-absence on the default path.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 7.

Generated jobs (three shape families, thresholds pinned small so the boundary cases are
cheap) run through the public `run_pipeline`. Two independent references judge each one:

* `expected_outcome` is a literal decision table written in THIS file. It does not import
  `_pipeline_routing` or any routing helper, so a routing drift makes expected != observed.
  It maps a case to either an expected `(layer-1 route, chunked?)` or an expected rejection
  code.
* `PandasExecutionAdapter.run` is the output oracle, the same one the parity harnesses use.

For a case the table marks SUCCESSFUL: the output equals the oracle's (under the two documented
`SEMANTIC_DIFFERENCES.md` normalizations: Arrow width drift folds in `to_pydict`, NaN folds to
null), and BOTH routing layers that actually ran match the table (`execution_mode`, plus the
`route_chunked` the generate+mask step received and the `auto_chunk` evidence). For a case it marks
REJECTED: the exact code is raised and nothing was published.

Routing is a pure function of the generated inputs here: byte-estimate and probe routing are
pinned off (byte estimates bypass the row thresholds and would test the wrong boundary).
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from decoy_engine.execution import (
    ExecutionError,
    PandasExecutionAdapter,
    ParquetTransactionalSink,
    run_pipeline,
)
from decoy_engine.execution import _pipeline_generate_mask as gen_mask

pytestmark = pytest.mark.filterwarnings("ignore")

# Small thresholds so a boundary case is a handful of rows. Each family has its own.
CHUNK_AT = 10  # auto_chunk_threshold_rows (single-table family)
OOC_AT = 10  # out_of_core_threshold_rows (bounded FK family)
REJECT_AT = 10  # full_frame_reject_rows (full-frame-only FK family)
HUGE = 10**9

FAMILIES = ("single", "bounded_fk", "full_frame_only_fk")
STRING_OPS = ("hash", "redact", "truncate", "passthrough")


@dataclass(frozen=True)
class Case:
    family: str
    op: str
    wide_string: bool
    int_kind: str  # "int64" | "int64_nullable" | "bool"
    with_faker: bool
    with_fpe: bool
    with_date_shift: bool
    data: str  # "present" | "nulls" | "empty" | "single"
    delta: int  # rows relative to the family's threshold
    seed: int
    auto_chunk: bool
    unified: bool


def rows_of(case: Case) -> int:
    if case.data == "empty":
        return 0
    if case.data == "single":
        return 1
    at = {"single": CHUNK_AT, "bounded_fk": OOC_AT, "full_frame_only_fk": REJECT_AT}[case.family]
    return max(0, at + case.delta)


# ---------------------------------------------------------------------------
# The independent decision table. Literal on purpose: no routing helper is consulted.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expected:
    route: str | None = None
    chunked: bool = False
    reject_code: str | None = None


def expected_outcome(case: Case) -> Expected:
    n = rows_of(case)
    if case.family == "single":
        # No relationships: layer 1 is always full_frame. Layer 2 chunks a single mask table
        # whose rows reach the auto-chunk threshold, when auto_chunk is on, unless a column has
        # a non-chunk-stable pandas round-trip dtype: an integer column that really holds a null
        # (the planner's documented "integer with nulls" rejection).
        has_int_null = case.int_kind == "int64_nullable" and n >= 1
        return Expected(
            route="full_frame", chunked=case.auto_chunk and n >= CHUNK_AT and not has_int_null
        )
    if case.family == "bounded_fk":
        # A pure-mask FK job every route supports: out_of_core at/above its threshold, else
        # sequential. Relationship jobs never chunk.
        return Expected(route="out_of_core" if n >= OOC_AT else "sequential")
    # A generate+mask FK job no bounded route admits: at/above the reject threshold it must
    # raise before execution; below it, full_frame.
    if n >= REJECT_AT:
        return Expected(reject_code="fk_full_frame_oom_risk_rejected")
    return Expected(route="full_frame")


# ---------------------------------------------------------------------------
# Building the job
# ---------------------------------------------------------------------------


def _strings(n: int, prefix: str, nulls: bool, wide: bool) -> pa.Array:
    vals: list[str | None] = [
        None if (nulls and i % 4 == 0) else f"{prefix}{i}@x.example" for i in range(n)
    ]
    return pa.array(vals, type=pa.large_string() if wide else pa.string())


def _op_column(name: str, op: str) -> dict[str, Any]:
    if op == "hash":
        return {"name": name, "strategy": "hash", "namespace": f"ns_{name}"}
    if op == "truncate":
        return {"name": name, "strategy": "truncate", "provider_config": {"length": 3}}
    return {"name": name, "strategy": op}


def build(case: Case, tmp: Path) -> tuple[dict[str, Any], dict[str, pa.Table], dict[str, Any]]:
    n = rows_of(case)
    nulls = case.data == "nulls"
    base: dict[str, Any] = {"version": 1, "global_settings": {"seed": case.seed}}
    if case.family == "single":
        cols: dict[str, pa.Array] = {"s": _strings(n, "u", nulls, case.wide_string)}
        specs = [_op_column("s", case.op)]
        if case.int_kind == "bool":
            cols["n"] = pa.array([i % 2 == 0 for i in range(n)], type=pa.bool_())
        else:
            ints = [
                None if (case.int_kind == "int64_nullable" and i % 3 == 0) else i for i in range(n)
            ]
            cols["n"] = pa.array(ints, type=pa.int64())
        specs.append({"name": "n", "strategy": "passthrough"})
        if case.with_faker:
            cols["f"] = pa.array([f"name{i}" for i in range(n)])
            specs.append(
                {
                    "name": "f",
                    "strategy": "faker",
                    "provider": "person_first_name",
                    "deterministic": True,
                    "namespace": "ns_f",
                    "pool_size": 40,
                }
            )
        if case.with_fpe:
            cols["e"] = pa.array([f"{100_000_000 + i}" for i in range(n)])
            specs.append(
                {
                    "name": "e",
                    "strategy": "fpe",
                    "namespace": "ns_e",
                    "provider_config": {"charset": "digits"},
                }
            )
        if case.with_date_shift:
            cols["d"] = pa.array([f"2020-01-{1 + i % 28:02d}" for i in range(n)])
            specs.append(
                {
                    "name": "d",
                    "strategy": "date_shift",
                    "namespace": "ns_d",
                    "provider_config": {"min_days": -3, "max_days": 3, "date_format": "%Y-%m-%d"},
                }
            )
        tables = {"t": pa.table(cols)}
        base["tables"] = [{"name": "t", "columns": specs}]
        kwargs = {"auto_chunk_threshold_rows": CHUNK_AT, "chunk_size_rows": 4}
    else:
        ids = [f"p{i}" for i in range(n)]
        parent: dict[str, pa.Array] = {"id": pa.array(ids, type=pa.string())}
        child: dict[str, pa.Array] = {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "parent_id": pa.array(ids, type=pa.string()),
        }
        parent_specs: list[dict[str, Any]] = [_op_column("id", "hash") | {"namespace": "ns"}]
        if case.family == "bounded_fk":
            parent["note"] = _strings(n, "n", nulls, case.wide_string)
            note_op = case.op if case.op != "hash" else "redact"
            parent_specs.append(_op_column("note", note_op))
        tables = {"parent": pa.table(parent), "child": pa.table(child)}
        base["tables"] = [
            {"name": "parent", "columns": parent_specs},
            {
                "name": "child",
                "columns": [
                    _op_column("cid", "hash"),
                    _op_column("parent_id", "hash") | {"namespace": "ns"},
                ],
            },
        ]
        base["relationships"] = [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ]
        if case.family == "bounded_fk":
            kwargs = {"out_of_core_threshold_rows": OOC_AT}
        else:
            base["tables"].append(
                {
                    "name": "extra",
                    "row_count": 3,
                    "generate_columns": [
                        {"name": "seq", "type": "sequence", "start": 1, "step": 1}
                    ],
                }
            )
            kwargs = {"full_frame_reject_rows": REJECT_AT}
    base["sources"] = {}
    base["targets"] = {}
    for name, table in tables.items():
        path = tmp / f"{name}.parquet"
        pq.write_table(table, path)
        base["sources"][name] = {"type": "file", "format": "parquet", "path": str(path)}
    for name in [*tables, *(["extra"] if case.family == "full_frame_only_fk" else [])]:
        base["targets"][name] = {
            "type": "file",
            "format": "parquet",
            "path": str(tmp / f"{name}.out.parquet"),
        }
    return base, tables, kwargs


# ---------------------------------------------------------------------------
# Oracle and comparison
# ---------------------------------------------------------------------------


def oracle_outputs(cfg: dict[str, Any], tables: dict[str, pa.Table]) -> dict[str, pa.Table]:
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    profile = profile_source(cfg, seed=cfg["global_settings"]["seed"])
    plan = compile_plan(cfg, profile, decoy_engine_version="r3-hypothesis")
    ns = build_namespace_registry(cfg, profile)
    if profile.relationships:
        graph = build_relationship_graph(
            profile.relationships,
            namespace_registry=ns,
            orphan_policy_lookup=check_orphan_fk_policy_completeness(cfg, profile.relationships),
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    mask_only = {
        n: t
        for n, t in tables.items()
        if any(x["name"] == n and "columns" in x for x in cfg["tables"])
    }
    result = PandasExecutionAdapter().run(
        plan,
        mask_only,
        registry=get_default_registry(),
        relationship_graph=graph,
        namespace_registry=ns,
    )
    return dict(result.outputs)


def _fold(value: object) -> object:
    return None if isinstance(value, float) and math.isnan(value) else value


def comparable(table: pa.Table) -> dict[str, list[object]]:
    """`to_pydict` folds Arrow width drift; NaN folds to null (the two documented normalizations)."""
    return {name: [_fold(v) for v in col] for name, col in table.to_pydict().items()}


class LaneSpy:
    """The `route_chunked` the generate+mask step actually received (absent: that step never ran)."""

    def __init__(self) -> None:
        self.route_chunked: list[bool] = []
        self._real = gen_mask.run_generate_and_mask_steps

    def __enter__(self) -> LaneSpy:
        spy = self

        def wrapped(**kw: Any) -> Any:
            spy.route_chunked.append(kw["route_chunked"])
            return spy._real(**kw)

        gen_mask.run_generate_and_mask_steps = wrapped  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        gen_mask.run_generate_and_mask_steps = self._real  # type: ignore[assignment]


def check_case(case: Case) -> None:
    want = expected_outcome(case)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, tables, route_kwargs = build(case, tmp)
        sink_dir = tmp / "sink_out"
        run_kwargs: dict[str, Any] = {
            "engine_version": "r3-hypothesis",
            "use_byte_estimate_routing": False,
            "use_probe_routing": False,
            "auto_chunk": case.auto_chunk,
            "unified_slice_enabled": case.unified,
            **route_kwargs,
        }
        if want.reject_code is not None:
            with pytest.raises(ExecutionError) as raised:
                run_pipeline(cfg, tables, sink=ParquetTransactionalSink(sink_dir), **run_kwargs)
            assert raised.value.code == want.reject_code
            assert not sink_dir.exists(), "a rejected job must not publish"
            return
        try:
            reference = oracle_outputs(cfg, tables)
        except Exception as exc:  # the oracle itself refuses this shape: the pipeline must too
            with pytest.raises(type(exc)):
                run_pipeline(cfg, tables, **run_kwargs)
            return
        with LaneSpy() as lane:
            result = run_pipeline(cfg, tables, **run_kwargs)
    qm = result.quality_metrics
    assert qm["execution"]["execution_mode"] == want.route  # layer 1 that ran
    if lane.route_chunked:  # layer 2 that ran (the step only runs on the full_frame executor)
        assert lane.route_chunked == [want.chunked]
    else:
        assert want.chunked is False
    assert (qm.get("auto_chunk", {}).get("mode") == "chunked") is want.chunked
    for name, table in reference.items():
        assert comparable(result.outputs[name]) == comparable(table), name


# ---------------------------------------------------------------------------
# The generated matrix
# ---------------------------------------------------------------------------

cases = st.builds(
    Case,
    family=st.sampled_from(FAMILIES),
    op=st.sampled_from(STRING_OPS),
    wide_string=st.booleans(),
    int_kind=st.sampled_from(["int64", "int64_nullable", "bool"]),
    with_faker=st.booleans(),
    with_fpe=st.booleans(),
    with_date_shift=st.booleans(),
    data=st.sampled_from(["present", "present", "nulls", "empty", "single"]),
    delta=st.sampled_from([-1, 0, 1]),
    seed=st.sampled_from([0, 1, 42]),
    auto_chunk=st.booleans(),
    unified=st.booleans(),
)


def _boundary(family: str, delta: int, **kw: Any) -> Case:
    base: dict[str, Any] = {
        "family": family,
        "op": "redact",
        "wide_string": False,
        "int_kind": "int64",
        "with_faker": False,
        "with_fpe": False,
        "with_date_shift": False,
        "data": "present",
        "delta": delta,
        "seed": 42,
        "auto_chunk": True,
        "unified": True,
    }
    base.update(kw)
    return Case(**base)


@settings(
    derandomize=True,
    deadline=None,
    max_examples=150,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(case=cases)
@example(case=_boundary("single", -1))
@example(case=_boundary("single", 0))
@example(case=_boundary("single", 1))
@example(case=_boundary("single", 0, auto_chunk=False))
@example(case=_boundary("single", 0, op="hash", with_faker=True, with_date_shift=True))
@example(case=_boundary("single", 1, with_fpe=True, data="nulls"))
@example(case=_boundary("single", 0, int_kind="int64_nullable"))
@example(case=_boundary("single", 1, int_kind="int64_nullable", data="empty"))
@example(case=_boundary("bounded_fk", -1))
@example(case=_boundary("bounded_fk", 0))
@example(case=_boundary("bounded_fk", 1))
@example(case=_boundary("full_frame_only_fk", -1))
@example(case=_boundary("full_frame_only_fk", 0))
@example(case=_boundary("full_frame_only_fk", 1))
@example(case=_boundary("single", 0, data="empty"))
@example(case=_boundary("single", 0, data="single"))
@example(case=_boundary("bounded_fk", 0, data="empty"))
def test_routing_and_output_parity(case: Case) -> None:
    check_case(case)


# ---------------------------------------------------------------------------
# Stamp-absence: the all-default path gains no work and no new key.
# ---------------------------------------------------------------------------

# Keys a default run must NOT add to quality_metrics. Each is stamped only by an opt-in or a
# non-default knob, so their absence is what "the hot path is unchanged" means.
OPT_IN_KEYS = (
    "execution_adapter",
    "execution_plan",
    "fidelity_reports",
    "quality_summary",
    "failed_checks",
    "post_validation_enforce",
    "quarantine",
    "row_errors",
    "code_set_corpora",
    "auto_chunk",
    "chunked_route",
    "chunked_route_by_table",
)
EXECUTION_KEYS = {
    "execution_mode",
    "route_reason",
    "eviction",
    "outputs_streamed",
    "loaded_fully_in_memory",
}


def _default_single(tmp: Path) -> Any:
    case = _boundary("single", -1, op="hash")
    cfg, tables, _ = build(case, tmp)
    return run_pipeline(cfg, tables, engine_version="r3-stamp")


def _default_fk(tmp: Path) -> Any:
    case = _boundary("bounded_fk", -1)
    cfg, tables, _ = build(case, tmp)
    return run_pipeline(cfg, tables, engine_version="r3-stamp")


@pytest.mark.parametrize("job", [_default_single, _default_fk])
def test_default_runs_stamp_nothing_new(job: Any, tmp_path: Path) -> None:
    metrics = job(tmp_path).quality_metrics
    for key in OPT_IN_KEYS:
        assert key not in metrics, key
    assert set(metrics["execution"]) == EXECUTION_KEYS  # no out_of_core_declined etc.
    # Whatever else is present is the unified lane's own evidence, on its admitted shape only.
    assert set(metrics) <= {"execution", "unified_slice_activation"}

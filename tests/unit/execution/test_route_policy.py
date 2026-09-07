"""Acceptance tests for P4-A Task 7's route seam
(`docs/plans/2026-09-03-p4-task7-route-seam.md` section 5, T1-T10 + T13):
sink-path auto-selection between the reorder driver and `_batch_join`, by
parent-key size, plus the `resolve_reorder_budgets` input-validation +
head-fit checks the selection predicate relies on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._memory_estimate import memory_limit_for
from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
from decoy_engine.execution.out_of_core._reorder_budget import (
    resolve_reorder_budgets,
)
from decoy_engine.execution.out_of_core._route_policy import (
    REORDER_PARENT_KEY_THRESHOLD,
    RouteDecision,
    decide_route,
    resolve_reorder_threshold_rows,
)
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from tests.parity.test_out_of_core_fk_parity import _build_chain, _build_single_edge
from tests.unit.execution.test_isolated_run import _mask_config, _mask_sources

_REG = get_default_registry()
_SEED = b"\x22" * 8
_BUDGET_BYTES = 1024 * 1024 * 1024  # 1 GiB
_DISK_BYTES = 200 * 1024 * 1024 * 1024
_SINK = object()  # decide_route only checks `is None`; a sentinel is sufficient


def _relation(tmp_path: Path, name: str, n_rows: int) -> ParentKeyRelation:
    """A real narrow parquet relation file with `n_rows` distinct keys -- the
    exact shape `decide_route`'s footer-read decision key expects."""
    path = tmp_path / f"{name}.parquet"
    pq.write_table(
        pa.table(
            {
                "__decoy_fk_join_key": [f"k{i}" for i in range(n_rows)],
                "__decoy_masked_key": [f"m{i}" for i in range(n_rows)],
            }
        ),
        path,
    )
    return ParentKeyRelation(path=path)


def _edge(*, parent: str = "parent", child: str = "child", tag: str = "e") -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=("pk",),
        child_table=child,
        child_columns=(f"fk_{tag}",),
        namespace=f"ns_{tag}",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


# ---------------------------------------------------------------------------
# T1-T6: pure predicate checks against `decide_route` directly.
# ---------------------------------------------------------------------------


def test_t1_sub_threshold_uses_batch_join(tmp_path: Path) -> None:
    relation = _relation(tmp_path, "parent", 10)
    edge = _edge()
    decision = decide_route(
        (edge,),
        {edge: relation},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1_000,
    )
    assert decision == RouteDecision(use_reorder=False, reorder_caps=None)


def test_t2_at_threshold_selects_reorder_with_correct_caps(tmp_path: Path) -> None:
    relation = _relation(tmp_path, "parent", 5_000)
    edge = _edge()
    decision = decide_route(
        (edge,),
        {edge: relation},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=5_000,
    )
    assert decision.use_reorder is True
    assert decision.reorder_caps is not None
    budgets = resolve_reorder_budgets(_BUDGET_BYTES, _DISK_BYTES)
    assert decision.reorder_caps.joiner_memory_limit == memory_limit_for(
        budgets.duckdb_memory_limit_bytes, 1
    )
    assert decision.reorder_caps.build_memory_limit == memory_limit_for(_BUDGET_BYTES, 1)
    assert decision.reorder_caps.run_bytes_cap == budgets.run_bytes_cap
    assert decision.reorder_caps.merge_fan_in == budgets.merge_fan_in


def test_t2_reorder_route_output_matches_batch_join_exactly(tmp_path: Path) -> None:
    """The end-to-end analog of T2: forcing reorder via the threshold override
    produces byte-identical sink output to the default (`_batch_join`) route."""
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="hash",
        policy=OrphanPolicy.WARN,
        parent_rows=6,
        child_refs=[0, 1, -1, 2, None, 0, 3],
        parent_nan_row=None,
    )
    batch_join_dir = tmp_path / "batch-join"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(batch_join_dir),
        temp_dir=tmp_path / "work-batch-join",
    )
    reorder_dir = tmp_path / "reorder"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(reorder_dir),
        temp_dir=tmp_path / "work-reorder",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=0,
    )
    for table in ("parent", "child"):
        batch_table = pq.read_table(batch_join_dir / f"{table}.parquet")
        reorder_table = pq.read_table(reorder_dir / f"{table}.parquet")
        assert reorder_table.schema.equals(batch_table.schema, check_metadata=True)
        assert reorder_table.equals(batch_table, check_metadata=True)


def test_t3_missing_memory_budget_falls_back_no_raise(tmp_path: Path) -> None:
    relation = _relation(tmp_path, "parent", 5_000_000)
    edge = _edge()
    decision = decide_route(
        (edge,),
        {edge: relation},
        sink=_SINK,
        budget_bytes=None,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
    )
    assert decision == RouteDecision(use_reorder=False, reorder_caps=None)


def test_t3_missing_disk_budget_falls_back_no_raise(tmp_path: Path) -> None:
    relation = _relation(tmp_path, "parent", 5_000_000)
    edge = _edge()
    decision = decide_route(
        (edge,),
        {edge: relation},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=None,
        threshold_rows=1,
    )
    assert decision == RouteDecision(use_reorder=False, reorder_caps=None)


def test_t4_root_table_never_reorders() -> None:
    decision = decide_route(
        (),
        {},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=0,
    )
    assert decision == RouteDecision(use_reorder=False, reorder_caps=None)


def test_t5_resident_path_never_reorders(tmp_path: Path) -> None:
    relation = _relation(tmp_path, "parent", 5_000_000)
    edge = _edge()
    decision = decide_route(
        (edge,),
        {edge: relation},
        sink=None,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
    )
    assert decision == RouteDecision(use_reorder=False, reorder_caps=None)


def test_t6_high_fan_in_falls_back_no_raise(tmp_path: Path) -> None:
    merge_fan_in = 16
    edges = tuple(_edge(tag=str(i)) for i in range(2 * merge_fan_in + 1))  # 33 > 32
    relations = {edge: _relation(tmp_path, f"p{i}", 5_000_000) for i, edge in enumerate(edges)}
    decision = decide_route(
        edges,
        relations,
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
        merge_fan_in=merge_fan_in,
    )
    assert decision == RouteDecision(use_reorder=False, reorder_caps=None)


def test_t6_admitted_fan_in_boundary_still_eligible(tmp_path: Path) -> None:
    merge_fan_in = 16
    edges = tuple(_edge(tag=str(i)) for i in range(2 * merge_fan_in))  # exactly 32
    relations = {edge: _relation(tmp_path, f"p{i}", 5_000_000) for i, edge in enumerate(edges)}
    decision = decide_route(
        edges,
        relations,
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
        merge_fan_in=merge_fan_in,
    )
    assert decision.use_reorder is True


# ---------------------------------------------------------------------------
# T7: a resolvable-but-too-small budget on a reorder-selected table fails
# closed and leaves the sink uncommitted.
# ---------------------------------------------------------------------------


def test_t7_too_small_budget_raises_and_aborts_sink(tmp_path: Path) -> None:
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=4,
        child_refs=[0, 1, 2],
        parent_nan_row=None,
    )
    target = tmp_path / "out"
    with pytest.raises(ExecutionError) as exc_info:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=tmp_path / "work",
            # Big enough that the ordinary batch-model build cap (`parent`'s own
            # relation build -- real DuckDB work, unrelated to the reorder path)
            # has comfortable headroom above DuckDB's own connection overhead;
            # still far under the sorter's MIN_RUN_BYTES / F_SORT floor once
            # `resolve_reorder_budgets` sizes `child`'s reorder route.
            budget_bytes=40_000_000,
            temp_disk_budget_bytes=_DISK_BYTES,
            out_of_core_reorder_threshold_rows=0,
        )
    assert exc_info.value.code == "out_of_core_reorder_budget_too_small"
    assert not target.exists(), "sink must not commit any output on a fail-closed abort"


# ---------------------------------------------------------------------------
# T8: the decision key is the DEDUPED parent-key count, not raw parent rows.
# ---------------------------------------------------------------------------


def _passthrough_seed() -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="passthrough",
        provider="passthrough",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        coherent_with=(),
    )


def _dup_parent_fixture() -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    seed = _passthrough_seed()
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
                ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    # 100 raw parent rows: only 3 DISTINCT non-null keys, the rest duplicates
    # or nulls -- the deduped relation the join actually consumes has 3 rows.
    raw_keys = (["k0", "k1", "k2"] * 30) + [None] * 10
    parent = pa.table({"pk": pa.array(raw_keys, type=pa.string())})
    child = pa.table({"fk": pa.array(["k0", "k1", "k2"] * 5, type=pa.string())})
    return plan, {"parent": parent, "child": child}, graph


def _witness_child_table_routes(monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    """Record which driver constructed a joiner for which child table."""
    seen: dict[str, set[str]] = {"batch_join": set(), "reorder": set()}
    orig_batch_init = ChildFkBatchJoiner.__init__
    orig_reorder_init = StreamFkJoiner.__init__

    def spy_batch_init(self: Any, *, edge: Any, **kwargs: Any) -> None:
        seen["batch_join"].add(edge.child_table)
        orig_batch_init(self, edge=edge, **kwargs)

    def spy_reorder_init(self: Any, *, edge: Any, **kwargs: Any) -> None:
        seen["reorder"].add(edge.child_table)
        orig_reorder_init(self, edge=edge, **kwargs)

    monkeypatch.setattr(ChildFkBatchJoiner, "__init__", spy_batch_init)
    monkeypatch.setattr(StreamFkJoiner, "__init__", spy_reorder_init)
    return seen


def test_t8_routes_by_deduped_key_count_not_raw_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, sources, graph = _dup_parent_fixture()
    seen = _witness_child_table_routes(monkeypatch)

    # threshold=10 sits BETWEEN the deduped count (3) and the raw row count
    # (100): routing on raw rows would wrongly reorder; routing on the
    # deduped count correctly stays on `_batch_join`.
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(tmp_path / "above-raw-below-deduped"),
        temp_dir=tmp_path / "work-1",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=10,
    )
    assert seen["reorder"] == set()
    assert seen["batch_join"] == {"child"}

    # threshold=3 <= the deduped count -> now eligible.
    seen2 = _witness_child_table_routes(monkeypatch)
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(tmp_path / "at-deduped-count"),
        temp_dir=tmp_path / "work-2",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=3,
    )
    assert seen2["batch_join"] == set()
    assert seen2["reorder"] == {"child"}


# ---------------------------------------------------------------------------
# T9: the `out_of_core_reorder_threshold_rows` override's validation
# semantics, direct AND through `run_pipeline_isolated`.
# ---------------------------------------------------------------------------


def test_t9_none_resolves_to_default() -> None:
    assert resolve_reorder_threshold_rows(None) == REORDER_PARENT_KEY_THRESHOLD


def test_t9_zero_means_reorder_every_eligible_table() -> None:
    assert resolve_reorder_threshold_rows(0) == 0


def test_t9_positive_int_is_the_boundary() -> None:
    assert resolve_reorder_threshold_rows(42) == 42


@pytest.mark.parametrize("bad", [-1, -1000])
def test_t9_negative_raises_coded_error(bad: int) -> None:
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_threshold_rows(bad)
    assert exc_info.value.code == "out_of_core_reorder_threshold_invalid"


@pytest.mark.parametrize("bad", [True, False])
def test_t9_bool_raises_coded_error_not_silently_coerced(bad: bool) -> None:
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_threshold_rows(bad)
    assert exc_info.value.code == "out_of_core_reorder_threshold_invalid"


@pytest.mark.parametrize("bad", [1.5, "5", 5.0])
def test_t9_non_integer_raises_coded_error(bad: object) -> None:
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_threshold_rows(bad)  # type: ignore[arg-type]
    assert exc_info.value.code == "out_of_core_reorder_threshold_invalid"


def test_t9_invalid_override_same_coded_error_direct_and_isolated(tmp_path: Path) -> None:
    cfg = _mask_config(tmp_path, n_cols=1)
    sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

    with pytest.raises(ExecutionError) as direct_exc:
        run_pipeline(
            cfg, sources, engine_version="t9-direct", out_of_core_reorder_threshold_rows=True
        )
    assert direct_exc.value.code == "out_of_core_reorder_threshold_invalid"

    result = run_pipeline_isolated(
        cfg,
        sources,
        engine_version="t9-isolated",
        isolate=True,
        out_of_core_reorder_threshold_rows=True,
    )
    assert result.outcome == "crashed"
    assert "out_of_core_reorder_threshold_invalid" in (result.error or "")


# ---------------------------------------------------------------------------
# T10: a mixed multi-table job routes each table independently; whole-job
# output matches an all-`_batch_join` run exactly.
# ---------------------------------------------------------------------------


def test_t10_mixed_job_routes_independently_and_matches_batch_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # parent (fixed 4 distinct "pk" keys) -> child (edge sees 4 >= threshold:
    # reorders); child's OWN "ck" cardinality is `len(child_refs)` -- keeping
    # that short makes the child -> grandchild edge see 2 < threshold, so it
    # stays on `_batch_join`. One job, two tables, two different routes.
    plan, sources, graph = _build_chain(
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        child_refs=[0, 1],
        gc_refs=[0, 1, 0],
    )
    seen = _witness_child_table_routes(monkeypatch)
    mixed_dir = tmp_path / "mixed"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(mixed_dir),
        temp_dir=tmp_path / "work-mixed",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=3,
    )
    assert seen["reorder"] == {"child"}
    assert seen["batch_join"] == {"grandchild"}

    baseline_dir = tmp_path / "baseline"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(baseline_dir),
        temp_dir=tmp_path / "work-baseline",
    )
    for table in ("parent", "child", "grandchild"):
        mixed_table = pq.read_table(mixed_dir / f"{table}.parquet")
        baseline_table = pq.read_table(baseline_dir / f"{table}.parquet")
        assert mixed_table.schema.equals(baseline_table.schema, check_metadata=True)
        assert mixed_table.equals(baseline_table, check_metadata=True)


# ---------------------------------------------------------------------------
# T13: `resolve_reorder_budgets` input validation + the phase-3 head-fit
# helper.
# ---------------------------------------------------------------------------


def test_t13_merge_fan_in_below_two_raises() -> None:
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_budgets(_BUDGET_BYTES, _DISK_BYTES, merge_fan_in=1)
    assert exc_info.value.code == "out_of_core_reorder_fan_in_invalid"


def test_t13_negative_disk_budget_raises() -> None:
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_budgets(_BUDGET_BYTES, -1)
    assert exc_info.value.code == "out_of_core_reorder_disk_budget_invalid"


def test_t13_head_fit_co_resident_readers_non_positive_raises() -> None:
    from decoy_engine.execution.out_of_core._reorder_budget import phase3_head_fit

    budgets = resolve_reorder_budgets(_BUDGET_BYTES, _DISK_BYTES)
    with pytest.raises(ExecutionError) as exc_info:
        phase3_head_fit(budgets, 0)
    assert exc_info.value.code == "out_of_core_reorder_head_fit_invalid"
    with pytest.raises(ExecutionError):
        phase3_head_fit(budgets, -3)


def test_t13_head_fit_passes_within_2x_fan_in_and_fails_beyond_it() -> None:
    from decoy_engine.execution.out_of_core._reorder_budget import phase3_head_fit

    budgets = resolve_reorder_budgets(_BUDGET_BYTES, _DISK_BYTES, merge_fan_in=16)
    within = phase3_head_fit(budgets, 32)
    assert within.fits is True
    assert within.max_co_resident_readers == 32
    beyond = phase3_head_fit(budgets, 33)
    assert beyond.fits is False
    assert beyond.max_co_resident_readers == 32


__all__: list[str] = []

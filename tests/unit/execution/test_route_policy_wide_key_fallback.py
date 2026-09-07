"""P0 fix: the reorder route's external sorter rejects any single joined row
wider than its per-head cap (`_external_sort.py`'s `out_of_core_sort_row_too_
wide`), a limit `_batch_join` never enforces. A wide FK value (e.g. a
multi-MB passthrough string parent key) worked on `_batch_join` but failed on
the reorder route -- a works-on-batch/fails-on-reorder regression.

After the slim-sort fix the sorter carries only the slim row (row_nr + match
token + masked parent), so `_route_policy.decide_route` falls back to
`_batch_join` per edge whenever a relation's measured
`max_sort_payload_row_bytes` (`_relation.ParentKeyRelation`, populated at build
time by `_key_width.SlimRowWidthTracker`) would overflow the sorter's
per-merge-head cap. Only a genuinely wide MASKED parent value pays the fallback;
ordinary FK jobs (and a wide RAW key with a narrow masked value, e.g. under
hash) keep the reorder speedup. This module proves both the route-policy
predicate in isolation and the full route end to end, on the exact shape a prior
review round reproduced: a path-backed `LazySource`, a sink, `PRESERVE` orphans,
a passthrough parent key (masked value stays wide), no orphans, and a 1 GiB
budget.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
from decoy_engine.execution.out_of_core._reorder_budget import resolve_reorder_budgets
from decoy_engine.execution.out_of_core._route_policy import (
    RouteDecision,
    decide_route,
)
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_REG = get_default_registry()
_JOB_SEED = b"\x33" * 8
_BUDGET_BYTES = 1024 * 1024 * 1024  # 1 GiB, matches the reproduced case
_DISK_BYTES = 200 * 1024 * 1024 * 1024
_SINK = object()  # decide_route only checks `is None`; a sentinel is sufficient


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


def _edge() -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _plan(seed: ColumnSeed | None = None) -> Any:
    seed = seed if seed is not None else _passthrough_seed()
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
                ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
            ),
        )
    )


def _hash_seed() -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _lazy_table(tmp_path: Path, name: str, table: pa.Table) -> LazySource:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return LazySource(path)


# ---------------------------------------------------------------------------
# Route-policy unit tests: a relation's measured width admits or rejects
# reorder, independent of everything else `decide_route` checks.
# ---------------------------------------------------------------------------


def _relation(tmp_path: Path, name: str, n_rows: int, row_bytes: int) -> ParentKeyRelation:
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
    return ParentKeyRelation(path=path, max_sort_payload_row_bytes=row_bytes)


def test_wide_max_key_bytes_falls_back_to_batch_join(tmp_path: Path) -> None:
    edge = _edge()
    relation = _relation(tmp_path, "parent", 100, row_bytes=10_000_000)
    decision = decide_route(
        (edge,),
        {edge: relation},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
    )
    assert decision == RouteDecision(use_reorder=False, reorder_caps=None)


def test_narrow_max_key_bytes_still_selects_reorder(tmp_path: Path) -> None:
    edge = _edge()
    relation = _relation(tmp_path, "parent", 100, row_bytes=32)
    decision = decide_route(
        (edge,),
        {edge: relation},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
    )
    assert decision.use_reorder is True
    assert decision.reorder_caps is not None


def test_width_admission_boundary_matches_per_head_cap_arithmetic(tmp_path: Path) -> None:
    """The per-edge admission boundary is now the slim row's own byte bound
    compared directly against `_reorder_budget`'s per-head cap (no empirical
    width factor): `bound < cap` reorders, `bound >= cap` falls back. cap-1
    reorders, cap and cap+1 fall back."""
    edge = _edge()
    budgets = resolve_reorder_budgets(_BUDGET_BYTES, _DISK_BYTES)
    per_head_cap = budgets.run_bytes_cap // (2 * budgets.merge_fan_in)

    def _routes(row_bytes: int) -> bool:
        relation = _relation(tmp_path, f"rel{row_bytes}", 100, row_bytes=row_bytes)
        return decide_route(
            (edge,),
            {edge: relation},
            sink=_SINK,
            budget_bytes=_BUDGET_BYTES,
            temp_disk_budget_bytes=_DISK_BYTES,
            threshold_rows=1,
        ).use_reorder

    assert _routes(per_head_cap - 1) is True
    assert _routes(per_head_cap) is False
    assert _routes(per_head_cap + 1) is False


def _edge_named(child_col: str) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=(child_col,),
        namespace=f"ns_{child_col}",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def test_two_edges_individually_under_collectively_over_still_reorder(tmp_path: Path) -> None:
    """Phase-2 sorts are SEQUENTIAL per edge, so the gate is PER EDGE, never a
    cross-edge sum: two edges each individually under the cap must still reorder
    even though their widths sum to OVER the cap."""
    budgets = resolve_reorder_budgets(_BUDGET_BYTES, _DISK_BYTES)
    per_head_cap = budgets.run_bytes_cap // (2 * budgets.merge_fan_in)
    e1, e2 = _edge_named("fk1"), _edge_named("fk2")
    each = int(per_head_cap * 0.7)  # individually under, 1.4x cap summed
    r1 = _relation(tmp_path, "r1", 100, row_bytes=each)
    r2 = _relation(tmp_path, "r2", 100, row_bytes=each)
    decision = decide_route(
        (e1, e2),
        {e1: r1, e2: r2},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
    )
    assert decision.use_reorder is True


def test_one_over_cap_edge_among_several_falls_back(tmp_path: Path) -> None:
    """A single over-cap edge forces the whole table to fall back, even beside
    narrow edges that would reorder on their own."""
    budgets = resolve_reorder_budgets(_BUDGET_BYTES, _DISK_BYTES)
    per_head_cap = budgets.run_bytes_cap // (2 * budgets.merge_fan_in)
    e1, e2 = _edge_named("fk1"), _edge_named("fk2")
    narrow = _relation(tmp_path, "narrow", 100, row_bytes=64)
    wide = _relation(tmp_path, "wide", 100, row_bytes=per_head_cap + 1)
    decision = decide_route(
        (e1, e2),
        {e1: narrow, e2: wide},
        sink=_SINK,
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        threshold_rows=1,
    )
    assert decision.use_reorder is False


# ---------------------------------------------------------------------------
# Full-route reproduction: the exact shape a prior review round reproduced
# (path-backed LazySource, sink, PRESERVE, passthrough, no orphans, 1 GiB),
# plus a narrow counterpart proving the fallback is precise, not blanket.
#
# NOT routed through the parity harness's `_run_sink` witness
# (`tests/parity/test_out_of_core_route_seam_parity.py`): that witness's
# `assert not saw["batch"]` assumes a forced reorder always constructs
# StreamFkJoiner, which is exactly what a legitimate width fallback violates.
# These tests spy the joiner constructors directly instead.
# ---------------------------------------------------------------------------


def _spy_joiner_inits(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    seen = {"batch": False, "reorder": False}
    orig_batch = ChildFkBatchJoiner.__init__
    orig_reorder = StreamFkJoiner.__init__

    def spy_batch(self: Any, *a: Any, **k: Any) -> None:
        seen["batch"] = True
        orig_batch(self, *a, **k)

    def spy_reorder(self: Any, *a: Any, **k: Any) -> None:
        seen["reorder"] = True
        orig_reorder(self, *a, **k)

    monkeypatch.setattr(ChildFkBatchJoiner, "__init__", spy_batch)
    monkeypatch.setattr(StreamFkJoiner, "__init__", spy_reorder)
    return seen


def test_wide_fk_key_falls_back_to_batch_join_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ~6 MiB string passthrough parent key used to raise
    `out_of_core_sort_row_too_wide` on the reorder route; it must now fall
    back to `_batch_join` and commit valid output instead."""
    wide_key = "w" * (6 * 1024 * 1024)
    parent = pa.table(
        {
            "pk": pa.array([wide_key, "k1"], type=pa.string()),
            "payload": pa.array(["p0", "p1"], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array([wide_key, "k1"], type=pa.string()),
            "payload": pa.array(["c0", "c1"], type=pa.string()),
        }
    )
    sources = {
        "parent": _lazy_table(tmp_path, "parent", parent),
        "child": _lazy_table(tmp_path, "child", child),
    }
    graph = RelationshipGraph(edges=(_edge(),), ordering=())
    target = tmp_path / "out"
    seen = _spy_joiner_inits(monkeypatch)

    result = run_fk_out_of_core(
        _plan(),
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=0,
    )

    assert result.outputs == {}  # sink path: nothing held resident
    assert seen["batch"] is True
    assert seen["reorder"] is False
    written_parent = pq.read_table(target / "parent.parquet")
    assert sorted(written_parent.column("pk").to_pylist()) == sorted([wide_key, "k1"])
    written_child = pq.read_table(target / "child.parquet")
    assert sorted(written_child.column("fk").to_pylist()) == sorted([wide_key, "k1"])


def test_matched_6mib_key_under_hash_selects_reorder_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact case a prior review round could not close: a matched ~6 MiB
    raw string FK. Under HASH the masked value is small, so the slim sorter row
    is tiny -- the reorder route is now SELECTED (never falls back) and commits
    valid output, where it previously raised `out_of_core_sort_row_too_wide`."""
    wide_key = "w" * (6 * 1024 * 1024)
    n = 3
    parent_keys = [wide_key] + [f"k{i}" for i in range(n)]
    parent = pa.table(
        {
            "pk": pa.array(parent_keys, type=pa.string()),
            "payload": pa.array([f"p{i}" for i in range(n + 1)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array(parent_keys, type=pa.string()),
            "payload": pa.array([f"c{i}" for i in range(n + 1)], type=pa.string()),
        }
    )
    sources = {
        "parent": _lazy_table(tmp_path, "parent", parent),
        "child": _lazy_table(tmp_path, "child", child),
    }
    graph = RelationshipGraph(edges=(_edge(),), ordering=())
    target = tmp_path / "out"
    seen = _spy_joiner_inits(monkeypatch)

    run_fk_out_of_core(
        _plan(_hash_seed()),
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=0,
    )

    assert seen["reorder"] is True
    assert seen["batch"] is False
    written_child = pq.read_table(target / "child.parquet")
    assert written_child.num_rows == n + 1
    # The wide raw key was hash-masked, never carried through the sorter raw.
    assert wide_key not in written_child.column("fk").to_pylist()


def test_narrow_fk_key_still_routes_through_reorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same shape as the wide-key case, but with ordinary short string keys
    and a large enough parent count to be a realistic table: the fallback
    must be precise, not blanket -- this must still take the reorder route."""
    n = 50_000
    parent_keys = [f"parent-key-{i:06d}" for i in range(n)]
    parent = pa.table(
        {
            "pk": pa.array(parent_keys, type=pa.string()),
            "payload": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array(parent_keys, type=pa.string()),  # every parent referenced once
            "payload": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
        }
    )
    sources = {
        "parent": _lazy_table(tmp_path, "parent", parent),
        "child": _lazy_table(tmp_path, "child", child),
    }
    graph = RelationshipGraph(edges=(_edge(),), ordering=())
    target = tmp_path / "out"
    seen = _spy_joiner_inits(monkeypatch)

    run_fk_out_of_core(
        _plan(),
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=0,
    )

    assert seen["reorder"] is True
    assert seen["batch"] is False
    assert pq.read_metadata(target / "parent.parquet").num_rows == n
    assert pq.read_metadata(target / "child.parquet").num_rows == n


__all__: list[str] = []

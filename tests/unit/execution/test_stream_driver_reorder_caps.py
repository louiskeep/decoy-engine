"""T11 (`docs/plans/2026-09-03-p4-task7-route-seam.md` section 5): the
reorder route's memory sizing, proven at the `run_fk_out_of_core` route
level (not the standalone driver) so the route seam's OWN wiring -- not just
`ReorderCaps`' arithmetic -- is under test.

Asserts the joiner connection opens at `F_DUCKDB * ceiling` and the build
connection at the undivided sink build cap, AND that the reorder path never
consults `resolve_phase_memory_limits` (the batch-model phase resolver) at
all -- proven by making that resolver raise and confirming the run still
completes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _relation as relation_mod
from decoy_engine.execution.out_of_core import _stream_driver, run_fk_out_of_core
from decoy_engine.execution.out_of_core import _stream_join as stream_join_mod
from decoy_engine.execution.out_of_core._memory_estimate import memory_limit_for
from decoy_engine.execution.out_of_core._reorder_budget import F_DUCKDB
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity.test_out_of_core_fk_parity import _build_chain, _build_single_edge

_REG = get_default_registry()
_BUDGET_BYTES = 1024 * 1024 * 1024  # 1 GiB
_DISK_BYTES = 200 * 1024 * 1024 * 1024


def _capture_connect_duckdb(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str | None]]:
    seen: dict[str, list[str | None]] = {"joiner": [], "build": []}
    real_join_connect = stream_join_mod.connect_duckdb
    real_relation_connect = relation_mod.connect_duckdb

    def fake_join_connect(*, temp_dir: Any, memory_limit: str | None = None) -> Any:
        seen["joiner"].append(memory_limit)
        return real_join_connect(temp_dir=temp_dir, memory_limit=memory_limit)

    def fake_relation_connect(*, temp_dir: Any, memory_limit: str | None = None) -> Any:
        seen["build"].append(memory_limit)
        return real_relation_connect(temp_dir=temp_dir, memory_limit=memory_limit)

    monkeypatch.setattr(stream_join_mod, "connect_duckdb", fake_join_connect)
    monkeypatch.setattr(relation_mod, "connect_duckdb", fake_relation_connect)
    return seen


def _forced_reorder_fixture() -> Any:
    return _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=5,
        child_refs=[0, 1, 2, -1, None],
        parent_nan_row=None,
    )


def test_t11_joiner_and_build_caps_match_reorder_budgets_not_batch_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, sources, graph = _forced_reorder_fixture()
    seen = _capture_connect_duckdb(monkeypatch)

    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(tmp_path / "out"),
        temp_dir=tmp_path / "work",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=0,
    )

    expected_joiner = memory_limit_for(round(F_DUCKDB * _BUDGET_BYTES), 1)
    expected_build = memory_limit_for(_BUDGET_BYTES, 1)
    # `child`'s one incoming edge is the only joiner opened on the reorder
    # route; `parent` has no incoming edges so it opens none.
    assert seen["joiner"] == [expected_joiner]
    # Both tables' outgoing-relation builds run through `_relation.py`'s
    # SAME `connect_duckdb` regardless of which driver rewrote the table --
    # `child` has no outgoing edge, so only `parent`'s build opens here, at
    # the undivided sink build cap (FAILS on the pre-fix batch-model sizing,
    # PASSES once the reorder path bypasses `resolve_phase_memory_limits`).
    assert seen["build"] == [expected_build]


def test_t11_reorder_path_never_consults_resolve_phase_memory_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, sources, graph = _forced_reorder_fixture()

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "the reorder route must never call resolve_phase_memory_limits "
            "(_stream_driver.py); ReorderCaps sizes it directly"
        )

    monkeypatch.setattr(_stream_driver, "resolve_phase_memory_limits", _forbidden)

    target = tmp_path / "out"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=0,
    )
    # A completed run (not the forbidden-call AssertionError) is the proof:
    # the child table's route is the only one with an incoming edge, and it
    # completed without ever calling the patched-to-raise resolver.
    assert pq.read_table(target / "child.parquet").num_rows == len(sources["child"])


def _forced_reorder_chain_fixture() -> Any:
    # parent -> child -> grandchild: `child` is the MIDDLE table here, the
    # one this gap-fix test targets -- it carries BOTH an incoming edge
    # (parent->child, reorder-routed at threshold=0) and an outgoing edge
    # (child->grandchild) whose relation build this test isolates.
    return _build_chain(
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        child_refs=[0, 1, 2, 3],
        gc_refs=[0, 1, 2, 3],
    )


def test_t11_middle_table_reorder_build_gets_undivided_sink_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T11 coverage gap: `test_t11_joiner_and_build_caps_match_reorder_budgets_
    not_batch_model` above routes `child` (no outgoing edge in that 2-table
    fixture) through reorder, so the ONE build call it observes is actually
    `parent`'s (batch route, since `parent` has no incoming edge) -- which
    happens to open at the SAME undivided cap as `ReorderCaps.build_memory_
    limit` purely because `resolve_phase_memory_limits`'s sink branch is ALSO
    undivided (`memory_limit_for(budget_bytes, 1)` regardless of incoming-
    edge count). That test would keep passing even if a reorder-routed
    table's OWN build cap regressed to a different formula, because it never
    actually observes one. This chain fixture gives `child` BOTH an incoming
    edge (so IT is reorder-routed) and an outgoing edge (so ITS OWN build
    runs through `reorder_caps.build_memory_limit`), isolating the call this
    invariant is actually about.
    """
    plan, sources, graph = _forced_reorder_chain_fixture()
    seen = _capture_connect_duckdb(monkeypatch)

    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(tmp_path / "out"),
        temp_dir=tmp_path / "work",
        budget_bytes=_BUDGET_BYTES,
        temp_disk_budget_bytes=_DISK_BYTES,
        out_of_core_reorder_threshold_rows=0,
    )

    expected_build = memory_limit_for(_BUDGET_BYTES, 1)
    # Build calls happen in the run's topo order (parent, then child;
    # `grandchild` has no outgoing edge so opens none): index 0 is `parent`'s
    # (batch route), index 1 is `child`'s -- the MIDDLE table's own reorder-
    # routed build, the one T11 pins.
    assert len(seen["build"]) == 2
    assert seen["build"][1] == expected_build


__all__: list[str] = []

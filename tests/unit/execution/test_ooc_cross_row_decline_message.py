"""The out-of-core cross-row decline names where, why, and what happens next.

`out_of_core_cross_row_strategy_unsupported` is the coded reason for the five
strategies the per-column streaming kernel cannot run (`_compat.py`'s
`_CROSS_ROW_STRATEGIES`). Its message used to say only "needs a bounded
relational lowering", which told an operator nothing about which column, what
state the kernel lacks, or that the job still runs. Every other deferred
decline in `_compat.py` names the `table.columns`, the cause and the fallback;
this pins the cross-row decline to the same shape so it cannot regress to a
bare phrase.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_REG = get_default_registry()
_CODE = "out_of_core_cross_row_strategy_unsupported"

# The state each strategy needs that the per-column kernel does not carry; the
# message must name it so the decline is actionable, not generic.
_WHY = {
    "group_key": "other columns of the same row",
    "windowed_date": "durable global row ordinal",
    "grouped_series": "per-group sequential stream",
    "derived_aggregate": "intra-table aggregate",
    "joint_mask": "reference-table row",
}


def _seed(strategy: str, *, namespace: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=(),
        coherent_with=(),
    )


def _rejections(payload: ColumnSeed) -> list[Any]:
    key = _seed("hash", namespace="kns")
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x11" * 8,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", key), ("pay", payload)), per_group=())),
                ("child", TableSeed(per_column=(("fk", key),), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    work = order_work(build_work_list(plan, _REG), graph)
    return [
        r for r in check_out_of_core_compatibility(plan, work, graph).rejections if r.code == _CODE
    ]


@pytest.mark.parametrize("strategy", sorted(_WHY))
def test_cross_row_decline_names_column_cause_and_fallback(strategy: str) -> None:
    found = _rejections(_seed(strategy))
    assert len(found) == 1, found
    message = found[0].message
    # Where: the table and column, like every other decline in the gate.
    assert message.startswith("parent.") and "'pay'" in message, message
    # What: the strategy, and the specific state the streaming kernel lacks.
    assert repr(strategy) in message, message
    assert _WHY[strategy] in message, message
    # What next: the job is not dropped, it falls back to the oracle route.
    assert "falls back" in message.lower(), message

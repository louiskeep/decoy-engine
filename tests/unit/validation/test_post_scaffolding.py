"""engine-v2 S10 slice 2: post-validation scaffolding + flag gate + dataclasses.

No scans yet (those land in slices 3-5). This pins the flag contract (off ->
None, zero overhead; on -> a QualitySummary), the QualityWarning forward from the
S9 ExecutionResult, and the quality_summary dataclass shapes.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionResult
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.validation.post import (
    CompositeCoherenceReport,
    DistinctCount,
    FkValidityReport,
    NullCount,
    PostValidationRunner,
    QualitySummary,
)

_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _run(
    config: dict[str, Any], warnings: tuple[QualityWarning, ...] = ()
) -> QualitySummary | None:
    result = ExecutionResult(outputs={"t": pa.table({"a": [1, 2]})}, warnings=warnings)
    return PostValidationRunner().run(
        plan=object(),  # unused by the slice-2 skeleton (no scans read it yet)
        execution_result=result,
        sources={"t": pa.table({"a": [1, 2]})},
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        config=config,
    )


class TestFlagGate:
    def test_flag_off_returns_none(self) -> None:
        assert _run({}) is None
        assert _run({"post_validation": False}) is None

    def test_flag_on_returns_summary_with_timing(self) -> None:
        summary = _run({"post_validation": True})
        assert isinstance(summary, QualitySummary)
        assert "post_validation_phase_ms" in summary.timing_per_phase
        # Scan-populated fields stay empty until slices 3-5.
        assert summary.distinct_counts == {}
        assert summary.fk_validity == {}
        assert summary.composite_coherence == {}
        assert summary.failed_checks == ()

    def test_flag_on_forwards_quality_warnings(self) -> None:
        warning = QualityWarning(
            code="orphan_fk", provider="ns", column="c", detail={"orphan_rows": 3}
        )
        summary = _run({"post_validation": True}, warnings=(warning,))
        assert summary is not None
        assert summary.quality_warnings == (warning,)  # forwarded from ExecutionResult


class TestQualitySummaryDataclasses:
    def test_construct_and_frozen(self) -> None:
        dc = DistinctCount(source_distinct=5, output_distinct=5)
        with pytest.raises((AttributeError, TypeError)):
            dc.source_distinct = 9  # type: ignore[misc]
        nc = NullCount(source_nulls=0, output_nulls=1)
        fk = FkValidityReport(
            relationship="customers.customer_id -> orders.customer_id",
            namespace="customer_identity",
            orphan_policy="warn",
            child_row_count=10,
            parent_match_count=8,
            orphan_count=2,
            invalid_count=0,
        )
        cc = CompositeCoherenceReport(
            generator="composite_name_email",
            columns=("first_name", "last_name", "email"),
            total_rows=10,
            coherent_rows=10,
            incoherent_rows=0,
        )
        assert (dc.output_distinct, nc.output_nulls, fk.orphan_count, cc.coherent_rows) == (
            5,
            1,
            2,
            10,
        )

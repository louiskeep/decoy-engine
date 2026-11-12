"""AT-P6: the projection names are public on ``decoy_engine.run_result`` only."""

from __future__ import annotations

import decoy_engine
import decoy_engine.run_result as run_result

NEW = (
    "CollectedFacts",
    "DeliveryFacts",
    "ProjectedTables",
    "RunResultCollector",
    "TableDelivery",
    "project_tables",
)


def test_package_exports() -> None:
    for name in NEW:
        assert name in run_result.__all__
        assert hasattr(run_result, name)
    assert not set(NEW) & set(decoy_engine.__all__)


BUILD = (
    "BaselineLink",
    "ConfigRef",
    "EvidenceLink",
    "FailureInput",
    "Links",
    "LogLink",
    "NativeRoute",
    "RouteView",
    "RunFacts",
    "build_run_result",
)


def test_build_exports() -> None:
    for name in BUILD:
        assert name in run_result.__all__
        assert hasattr(run_result, name)
    assert not set(BUILD) & set(decoy_engine.__all__)

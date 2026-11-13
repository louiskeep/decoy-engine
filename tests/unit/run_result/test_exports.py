"""AT-P6: the projection names are public on ``decoy_engine.run_result`` only."""

from __future__ import annotations

import decoy_engine
import decoy_engine.run_result as run_result

NEW = ("CollectedFacts", "DeliveryFacts", "ProjectedTables", "TableDelivery", "project_tables")


def test_package_exports() -> None:
    for name in NEW:
        assert name in run_result.__all__
        assert hasattr(run_result, name)
    assert not set(NEW) & set(decoy_engine.__all__)

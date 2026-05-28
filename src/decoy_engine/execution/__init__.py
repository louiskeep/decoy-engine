"""engine-v2 S9 execution adapter package.

The boundary between planning and execution: `ExecutionAdapter.run(plan, source)
-> ExecutionResult`. The first concrete adapter is the pandas adapter.

Slice 1 (landed): the runner core -- `build_work_list` (enumerates maskable
units from the seed envelope, the authoritative work list) + `order_work`
(FK + R17 composite-before-child topological ordering) + the execution error
hierarchy.

Later slices add: the ExecutionAdapter protocol + ExecutionResult, the concrete
PandasExecutionAdapter, the 11 baseline strategies re-keyed onto S3/S5, and the
Faker / FPE per-strategy parallelism.

Spec: docs/v2/sprints/engine-v2/sprint-09-execution-adapter-pandas.md in decoy-platform.
"""

from __future__ import annotations

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work

__all__ = [
    "ExecutionError",
    "StrategyError",
    "WorkNode",
    "build_work_list",
    "order_work",
]

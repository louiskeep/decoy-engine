"""engine-v2 S9 execution adapter package.

The boundary between planning and execution: `ExecutionAdapter.run(plan, source)
-> ExecutionResult`. The first concrete adapter is `PandasExecutionAdapter`.

Public API:

    from decoy_engine.execution import (
        ExecutionAdapter,
        PandasExecutionAdapter,
        ExecutionResult,
        ExecutionEvent,
        ExecutionError,
        StrategyError,
        get_default_executor,
    )

Landed so far: the runner core (`build_work_list` from the seed envelope +
`order_work` FK/R17 ordering), the Arrow boundary + `PandasExecutionAdapter`,
and the three no-backend strategies (passthrough, redact, truncate). Later
slices add the backend-keyed strategies (faker/hash/date_shift/bucketize/
categorical/shuffle/formula/fpe) re-keyed onto S3/S5, composite routing, orphan
policy, and the Faker/FPE per-strategy parallelism.

Spec: docs/v2/sprints/engine-v2/sprint-09-execution-adapter-pandas.md in decoy-platform.
"""

from __future__ import annotations

from decoy_engine.execution._adapter import (
    ExecutionAdapter,
    ExecutionResult,
    StrategyContext,
    StrategyHandler,
)
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._events import ExecutionEvent
from decoy_engine.execution._pandas_adapter import (
    PandasExecutionAdapter,
    get_default_executor,
)
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.execution._substrate import (
    VALID_SUBSTRATES,
    resolve_substrate,
    select_execution_adapter,
)
from decoy_engine.execution.polars import PolarsExecutionAdapter

__all__ = [
    "VALID_SUBSTRATES",
    "ExecutionAdapter",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionResult",
    "PandasExecutionAdapter",
    "PolarsExecutionAdapter",
    "StrategyContext",
    "StrategyError",
    "StrategyHandler",
    "WorkNode",
    "build_work_list",
    "get_default_executor",
    "order_work",
    "resolve_substrate",
    "select_execution_adapter",
]

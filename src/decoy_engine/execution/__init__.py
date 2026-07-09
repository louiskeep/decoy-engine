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

SC5 cross-repo query surface (2026-07-09): `check_out_of_core_compatibility`,
`OutOfCoreCompatibility`/`OutOfCoreRejection`, `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT`,
`FULL_FRAME_REJECT_ROWS_DEFAULT`, and `OUT_OF_CORE_SUPPORTED_STRATEGIES` are
re-exported here so an external caller (decoy-platform's admission estimator)
can ask "is this job out-of-core eligible, and what are the current
thresholds/strategy set" without reaching into the `out_of_core` package's
`_`-prefixed modules directly. This is a thin re-export -- no new routing
logic -- of the exact gate + constants `_pipeline_routing.decide_execution_route`
already consults live. Callers still need a compiled `Plan` + `WorkNode` list
+ `RelationshipGraph` to call `check_out_of_core_compatibility` itself (see
its docstring and `_pipeline_routing.out_of_core_admission` for the
recipe); a caller that cannot afford to compile one pre-read (e.g. an
admission-time estimator that must not read source data) can still consult
the threshold/strategy-set constants for a coarser, config-only proxy.
`OUT_OF_CORE_SUPPORTED_STRATEGIES` tracks the current PAYLOAD-column admitted
set (widens as SC3/SC4/... land); it does not capture the narrower FK
parent-key surface or per-strategy conditional config shapes -- see
`out_of_core._compat.SUPPORTED_STRATEGIES`'s docstring for what it omits.

Spec: docs/v2/sprints/engine-v2/sprint-09-execution-adapter-pandas.md in decoy-platform.
"""

from __future__ import annotations

from decoy_engine.execution._adapter import (
    ExecutionAdapter,
    ExecutionResult,
    StrategyContext,
    StrategyHandler,
)
from decoy_engine.execution._chunked import (
    CHUNK_CONDITIONAL_STRATEGIES,
    CHUNK_SAFE_STRATEGIES,
    check_chunked_compatibility,
    run_mask_pipeline_chunked,
)
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._events import ExecutionEvent
from decoy_engine.execution._pandas_adapter import (
    PandasExecutionAdapter,
    get_default_executor,
)
from decoy_engine.execution._pipeline import classify_table_kinds, run_pipeline
from decoy_engine.execution._planner import (
    EXECUTION_MODES,
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
    ExecutionPlan,
    classify_job,
)
from decoy_engine.execution._row_errors import RowError, RowErrorRecord
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.execution._substrate import (
    VALID_SUBSTRATES,
    resolve_substrate,
    select_execution_adapter,
)
from decoy_engine.execution._transactional_sink import (
    ParquetTransactionalSink,
    TransactionalSink,
)
from decoy_engine.execution.out_of_core import (
    SUPPORTED_STRATEGIES as OUT_OF_CORE_SUPPORTED_STRATEGIES,
)
from decoy_engine.execution.out_of_core import (
    OutOfCoreCompatibility,
    OutOfCoreRejection,
    check_out_of_core_compatibility,
)
from decoy_engine.execution.polars import PolarsExecutionAdapter

__all__ = [
    "CHUNK_CONDITIONAL_STRATEGIES",
    "CHUNK_SAFE_STRATEGIES",
    "EXECUTION_MODES",
    "FULL_FRAME_REJECT_ROWS_DEFAULT",
    "OUT_OF_CORE_SUPPORTED_STRATEGIES",
    "OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT",
    "VALID_SUBSTRATES",
    "ExecutionAdapter",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionPlan",
    "ExecutionResult",
    "OutOfCoreCompatibility",
    "OutOfCoreRejection",
    "PandasExecutionAdapter",
    "ParquetTransactionalSink",
    "PolarsExecutionAdapter",
    "RowError",
    "RowErrorRecord",
    "StrategyContext",
    "StrategyError",
    "StrategyHandler",
    "TransactionalSink",
    "WorkNode",
    "build_work_list",
    "check_chunked_compatibility",
    "check_out_of_core_compatibility",
    "classify_job",
    "classify_table_kinds",
    "get_default_executor",
    "order_work",
    "resolve_substrate",
    "run_mask_pipeline_chunked",
    "run_pipeline",
    "select_execution_adapter",
]

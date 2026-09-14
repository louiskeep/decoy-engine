"""Batch-operator family protocols + the `TableDriver` protocol (Task 4.2, D1).

Plan C2 draws a hard line: a DRIVER-internal stage (`run_mask_pipeline_
chunked`, `run_native_or_oracle_chunked`, the OOC `ChildFkBatchJoiner` /
reorder stream driver) is NOT a batch-operator family -- it belongs to its
driver adapter (D2) and is characterized there. What follows are the five
per-`WorkNode` operator families a driver HOSTS (design doc section 5), each
kept in its REAL shape rather than forced onto one stateless signature:

  * `PandasScalarOperator` mirrors the existing `StrategyHandler` Protocol
    (`execution/_adapter.py`) exactly -- one scalar column, the job-scoped
    `StrategyContext`.
  * `PandasCompositeOperator` mirrors `CompositeHandler.run`
    (`execution/_strategies/_composite.py`): it receives the whole `WorkNode`,
    not a bare column name, because a composite bundle writes multiple output
    columns in one pass.
  * `PandasFkResolveOperator` mirrors `PandasExecutionAdapter._resolve_fk_node`
    (`_pandas_adapter.py`): an FK child node plus its parent map/cache state.
  * `NativeScalarKeyedOperator` mirrors the compiled kernels
    (`native/_kernels_scalar.py`, `native/_kernels_keyed.py`,
    `native/_index_ext.derive_index_batch`): `pa.Array` in, `pa.Array` out,
    reporting through native-call counters/ledgers rather than
    `StrategyContext`.
  * `BoundedPythonOperator` is the pool-build/ML family (Faker pool
    construction, ML inference): a bounded pandas/Arrow batch in, one out;
    its state lives in the pool cache / model state carried inside
    `StrategyContext`, not a separate channel.

None of these protocols are asserted via `isinstance` against the production
handler classes (they use non-uniform signatures on purpose, matching plan
C2's "do NOT force one stateless signature" instruction); they exist so a
later compiler (Task 4.3) has a named, typed shape per family to lower against.

`TableDriver` is a minimal, `runtime_checkable` marker Protocol: a driver
adapter DECLARES its `capabilities` and exposes a `run(...)` method (each
driver's `run` has its own real parameter list, matching its delegate exactly
-- see `drivers/`); `runtime_checkable` here checks method/attribute
PRESENCE only, never signature, so it never forces a shared calling
convention across drivers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd
import pyarrow as pa

from decoy_engine.execution.physical._capabilities import DriverCapabilities
from decoy_engine.generation.pool._events import QualityWarning

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import ColumnSeed


class PandasScalarOperator(Protocol):
    """One scalar masking strategy hosted by a pandas driver (`full_frame`,
    `sequential`, the pandas-oracle side of `chunked`). Structurally identical
    to `execution._adapter.StrategyHandler`; declared here as the seam's own
    named family so a compiler can key operators by family without importing
    the execution-boundary Protocol for a naming purpose."""

    name: str

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]: ...


class PandasCompositeOperator(Protocol):
    """A composite bundle (`_strategies/_composite.CompositeHandler`): receives
    the whole `WorkNode` (multiple output columns), not a bare column name."""

    name: str

    def run(
        self,
        df: pd.DataFrame,
        node: WorkNode,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]: ...


class PandasFkResolveOperator(Protocol):
    """An FK-child resolution node (`PandasExecutionAdapter._resolve_fk_node`):
    maps a child's source key through its parent(s)' source->masked map(s) and
    applies the orphan policy. Threads the job-scoped `StrategyContext` plus
    the parent-map/error caches `run_sequential`/`PandasExecutionAdapter.run`
    already carry across the whole relationship job."""

    def run(
        self,
        node: WorkNode,
        edges: tuple[Any, ...],
        frames: dict[str, pd.DataFrame],
        source_snapshots: dict[tuple[str, str], pd.Series],
        parent_map_cache: dict[Any, dict[Any, Any]],
        node_by_key: dict[Any, WorkNode],
        ctx: StrategyContext,
        *,
        key_error_rows: Any = None,
        errored_keys_cache: dict[Any, dict[Any, str]] | None = None,
    ) -> list[QualityWarning]: ...


class NativeScalarKeyedOperator(Protocol):
    """A compiled kernel operator (`native/_kernels_scalar.py`,
    `native/_kernels_keyed.py`, `native/_index_ext.derive_index_batch`):
    `pa.Array` in, `pa.Array` out, stateless w.r.t. any per-job Python
    context. Errors are coded kernel errors; success is reported through
    native-call counters/ledgers (`NativeRouteLedger` /
    `NativeRouteEvidence`), never `StrategyContext`."""

    def __call__(self, array: pa.Array | pa.ChunkedArray, **kwargs: Any) -> pa.Array: ...


class BoundedPythonOperator(Protocol):
    """A bounded-Python operator (Faker pool build, ML inference, an approved
    hard-tail case): runs inside whichever driver hosts the table -- never a
    table driver itself, never arbitrary Python on a large job (design doc
    section 5). State (pool cache / model state) lives in `StrategyContext`,
    the same channel the pandas-hosted families use."""

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]: ...


@runtime_checkable
class TableDriver(Protocol):
    """A driver adapter: declares its capability metadata and exposes a
    `run(...)` that delegates at the driver's real scope. `runtime_checkable`
    checks attribute/method PRESENCE only (never `run`'s signature, which
    differs per driver by design -- plan C1/C2)."""

    capabilities: DriverCapabilities

    def run(self, *args: Any, **kwargs: Any) -> Any: ...

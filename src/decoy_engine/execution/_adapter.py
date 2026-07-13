"""ExecutionAdapter protocol + ExecutionResult + strategy-handler contract (S9).

The boundary between planning and execution (S9 spec §2). Concrete adapter at
S9 close: PandasExecutionAdapter. The boundary is Arrow-shaped (`pa.Table` in,
`pa.Table` out); what a strategy does internally (pandas Series ops today,
Polars in S12) is invisible to the boundary.

Refinement vs the spec's StrategyHandler signature: the rarely-used run() deps
(registry, pool_cache, relationship_graph, namespace_registry, job_seed) are
bundled into a frozen `StrategyContext` rather than passed as five separate
kwargs, so a no-backend strategy (passthrough/redact/truncate) does not carry
five unused parameters. Scalar handlers receive one `column: str`; the composite
handler (later slice) writes multiple columns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._row_errors import RowError, RowErrorRecord
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import StrategyTimingRecord

if TYPE_CHECKING:
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


@dataclass(frozen=True)
class ExecutionResult:
    """The output of `ExecutionAdapter.run(...)` (S9 spec §2).

    `outputs` maps table name -> masked or generated `pa.Table`. A
    multi-table job (FK parent + child masked in one run) carries one
    entry per table; a single-table job carries one. `output` is a
    convenience accessor for the single-table case (it raises rather
    than guess when the result is multi-table; per the slice-2h
    contract widening, PQ-S9-C).

    FC-1 (2026-06-02) adds `table_kinds`: a per-table `"mask"` or
    `"generate"` classification populated by `run_pipeline` so the
    downstream manifest writer can stamp `kind` per node-run. PO D1
    sub-decision 2026-06-01 (resolved per-table). Empty dict on the
    pre-FC-1 single-kind adapter call paths (`PandasExecutionAdapter.run`
    + `generate_tables`); `run_pipeline` populates it.
    """

    outputs: dict[str, pa.Table]
    timings: tuple[StrategyTimingRecord, ...] = ()
    boundary_conversion_ms: float = 0.0
    warnings: tuple[QualityWarning, ...] = ()
    quality_metrics: dict[str, Any] = field(default_factory=dict)
    table_kinds: dict[str, str] = field(default_factory=dict)
    # Sprint 2 honesty pack (D7): per-row failures recorded by strategy
    # handlers (bucketize/date_shift format_error, code_set mask_error),
    # table-attributed by the adapter's drain point. Additive; default empty
    # tuple leaves every existing ExecutionResult construction unchanged.
    row_errors: tuple[RowErrorRecord, ...] = ()

    @property
    def output(self) -> pa.Table:
        """The single masked table. Raises if the result holds 0 or >1 tables."""
        if len(self.outputs) != 1:
            raise ExecutionError(
                code="multi_table_result_has_no_single_output",
                message=(
                    f"ExecutionResult holds {len(self.outputs)} tables "
                    f"({sorted(self.outputs)}); use .outputs[table] for a multi-table job."
                ),
            )
        return next(iter(self.outputs.values()))


@dataclass(frozen=True)
class StrategyContext:
    """Shared per-job dependencies threaded into every strategy handler.

    `job_seed` (8 bytes) is the sole entropy input deterministic strategies feed
    into `derive` / `derive_index` / `PoolSampler.sample` (S3 removed per-column
    seed integers).
    """

    registry: ProviderRegistry
    pool_cache: PoolCache
    relationship_graph: RelationshipGraph
    namespace_registry: NamespaceRegistry
    job_seed: bytes
    # Sprint 2 honesty pack (D7): the shared per-row-error sink. The
    # dataclass is frozen, but a mutable field's CONTENTS may still be
    # mutated: handlers append RowError instances here; nothing reassigns
    # the field (trap T6 -- `ctx.row_errors = [...]` would raise
    # FrozenInstanceError and, more importantly, would break the drain
    # point's identity-based reference to this list).
    row_errors: list[RowError] = field(default_factory=list)


class StrategyHandler(Protocol):
    """A single scalar masking strategy, invoked through the boundary."""

    name: str

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        """Mutate `df[column]` per the plan; return (df, warnings)."""
        ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """The planning/execution boundary (S9 spec §2). Narrow by design.

    `runtime_checkable` so a second concrete adapter (S11's polars adapter) can
    assert conformance via `isinstance`; this is name-presence only, the real
    signature conformance is the mypy gate.
    """

    adapter_name: str
    adapter_version: str

    def run(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        # DE-03 fail-closed output projection. `unconfigured_column_policy` None
        # resolves to the release-phase default; `generate_output_tables` names
        # the generate-echo tables exempt from the mask plan's declared surface.
        unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
        generate_output_tables: frozenset[str] = frozenset(),
    ) -> ExecutionResult: ...

    def supports_strategy(self, strategy_name: str) -> bool: ...

    def shutdown(self) -> None: ...


def provider_config_to_dict(provider_config: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """Flatten a `ColumnSeed.provider_config` tuple-of-pairs into a dict."""
    return dict(provider_config)


def pandas_column_to_kernel_input(column: pd.Series) -> pa.Array | list[Any]:
    """Convert one pandas column to the masking kernel's expected input.

    The fast path is a whole-column Arrow conversion. A pandas object column
    can legitimately mix Python scalar types (e.g. str and int identifiers in
    one column); Arrow has no single type for that, so
    `pa.array(column, from_pandas=True)` raises. Falling back to a plain
    Python list of scalars (None-normalized for NaN/NaT/None) lets the
    kernel's per-value dispatch (`decoy_engine.kernel._scalar._array_to_pylist`)
    mask every value anyway, matching the pre-kernel per-value handlers this
    replaced (SC1 port) instead of raising on a column shape that used to
    mask successfully.
    """
    try:
        return pa.array(column, from_pandas=True)
    except pa.ArrowException:
        na_mask = column.isna().to_numpy()
        return [None if is_na else value for is_na, value in zip(na_mask, column, strict=True)]

"""Per-run, per-column native disposition for the out-of-core route.

The route masks `hash` columns in `mask_batch` (payload and parent-key columns
alike), and deterministic `categorical` and `bucket_perturb` columns the same way. This module owns two
things that must not live in the drivers: the decision of which backend computes each
such column, and the caller-visible record of that decision
(`ExecutionResult.quality_metrics[HASH_DISPOSITION_KEY]` for hash,
`[CATEGORICAL_DISPOSITION_KEY]` for categorical, `[BUCKET_DISPOSITION_KEY]` for
bucket_perturb). The categorical and bucket decisions live in `_categorical_native` and
`_bucket_native`; this class loads the index kernel once and carries their per-table plans
beside the hash plan so the drivers thread one object.

A column is decided ONCE per table, before its first batch, from the companion
load result and the column's up-front INPUT Arrow type (the hash output is always
a string, so the output type says nothing about the input). The decision is
immutable for the run: a whole column takes one backend, never half of each. A
native column validates every batch against the pinned type and aborts the run on a
mismatch or a missing column instead of switching path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.execution.native._index_ext import (
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.execution.native._text_redact_kernel import load_text_redact_kernel
from decoy_engine.execution.out_of_core._bucket_native import (
    NativeBucket,
    TableBucketPlan,
)
from decoy_engine.execution.out_of_core._bucket_native import decide_table as decide_bucket_table
from decoy_engine.execution.out_of_core._categorical_native import (
    BoundIndexKernel,
    NativeCategorical,
    TableCategoricalPlan,
    decide_table,
)
from decoy_engine.execution.out_of_core._faker_native import (
    BoundFakerKernel,
    NativeFaker,
    TableFakerPlan,
)
from decoy_engine.execution.out_of_core._faker_native import decide_table as decide_faker_table
from decoy_engine.execution.out_of_core._fpe_native import (
    BoundFpeKernel,
    NativeFpe,
    TableFpePlan,
)
from decoy_engine.execution.out_of_core._fpe_native import decide_table as decide_fpe_table
from decoy_engine.execution.out_of_core._hash_native import (
    REASON_UNAVAILABLE,
    BoundHashKernel,
    decline_reason,
)
from decoy_engine.execution.out_of_core._mask import table_seed
from decoy_engine.execution.out_of_core._text_redact_native import (
    BoundTextRedactKernel,
    NativeTextRedact,
    TableTextRedactPlan,
)
from decoy_engine.execution.out_of_core._text_redact_native import (
    decide_table as decide_text_redact_table,
)
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from decoy_engine.execution.out_of_core._faker_pool import OocFakerPoolContext
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge

HASH_DISPOSITION_KEY = "out_of_core_hash"
CATEGORICAL_DISPOSITION_KEY = "out_of_core_categorical"
BUCKET_DISPOSITION_KEY = "out_of_core_bucket_perturb"
FPE_DISPOSITION_KEY = "out_of_core_fpe"
TEXT_REDACT_DISPOSITION_KEY = "out_of_core_text_redact"
FAKER_DISPOSITION_KEY = "out_of_core_faker"
REMAP_DEFERRED = "fk_key_hash_remap_native_deferred"
_NO_KERNEL_RAN = "none"
_REASON_NO_ROWS = "no_rows_hashed"


@dataclass
class HashColumnDecision:
    """The backend one hash column takes for the whole run, and how many rows it hashed.

    `pinned_type` is the input Arrow type a native column is held to."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    rows: int = 0

    @property
    def native(self) -> bool:
        return self.backend == RUST_COMPANION

    def entry(self) -> dict[str, Any]:
        ran = self.rows > 0
        reason = self.reason or (None if ran else _REASON_NO_ROWS)
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": "mask_batch",
            "executed_backend": self.backend if ran else _NO_KERNEL_RAN,
        }
        if reason is not None:
            entry["reason"] = reason
        return entry


class TableHashPlan:
    """The immutable per-column decisions for one table, and the per-batch gate."""

    def __init__(
        self,
        decisions: Mapping[str, HashColumnDecision],
        bound: BoundHashKernel | None,
        categorical: TableCategoricalPlan | None = None,
        bucket: TableBucketPlan | None = None,
        fpe: TableFpePlan | None = None,
        text_redact: TableTextRedactPlan | None = None,
        faker: TableFakerPlan | None = None,
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound
        self.categorical = categorical
        self.bucket = bucket
        self.fpe = fpe
        self.text_redact = text_redact
        self.faker = faker

    @property
    def decisions(self) -> Mapping[str, HashColumnDecision]:
        return self._decisions

    def categorical_for(self, column: str, batch: pa.RecordBatch) -> NativeCategorical | None:
        """The native handle for categorical `column` in `batch`, or None (see `gate`)."""
        return self.categorical.gate(column, batch) if self.categorical is not None else None

    def bucket_for(self, column: str, batch: pa.RecordBatch) -> NativeBucket | None:
        """The native handle for bucket_perturb `column` in `batch`, or None (see `gate`)."""
        return self.bucket.gate(column, batch) if self.bucket is not None else None

    def fpe_for(self, column: str, batch: pa.RecordBatch) -> NativeFpe | None:
        """The native handle for fpe `column` in `batch`, or None (see `gate`)."""
        return self.fpe.gate(column, batch) if self.fpe is not None else None

    def text_redact_for(self, column: str, batch: pa.RecordBatch) -> NativeTextRedact | None:
        """The native handle for text_redact `column` in `batch`, or None (see `gate`)."""
        return self.text_redact.gate(column, batch) if self.text_redact is not None else None

    def faker_for(self, column: str, batch: pa.RecordBatch) -> NativeFaker | None:
        """The native handle for faker `column` in `batch`, or None (see `gate`)."""
        return self.faker.gate(column, batch) if self.faker is not None else None

    def kernel_for(self, column: str, batch: pa.RecordBatch) -> BoundHashKernel | None:
        """The native kernel for `column` in `batch`, or None when it stays on Python.

        A native column is held to its pinned input type: a batch that lacks it or
        carries another type aborts the run, so a column never changes path mid-stream.
        """
        decision = self._decisions.get(column)
        if decision is None:
            return None
        idx = batch.schema.get_field_index(column)
        if decision.native:
            if idx < 0:
                raise ExecutionError(
                    code="out_of_core_hash_column_missing",
                    message=(
                        f"hash column {column!r} of table {decision.table!r} is missing from a "
                        "streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code="out_of_core_hash_input_type_drift",
                    message=(
                        f"hash column {column!r} of table {decision.table!r} was pinned as "
                        f"{decision.pinned_type} but a batch carries "
                        f"{batch.schema.field(idx).type}; the run is aborted rather than "
                        "changing path mid-column."
                    ),
                )
        if idx >= 0:
            decision.rows += batch.num_rows
        return self._bound if decision.native else None


class OocHashRun:
    """Run-scoped hash decisions, collected across every table of one run.

    The companion is loaded at most once per run, on the first hash column that
    needs a decision; a plan with no hash column never touches it.
    """

    def __init__(
        self,
        *,
        native_threads: int | None = None,
        faker_pool: OocFakerPoolContext | None = None,
    ) -> None:
        self._native_threads = native_threads
        # The run-scoped faker pool lifecycle (B3). None when the run builds no
        # faker column; present, it owns the one pool each faker column reuses
        # across batches and bounds that pool against the run budget.
        self._faker_pool = faker_pool
        self._loaded = False
        self._bound: BoundHashKernel | None = None
        self._index_loaded = False
        self._index_bound: BoundIndexKernel | None = None
        self._faker_bound: BoundFakerKernel | None = None
        self._fpe_loaded = False
        self._fpe_bound: BoundFpeKernel | None = None
        self._text_redact_loaded = False
        self._text_redact_bound: BoundTextRedactKernel | None = None
        self._plans: list[TableHashPlan] = []
        self._remap: list[dict[str, Any]] = []

    def _kernel(self) -> BoundHashKernel | None:
        if not self._loaded:
            self._loaded = True
            try:
                self._bound = BoundHashKernel(load_compiled_crypto_kernel(), self._native_threads)
            except CryptoExtensionUnavailableError:
                self._bound = None
        return self._bound

    def _index_kernel(self) -> BoundIndexKernel | None:
        if not self._index_loaded:
            self._index_loaded = True
            try:
                self._index_bound = BoundIndexKernel(
                    load_compiled_index_kernel(), self._native_threads
                )
            except CryptoExtensionUnavailableError:
                self._index_bound = None
        return self._index_bound

    def _faker_kernel(self) -> BoundFakerKernel:
        """The faker index kernel for the run: the compiled companion when present,
        else the pure-Python reference (`reference_index_derivation`).

        Unlike the other strategies, faker never degrades to None: it has no
        in-route Python handler to fall back to, and the reference kernel is
        byte-identical to the compiled one (INDEX_KAT), so a box without the
        companion still selects correctly. The `compiled` flag only records which
        backend ran. Reuses the already-loaded compiled index kernel so a run
        with both categorical and faker pays the companion load once.
        """
        bound = self._faker_bound
        if bound is None:
            index = self._index_kernel()
            bound = (
                BoundFakerKernel(index.kernel, index.native_threads, compiled=True)
                if index is not None
                else BoundFakerKernel(
                    reference_index_derivation(), self._native_threads, compiled=False
                )
            )
            self._faker_bound = bound
        return bound

    def _fpe_kernel(self) -> BoundFpeKernel | None:
        if not self._fpe_loaded:
            self._fpe_loaded = True
            try:
                self._fpe_bound = BoundFpeKernel(load_compiled_fpe_kernel(), self._native_threads)
            except CryptoExtensionUnavailableError:
                self._fpe_bound = None
        return self._fpe_bound

    def _text_redact_kernel(self) -> BoundTextRedactKernel | None:
        """Companion presence for text_redact; the loader returns None, never raises."""
        if not self._text_redact_loaded:
            self._text_redact_loaded = True
            kernel = load_text_redact_kernel()
            self._text_redact_bound = None if kernel is None else BoundTextRedactKernel(kernel)
        return self._text_redact_bound

    def prepare_table(
        self,
        plan: Plan,
        table_name: str,
        source_schema: pa.Schema,
        skip_columns: frozenset[str],
        incoming_edges: Iterable[RelationshipEdge],
        *,
        site: str,
    ) -> TableHashPlan:
        decisions: dict[str, HashColumnDecision] = {}
        seed = table_seed(plan, table_name)
        for column, column_seed in seed.per_column if seed is not None else ():
            if column_seed.strategy != "hash" or column in skip_columns:
                continue
            if source_schema.get_field_index(column) < 0:
                continue
            input_type = source_schema.field(column).type
            bound = self._kernel()
            reason = decline_reason(column, input_type) if bound is not None else REASON_UNAVAILABLE
            native = bound is not None and reason is None
            decisions[column] = HashColumnDecision(
                table_name,
                column,
                RUST_COMPANION if native else PYTHON_SCALAR,
                reason,
                input_type,
            )
        categorical_seeds = {
            column: column_seed
            for column, column_seed in (seed.per_column if seed is not None else ())
            if column_seed.strategy == "categorical"
            and column not in skip_columns
            and source_schema.get_field_index(column) >= 0
        }
        categorical = (
            decide_table(
                table_name,
                source_schema,
                categorical_seeds,
                self._index_kernel() if categorical_seeds else None,
            )
            if categorical_seeds
            else None
        )
        bucket_seeds = {
            column: column_seed
            for column, column_seed in (seed.per_column if seed is not None else ())
            if column_seed.strategy == "bucket_perturb"
            and column not in skip_columns
            and source_schema.get_field_index(column) >= 0
        }
        bucket = (
            decide_bucket_table(
                table_name,
                source_schema,
                bucket_seeds,
                self._index_kernel() if bucket_seeds else None,
            )
            if bucket_seeds
            else None
        )
        fpe_seeds = {
            column: column_seed
            for column, column_seed in (seed.per_column if seed is not None else ())
            if column_seed.strategy == "fpe"
            and column not in skip_columns
            and source_schema.get_field_index(column) >= 0
        }
        fpe = (
            decide_fpe_table(
                table_name,
                source_schema,
                fpe_seeds,
                self._fpe_kernel() if fpe_seeds else None,
            )
            if fpe_seeds
            else None
        )
        text_redact_seeds = {
            column: column_seed
            for column, column_seed in (seed.per_column if seed is not None else ())
            if column_seed.strategy == "text_redact"
            and column not in skip_columns
            and source_schema.get_field_index(column) >= 0
        }
        text_redact = (
            decide_text_redact_table(
                table_name,
                source_schema,
                text_redact_seeds,
                self._text_redact_kernel() if text_redact_seeds else None,
            )
            if text_redact_seeds
            else None
        )
        faker_seeds = {
            column: column_seed
            for column, column_seed in (seed.per_column if seed is not None else ())
            if column_seed.strategy == "faker"
            and column not in skip_columns
            and source_schema.get_field_index(column) >= 0
        }
        faker = (
            decide_faker_table(
                table_name,
                source_schema,
                faker_seeds,
                self._faker_kernel(),
                self._faker_pool,
            )
            if faker_seeds
            else None
        )
        table_plan = TableHashPlan(
            decisions, self._bound, categorical, bucket, fpe, text_redact, faker
        )
        self._plans.append(table_plan)
        self._remap.extend(_remap_entries(plan, table_name, incoming_edges, site))
        return table_plan

    def metrics(self) -> dict[str, Any]:
        """The `quality_metrics` fragment: empty when the run hashed and categorized nothing."""
        entries = [d.entry() for p in self._plans for d in p.decisions.values()]
        entries.extend(self._remap)
        categorical = [
            d.entry()
            for p in self._plans
            if p.categorical is not None
            for d in p.categorical.decisions.values()
        ]
        bucket = [
            d.entry()
            for p in self._plans
            if p.bucket is not None
            for d in p.bucket.decisions.values()
        ]
        fpe = [
            d.entry() for p in self._plans if p.fpe is not None for d in p.fpe.decisions.values()
        ]
        text_redact = [
            d.entry()
            for p in self._plans
            if p.text_redact is not None
            for d in p.text_redact.decisions.values()
        ]
        faker = [
            d.entry()
            for p in self._plans
            if p.faker is not None
            for d in p.faker.decisions.values()
        ]
        return {
            **({HASH_DISPOSITION_KEY: entries} if entries else {}),
            **({CATEGORICAL_DISPOSITION_KEY: categorical} if categorical else {}),
            **({BUCKET_DISPOSITION_KEY: bucket} if bucket else {}),
            **({FPE_DISPOSITION_KEY: fpe} if fpe else {}),
            **({TEXT_REDACT_DISPOSITION_KEY: text_redact} if text_redact else {}),
            **({FAKER_DISPOSITION_KEY: faker} if faker else {}),
        }


def _remap_entries(
    plan: Plan, table_name: str, incoming_edges: Iterable[RelationshipEdge], site: str
) -> list[dict[str, Any]]:
    """REMAP mints orphan keys with the Python hash at the join; recorded as deferred."""
    entries: list[dict[str, Any]] = []
    for edge in incoming_edges:
        if edge.orphan_policy is not OrphanPolicy.REMAP:
            continue
        parent_seed = table_seed(plan, edge.parent_table)
        parent_columns = dict(parent_seed.per_column if parent_seed is not None else ())
        for parent_col, child_col in zip(edge.parent_columns, edge.child_columns, strict=True):
            seed = parent_columns.get(parent_col)
            if seed is None or seed.strategy != "hash":
                continue
            entries.append(
                {
                    "table": table_name,
                    "column": child_col,
                    "path": "fk_remap",
                    "executed_backend": PYTHON_SCALAR,
                    "reason": f"{REMAP_DEFERRED}:{site}",
                }
            )
    return entries


__all__ = [
    "BUCKET_DISPOSITION_KEY",
    "CATEGORICAL_DISPOSITION_KEY",
    "FAKER_DISPOSITION_KEY",
    "FPE_DISPOSITION_KEY",
    "HASH_DISPOSITION_KEY",
    "REMAP_DEFERRED",
    "TEXT_REDACT_DISPOSITION_KEY",
    "HashColumnDecision",
    "OocHashRun",
    "TableHashPlan",
]

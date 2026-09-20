"""Seam-owned identity/scope/publication/operator-family enums (Task 4.2, D1).

Every value here names something the design doc
(docs/plans/2026-09-13-physical-plan-design.md) already describes about the
CURRENT executors; nothing here changes routing or behavior. Kept intentionally
small: complete identifiers and enums only, no `PhysicalNode`/`PhysicalTable`
materialization (that is Task 4.3's compiler).
"""

from __future__ import annotations

from enum import Enum


class DriverId(str, Enum):
    """The four masking drivers plus the synthesis stage (design doc section 4)."""

    FULL_FRAME = "full_frame"
    SEQUENTIAL = "sequential"
    CHUNKED = "chunked"
    OUT_OF_CORE = "out_of_core"
    SYNTHESIS = "synthesis"


class ExecutionScope(str, Enum):
    """What one adapter invocation covers (plan section "What to build", D1).

    `TABLE` is the narrowest unit a driver ever commits independently
    (`native_stream`, and each chunked-table call). `RELATIONSHIP_JOB` is the
    whole dependency-ordered table set a relationship-aware driver commits as
    one unit (`sequential`, `out_of_core` -- see plan C1: neither splits into
    per-table calls, since that would change FK-state retention, write order,
    and commit scope). `SYNTHESIS_STAGE` is the whole generate-kind table set
    `generate_tables()` produces before masking starts. `FULL_FRAME_JOB` is the
    whole multi-table mapping `ExecutionAdapter.run` resolves relationships
    across in one call.
    """

    TABLE = "table"
    RELATIONSHIP_JOB = "relationship_job"
    SYNTHESIS_STAGE = "synthesis_stage"
    FULL_FRAME_JOB = "full_frame_job"


class Residency(str, Enum):
    """A driver's source/working-set residency (design doc section 3/4)."""

    RESIDENT = "resident"
    EVICT_PER_TABLE = "evict_per_table"
    CHUNKED = "chunked"
    STREAM_PER_BATCH = "stream_per_batch"


class PublicationMode(str, Enum):
    """The exact current publication shape a driver call can take (design doc
    section 7). Naming-only: the physical plan "represents the full current
    surface, not a single contract", and this seam changes none of it.

    A driver's `DriverCapabilities.publication_modes` lists every mode that
    driver can produce depending on whether a sink is supplied; which mode a
    given call actually took is not tracked here (that observation belongs to
    the characterization tests, not this seam's static declarations).
    """

    # full_frame / the resident chunked aggregator: a dict of resident
    # `pa.Table`s; a caller-provided sink is silently ignored (accepted
    # today, not an error).
    RESIDENT_SINK_IGNORED = "resident_sink_ignored"
    # the direct chunked entrypoints (`run_mask_pipeline_chunked` /
    # `run_native_or_oracle_chunked`): the caller drives publication by
    # consuming the returned `Iterator[pa.Table]`.
    ITERATOR_PUBLISHER = "iterator_publisher"
    # sequential / native_stream / out_of_core with no sink: a resident dict,
    # collected like `ExecutionAdapter.run`.
    RESIDENT_NO_SINK = "resident_no_sink"
    # sequential with a `TransactionalSink`: whole-table `write` + one
    # job-level `commit`; `abort()` best-effort on any exception.
    WHOLE_TABLE_WRITE_COMMIT = "whole_table_write_commit"
    # sequential with a plain `Callable[[str, pa.Table], None]` sink: the
    # legacy immediate, non-transactional contract (partial output on abort
    # is documented and pinned by test).
    LEGACY_CALLABLE_SINK = "legacy_callable_sink"
    # native_stream / out_of_core with a `TransactionalSink`:
    # `write_batches` per batch, one `commit`, best-effort `abort`.
    BATCH_WRITE_COMMIT = "batch_write_commit"
    # synthesis: resident output the driver itself never publishes or merges;
    # `run_pipeline` owns the merge into masking sources (plan C1).
    STAGE_NOT_PUBLISHED = "stage_not_published"


class Substrate(str, Enum):
    """The `full_frame` driver's two hosted substrate variants (design doc
    section 4/8/C3). The other five drivers are pandas-only."""

    PANDAS = "pandas"
    POLARS = "polars"


class OperatorFamily(str, Enum):
    """Node-operator families a driver hosts (design doc section 5, plan C2).

    Distinct from a driver's own internal stages (`run_mask_pipeline_chunked`,
    `run_native_or_oracle_chunked`, the OOC `ChildFkBatchJoiner`/reorder
    stream driver): those are table-driver-internal and characterized by the
    driver's own adapter, not modeled as operator families here (plan C2).
    """

    PANDAS_SCALAR = "pandas_scalar"
    PANDAS_COMPOSITE = "pandas_composite"
    PANDAS_FK_RESOLVE = "pandas_fk_resolve"
    NATIVE_SCALAR_KEYED = "native_scalar_keyed"
    BOUNDED_PYTHON = "bounded_python"
    # the synthesis driver's own hosted kinds (`Plan.generation`), not
    # `NativePlanNode`/`NodeRequirements` (design doc section 5/9).
    GENERATION = "generation"

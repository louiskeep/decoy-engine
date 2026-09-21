"""`DriverCapabilities`: the per-driver capability declaration (Task 4.2, D1).

One frozen record per driver, naming what design doc section 4's table already
says about it. This is metadata only -- it does not gate, select, or enforce
anything; Task 4.3's compiler is where invariant 1 ("the compiler never
assigns a table to a driver that cannot host every one of its nodes") becomes
an enforced decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from decoy_engine.execution.physical._types import (
    DriverId,
    ExecutionScope,
    OperatorFamily,
    PublicationMode,
    Residency,
    Substrate,
)


@dataclass(frozen=True)
class DriverCapabilities:
    """A driver's capability declaration (design doc section 4).

    `sink_api` names the sink shape(s) the driver's publication modes above
    can consume: `()` when it never accepts one meaningfully (full_frame,
    chunked -- a provided sink is silently ignored, `accepts_sink=False`
    records that it is never wired through, not that passing one errors);
    `("transactional", "legacy_callable")` for sequential (the only driver
    with the legacy plain-callable path); `("transactional",)` for
    native_stream / out_of_core (`write_batches` + `commit`); `()` for
    synthesis (no sink concept at all).
    """

    driver_id: DriverId
    scope: ExecutionScope
    residency: Residency
    publication_modes: tuple[PublicationMode, ...]
    accepts_sink: bool
    sink_api: tuple[str, ...]
    hosted_operator_families: tuple[OperatorFamily, ...]
    can_own_fk_table: bool
    substrate: tuple[Substrate, ...]


_ORACLE_FAMILIES: tuple[OperatorFamily, ...] = (
    OperatorFamily.PANDAS_SCALAR,
    OperatorFamily.PANDAS_COMPOSITE,
    OperatorFamily.PANDAS_FK_RESOLVE,
    OperatorFamily.BOUNDED_PYTHON,
)

CAPABILITIES: dict[DriverId, DriverCapabilities] = {
    DriverId.FULL_FRAME: DriverCapabilities(
        driver_id=DriverId.FULL_FRAME,
        scope=ExecutionScope.FULL_FRAME_JOB,
        residency=Residency.RESIDENT,
        publication_modes=(PublicationMode.RESIDENT_SINK_IGNORED,),
        accepts_sink=False,
        sink_api=(),
        hosted_operator_families=_ORACLE_FAMILIES,
        can_own_fk_table=True,
        substrate=(Substrate.PANDAS,),
    ),
    DriverId.SEQUENTIAL: DriverCapabilities(
        driver_id=DriverId.SEQUENTIAL,
        scope=ExecutionScope.RELATIONSHIP_JOB,
        residency=Residency.EVICT_PER_TABLE,
        publication_modes=(
            PublicationMode.RESIDENT_NO_SINK,
            PublicationMode.WHOLE_TABLE_WRITE_COMMIT,
            PublicationMode.LEGACY_CALLABLE_SINK,
        ),
        accepts_sink=True,
        sink_api=("transactional", "legacy_callable"),
        hosted_operator_families=_ORACLE_FAMILIES,
        can_own_fk_table=True,
        substrate=(Substrate.PANDAS,),
    ),
    DriverId.CHUNKED: DriverCapabilities(
        driver_id=DriverId.CHUNKED,
        scope=ExecutionScope.TABLE,
        residency=Residency.CHUNKED,
        publication_modes=(
            PublicationMode.RESIDENT_SINK_IGNORED,
            PublicationMode.ITERATOR_PUBLISHER,
        ),
        accepts_sink=False,
        sink_api=(),
        hosted_operator_families=(
            OperatorFamily.PANDAS_SCALAR,
            OperatorFamily.PANDAS_COMPOSITE,
            OperatorFamily.NATIVE_SCALAR_KEYED,
            OperatorFamily.BOUNDED_PYTHON,
        ),
        can_own_fk_table=False,
        substrate=(Substrate.PANDAS,),
    ),
    DriverId.OUT_OF_CORE: DriverCapabilities(
        driver_id=DriverId.OUT_OF_CORE,
        scope=ExecutionScope.RELATIONSHIP_JOB,
        residency=Residency.STREAM_PER_BATCH,
        publication_modes=(
            PublicationMode.RESIDENT_NO_SINK,
            PublicationMode.BATCH_WRITE_COMMIT,
        ),
        accepts_sink=True,
        sink_api=("transactional",),
        hosted_operator_families=(OperatorFamily.NATIVE_SCALAR_KEYED,),
        can_own_fk_table=True,
        substrate=(Substrate.PANDAS,),
    ),
    DriverId.SYNTHESIS: DriverCapabilities(
        driver_id=DriverId.SYNTHESIS,
        scope=ExecutionScope.SYNTHESIS_STAGE,
        residency=Residency.RESIDENT,
        publication_modes=(PublicationMode.STAGE_NOT_PUBLISHED,),
        accepts_sink=False,
        sink_api=(),
        hosted_operator_families=(OperatorFamily.GENERATION,),
        can_own_fk_table=False,
        substrate=(Substrate.PANDAS,),
    ),
}

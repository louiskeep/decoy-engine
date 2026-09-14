"""`synthesis` stage adapter (Task 4.2, D2).

Wraps `generation._plan_entry.generate_tables` (design doc section 4/9): the
whole generation stage, run before masking. Returns the generated
`dict[str, pa.Table]` UNCHANGED. This adapter does not merge the result into
masking sources and does not publish anything -- `run_pipeline` owns the merge
and stitch precedence into the mask stage's sources (`_pipeline.py`), which
stays completely outside this seam.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution.physical._capabilities import CAPABILITIES
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._types import DriverId, ExecutionScope
from decoy_engine.generation.synthesize import generate_tables


class SynthesisStageAdapter:
    """Pure delegation to `generate_tables`. Never merges into mask sources,
    never publishes; the resident output is returned exactly as produced."""

    capabilities = CAPABILITIES[DriverId.SYNTHESIS]

    def __init__(self) -> None:
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        plan: Any,
        derive_key: Any = None,
        instance_default_locale: str | None = None,
    ) -> dict[str, pa.Table]:
        self.last_invocation = SeamContext(
            driver_id=DriverId.SYNTHESIS,
            scope=ExecutionScope.SYNTHESIS_STAGE,
            tables=(),
        )
        return generate_tables(plan, derive_key, instance_default_locale)

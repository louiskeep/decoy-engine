"""D2 output records: `PhysicalPlan` / `PhysicalTable` / `PhysicalNode` /
`SynthesisStage` / `RejectedAlternative`, per the approved Task 4.1 design
(docs/plans/2026-09-13-physical-plan-design.md section 3).

Scoped to what Task 4.3 actually needs to prove: driver selection and its
coded reasons, which the D4 preflight-decision-equivalence harness verifies.
`PhysicalNode` is a real, minimal construction (via the existing `_runner.
build_work_list` + `native._requirements.requirements_for` + `native.
_provider_class.classify_provider`, the same construction mechanism the
design doc names) -- not a full per-node dispatch surface. Its own
correctness is a CONSTRUCTION, not a selection (design doc section 5); the
design explicitly defers per-node equivalence to Task 4.4, so it carries no
resource estimate, determinism binding, or diagnostic-obligation set here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from decoy_engine.execution.physical._types import DriverId

__all__ = [
    "PhysicalNode",
    "PhysicalPlan",
    "PhysicalTable",
    "RejectedAlternative",
    "SynthesisStage",
]


@dataclass(frozen=True)
class RejectedAlternative:
    """One faster-lane entry in a table's `rejected_alternatives` (design doc
    section 3). `attempted=False` means an earlier gate excluded this lane
    before any live check ran for it (e.g. `auto_chunk=False` never invokes
    `classify_job`); the lane is still recorded, never silently dropped, per
    the design doc's "unattempted" contract.
    """

    driver: DriverId
    reason: str
    attempted: bool


@dataclass(frozen=True)
class PhysicalNode:
    """One masking work node's operator lowering (design doc section 5),
    constructed -- not selected -- via `native._requirements.requirements_
    for` (`fallback_policy`) and `native._provider_class.classify_provider`
    (`provider_class`, faker/custom-provider nodes only)."""

    node_id: str
    table: str
    columns: tuple[str, ...]
    kind: str
    strategy: str
    fallback_policy: Literal["native", "python_only"]
    provider_class: str | None


@dataclass(frozen=True)
class SynthesisStage:
    """The `generate_tables()` stage (design doc section 4): generate-kind
    tables, produced before masking, merged into the mask stage's sources.
    Operators map to `Plan.generation`, never `NativePlanNode`/
    `NodeRequirements` (which exclude generation by construction)."""

    tables: tuple[str, ...]


@dataclass(frozen=True)
class PhysicalTable:
    """One masking table's driver assignment (design doc section 3)."""

    table: str
    driver: DriverId
    driver_reason: str
    driver_reason_detail: str | None
    rejected_alternatives: tuple[RejectedAlternative, ...]
    relationship_role: Literal["independent", "parent", "child"]
    substrate: str
    nodes: tuple[PhysicalNode, ...]


@dataclass(frozen=True)
class PhysicalPlan:
    """The frozen, per-job physical plan (design doc section 3)."""

    engine_version: str
    plan_hash: str
    synthesis: SynthesisStage | None
    tables: tuple[PhysicalTable, ...]

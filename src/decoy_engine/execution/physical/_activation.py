"""Task 4.5 D6: the frozen PLANNED activation overlay for the unified-slice
production lane.

D6 rejected adding an `owner` field to the 4.3 `PhysicalPlan`
(`PhysicalTable.driver` already means the selected table driver -- a second
field would create two competing ownership truths). Instead this module
builds a small, separate, frozen record capturing what was PLANNED before
execution: the base plan identity, the legacy routing disposition the job
arrived with, which lane is about to own the table, and the exact node/
operator identities admission selected -- with its own deterministic hash
that folds in `unified_slice_enabled` so a flag-off activation object can
never collide with a flag-on one.

This overlay is PLANNED activation only (Settled decision 1). It is never,
by itself, proof that anything executed -- that proof is D7's POST-execution
node evidence (`_unified_slice.py`), built from the coordinator's returned
`route_evidence` after a successful run, never from this overlay alone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from decoy_engine.execution.physical._plan import PhysicalPlan

__all__ = [
    "ACTIVATION_VERSION",
    "AdmittedNode",
    "UnifiedSliceActivation",
    "build_unified_slice_activation",
]

# Bumped only if the overlay's own hashed shape changes (a field added or
# removed); unrelated to `PhysicalPlan.plan_hash`, which already versions the
# compiled plan it is built from.
ACTIVATION_VERSION = 1

_PLANNED_OWNER = "unified_slice"


@dataclass(frozen=True)
class AdmittedNode:
    """One admitted node's identity, frozen onto the overlay so the planned
    node/operator set is fixed before the coordinator ever runs."""

    node_id: str
    operator_id: str


@dataclass(frozen=True)
class UnifiedSliceActivation:
    """The planned activation overlay, built once per admitted job, before
    the coordinator is invoked.

    `legacy_disposition` names the exact legacy routing facts that made this
    table eligible (`"full_frame"`, mirroring `PhysicalTable.driver_reason`'s
    style) -- recorded so a later audit of `plan_hash` + this overlay can
    reconstruct why the table was ever a candidate, without re-deriving the
    live route decision. `planned_owner` is a plain string, not `DriverId`:
    the unified slice is not one of the six drivers `DriverId` enumerates
    (D6 rejected widening that enum for this task), it is a NEW lane that
    happens to admit only what `DriverId.FULL_FRAME` would have taken.
    """

    plan_hash: str
    legacy_disposition: str
    planned_owner: str
    activation_version: int
    activation_reason: str
    table: str
    admitted_nodes: tuple[AdmittedNode, ...]
    activation_hash: str


def build_unified_slice_activation(
    physical_plan: PhysicalPlan,
    *,
    table: str,
    legacy_disposition: str,
    unified_slice_enabled: bool,
) -> UnifiedSliceActivation:
    """Build the frozen overlay for `table` from the already-compiled
    `physical_plan` (D4's live-facts `PhysicalPlanInputs` -> `compile_
    physical_plan` output). Admission has already verified every node for
    `table` carries a non-None `execution` binding by the time this is
    called; nodes are re-filtered here defensively rather than trusted
    blindly, so a future admission-check gap fails toward an EMPTY admitted
    set (caught downstream by D7's coverage check) instead of silently
    including an unbound node.
    """
    physical_table = next(t for t in physical_plan.tables if t.table == table)
    admitted_nodes = tuple(
        AdmittedNode(node_id=node.node_id, operator_id=node.execution.operator_id)
        for node in physical_table.nodes
        if node.execution is not None
    )
    activation_hash = _compute_activation_hash(
        plan_hash=physical_plan.plan_hash,
        legacy_disposition=legacy_disposition,
        table=table,
        admitted_nodes=admitted_nodes,
        unified_slice_enabled=unified_slice_enabled,
    )
    return UnifiedSliceActivation(
        plan_hash=physical_plan.plan_hash,
        legacy_disposition=legacy_disposition,
        planned_owner=_PLANNED_OWNER,
        activation_version=ACTIVATION_VERSION,
        activation_reason=f"{legacy_disposition}:unified_slice_admitted",
        table=table,
        admitted_nodes=admitted_nodes,
        activation_hash=activation_hash,
    )


def _compute_activation_hash(
    *,
    plan_hash: str,
    legacy_disposition: str,
    table: str,
    admitted_nodes: tuple[AdmittedNode, ...],
    unified_slice_enabled: bool,
) -> str:
    """A free function, not a method (mutmut 3.7 skips decorated-class
    bodies; see the mutation-tooling note in `_inputs.compute_plan_hash`,
    which this mirrors). Includes `unified_slice_enabled` explicitly (D6) so
    an activation built while the flag reads False can never hash equal to
    one built while it reads True, even given an otherwise-identical plan.
    """
    parts: tuple[object, ...] = (
        plan_hash,
        legacy_disposition,
        _PLANNED_OWNER,
        ACTIVATION_VERSION,
        table,
        tuple((n.node_id, n.operator_id) for n in admitted_nodes),
        unified_slice_enabled,
    )
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()

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
from typing import Any, Literal

import pyarrow as pa

from decoy_engine.execution.native._operator_params import (
    BucketPerturbParams,
    CategoricalParams,
    DateShiftParams,
    GroupKeyParams,
    OperatorParams,
)
from decoy_engine.execution.physical._types import DriverId

__all__ = [
    "ExecutionBinding",
    "KeyBinding",
    "PhysicalNode",
    "PhysicalPlan",
    "PhysicalTable",
    "PoolBinding",
    "RejectedAlternative",
    "SynthesisStage",
]


@dataclass(frozen=True)
class KeyBinding:
    """Non-secret key reference for a keyed slice node (Task 4.4 C0).

    Carries ONLY the key-source TOKEN (`mask_key`, or `job_seed` for a position-keyed
    Faker, whose draw never re-identifies a source value) plus the namespace the draw
    keys on --
    never a `KeyProvider` object and never key bytes. The resolved
    `KeyProvider` and the resolved mask-key bytes live EXCLUSIVELY in the
    runtime `ShadowContext` (`_shadow_context.py`), passed at execution, never
    stored on this frozen binding or the plan it hangs off of (guarded by
    `tests/physical/test_shadow_no_secret_serialization.py`).
    """

    key_source: str
    namespace: str


@dataclass(frozen=True)
class PoolBinding:
    """Non-secret pool reference for a faker slice node (Task 4.6 slice 1).

    Carries ONLY `provider` and `plan_pool_size` -- the two facts a pool's
    IDENTITY is not otherwise recoverable from at execution time. Locale and
    build_config are NOT duplicated here: they are resolved solely inside
    `resolve_faker_pool_identity` from `dict(binding.resolved_config)`, the
    same way the native chunked route resolves them, so there is exactly one
    place that split lives. The pool namespace is the CONFIGURED one, read from the bound
    `FakerParams.namespace` (it can be None, and for a position-keyed Faker it differs from
    `KeyBinding.namespace`, which names the selection namespace).
    """

    provider: str
    plan_pool_size: int


@dataclass(frozen=True)
class ExecutionBinding:
    """Slice-only immutable execution binding attached to a `PhysicalNode` at
    COMPILE time (Task 4.4 C0; design doc section 8.1's per-node field list,
    scoped to the five strategies the shadow coordinator admits -- passthrough,
    redact, truncate, keyed hash, and (Task 4.6 slice 1) deterministic faker
    over the frozen C1 provider allowlist). Populated only when the node's
    compiled config was admitted to the corresponding native kernel or pool
    route; every other node leaves `PhysicalNode.execution` unset.

    `resolved_config` is the node's fully-resolved provider-config as a
    sorted tuple of (key, value) pairs (e.g. truncate's `length`/`keep`,
    including the legacy `from_end` -> `keep` resolution) -- never raw
    key/secret material. `determinism_family`/`determinism_version` name the
    draw-site family (`native/_capabilities.capabilities_for(strategy).
    draw_family`, `None` for the three unkeyed transforms) and the plan's
    `seed_protocol_version`. `key_binding` is set for the keyed-hash node and
    the faker node (both draw from `mask_key`). `pool_binding` is set only
    for the faker node -- `None` for every other operator, so the four
    scalar operators built before Task 4.6 carry an unchanged shape.
    `diagnostic_obligations` mirrors `NodeRequirements.diagnostic_reducers`
    (empty for this slice's zero-diagnostic strategies). `batch_estimate` is
    the resident source table's row count when known, for reporting only --
    it does not gate anything.

    `params` carries the node's resolved operator parameters (`native/_operator_params.py`):
    the defaults, coercions, namespace and, for categorical, the prepared categories and
    CDF, resolved once at bind time by the same resolver the chunked route calls. It is
    `None` only on a hand-built binding. The marker properties below answer "is this a bound
    X node" field by field, so a half-built binding behaves as it did when each parameter
    was its own field.
    """

    operator_id: str
    operator_reason: str
    resolved_config: tuple[tuple[str, Any], ...]
    input_schema: pa.Schema
    output_schema: pa.Schema
    determinism_family: str | None
    determinism_version: int
    key_binding: KeyBinding | None
    diagnostic_obligations: tuple[str, ...]
    required_prepasses: tuple[str, ...]
    batch_estimate: int | None
    pool_binding: PoolBinding | None = None
    params: OperatorParams | None = None
    # The node's `when:` predicate; the masked kernel step covers the admitted value-keyed strategies.
    # A binding that carries one must never run without a row mask.
    when_expression: str | None = None

    @property
    def needs_index_kernel(self) -> bool:
        """Whether this node draws through the compiled `derive_index_batch`
        kernel: faker (pool selection), categorical (either keying), bucket_perturb,
        or date_shift. The coordinator loads the kernel once per run for any such
        node. Deliberately separate from `pool_binding`, which alone gates POOL
        RESOLUTION: a date_shift node needs the kernel but has no pool."""
        params = self.params
        return (
            self.pool_binding is not None
            or isinstance(params, CategoricalParams)
            or (isinstance(params, BucketPerturbParams) and params.bucket is not None)
            or (isinstance(params, DateShiftParams) and params.date_format is not None)
        )

    @property
    def categorical_deterministic(self) -> bool:
        """True for a bound source-keyed categorical node; a position-keyed one
        (`prepared.positional`) is keyed on the row ordinal and answers False."""
        return isinstance(self.params, CategoricalParams) and not self.params.prepared.positional

    @property
    def group_key_sibling(self) -> str | None:
        """The sibling column a bound group_key node keys on, or `None`. The coordinator's
        input feed and unified admission's resident-type check both read it from here."""
        params = self.params
        if isinstance(params, GroupKeyParams) and params.group_by is not None:
            return params.group_by
        return None


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
    # Task 4.4 C0: set only for the four slice strategies this task shadows,
    # and only when the node's resolved config was admitted to the matching
    # native kernel. `None` for every other node (out of scope for 4.4).
    execution: ExecutionBinding | None = None


@dataclass(frozen=True)
class SynthesisStage:
    """The `generate_tables()` stage (design doc section 4): generate-kind
    tables, produced before masking, merged into the mask stage's sources.
    Operators map to `Plan.generation`, never `NativePlanNode`/
    `NodeRequirements` (which exclude generation by construction).

    `config_digest` (Task 4.6 slice 5a) is
    `sha256(Plan.generation.config_json.encode("utf-8")).hexdigest()`,
    populated by the compiler. The shadow coordinator's generation-dispatch
    admission gate recomputes the same digest from `ShadowContext.plan.
    generation.config_json` at dispatch time and requires an exact match --
    closing an identity hole `pipeline_config_hash` cannot (that hash
    deliberately excludes sources/targets, so two Plans compiled from
    different generate configs but the same table names could otherwise
    collide there).
    """

    tables: tuple[str, ...]
    config_digest: str


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

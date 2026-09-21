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

    Carries ONLY the `KeySource` TOKEN (`native/_capabilities.py:51`'s
    `Literal["mask_key", "generation_seed"]`) plus the column's namespace --
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
    place that split lives. Namespace is reused from `KeyBinding.namespace`,
    never copied a second time onto this binding.
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

    Phase 5 Track B adds the deterministic-categorical carriers. `categorical_
    deterministic` is set True only for a bound categorical node, sourced from
    `ColumnSeed.deterministic` (equal by construction to `is_deterministic_
    categorical(config)`) -- the runtime asserts it before invoking the native
    operator so the unseeded path can never reach it under a wiring bug.
    `categorical_categories` is the resolved STRING category tuple; `categorical
    _cdf` is the resolved integer CDF (`_build_cdf`) for the weighted variant,
    `None` for the uniform one. All four default so every non-categorical
    construction is unchanged.
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
    # Task 4.6 slice 1: LAST field before Phase 5, defaulted to None, so every
    # pre-existing construction (the four scalar operators) is unchanged.
    pool_binding: PoolBinding | None = None
    # Phase 5 Track B (deterministic categorical); all defaulted, so no
    # pre-existing ExecutionBinding construction changes shape.
    categorical_deterministic: bool = False
    categorical_categories: tuple[str, ...] | None = None
    categorical_cdf: tuple[int, ...] | None = None
    # Phase 5 S-slate (native bucket_perturb); both defaulted, so no pre-existing
    # ExecutionBinding construction changes shape. `bucket_perturb_bucket` is the
    # resolved bucket name (week/month/quarter) and doubles as the "this is a
    # bound bucket_perturb node" marker the coordinator's index-kernel-load check
    # reads; `bucket_perturb_date_format` is the resolved explicit format string.
    # A bound bucket_perturb node also carries a `KeyBinding` (it is source-keyed,
    # like hash/categorical), reusing the `key_binding` field above.
    bucket_perturb_bucket: str | None = None
    bucket_perturb_date_format: str | None = None


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

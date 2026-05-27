"""Plan module: compile pipeline config + profile into a versioned plan artifact.

Public API:

    from decoy_engine.plan import (
        Plan,
        PlanCompileError,
        S1_STUB_REGISTRY,
        compile_plan,
        plan_from_yaml,
        plan_to_yaml,
    )

`compile_plan(config, profile, *, decoy_engine_version)` is the keystone
S1 deliverable. Given a parsed pipeline config dict and a Profile (from
`decoy_engine.profile`), it runs five compile-time checks and produces
a frozen `Plan` dataclass. The plan is YAML-serializable; same input
produces byte-identical output (the S1 determinism contract).

S1 ships the plumbing plus five foundational checks. S2-S9 add per-module
rules. See the [compile-check ownership table] in
docs/v2/sprints/engine-v2/sprint-01-profile-plan-and-fixtures.md for the
canonical sprint-by-sprint map.

Source patterns: Plan shape draws from SQL compiler IR + dbt's manifest
model (immutable artifact, byte-stable serialization, audit-trail-first).
PlanCompileError follows clang-style error rendering: code + path +
human-readable message. Determinism contract mirrors PERF.BASE.2.
"""

from __future__ import annotations

from decoy_engine.plan._compile import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.plan._types import (
    ColumnSeed,
    GroupSeed,
    NamespaceBinding,
    OrderingNode,
    Plan,
    PlanCompileResult,
    PlanRelationship,
    PlanRelationshipEnd,
    SeedEnvelope,
    TableSeed,
)

__all__ = [
    "ColumnSeed",
    "GroupSeed",
    "NamespaceBinding",
    "OrderingNode",
    "Plan",
    "PlanCompileError",
    "PlanCompileResult",
    "PlanRelationship",
    "PlanRelationshipEnd",
    "SeedEnvelope",
    "TableSeed",
    "compile_plan",
    "plan_from_yaml",
    "plan_to_yaml",
]

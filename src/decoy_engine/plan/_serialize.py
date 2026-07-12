"""YAML serialization for Plan.

Determinism contract: same Plan -> byte-identical YAML. PyYAML's
`safe_dump` with `sort_keys=False` preserves the order the dataclass
declared. Tuples serialize as lists; ColumnSeed.provider_config
(a tuple of pairs) serializes as a dict-shaped block.

`plan_to_yaml(plan)` produces the manifest-ready string.
`plan_from_yaml(s)` parses it back; round-trip equality holds.
"""

from __future__ import annotations

from typing import Any

import yaml

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


def plan_to_yaml(plan: Plan) -> str:
    """Serialize a Plan to YAML."""
    rendered: str = yaml.safe_dump(_plan_to_dict(plan), sort_keys=False, default_flow_style=False)
    return rendered


def plan_from_yaml(s: str) -> Plan:
    """Deserialize a YAML string back into a Plan."""
    data = yaml.safe_load(s)
    if not isinstance(data, dict):
        raise ValueError(
            f"plan_from_yaml: top-level YAML must be a mapping, got {type(data).__name__}"
        )
    return _plan_from_dict(data)


# ---------------------------------------------------------------------
# To-dict
# ---------------------------------------------------------------------


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "plan_version": plan.plan_version,
        "seed_protocol_version": plan.seed_protocol_version,
        "engine_version": plan.engine_version,
        "pipeline_config_hash": plan.pipeline_config_hash,
        "profile_hash": plan.profile_hash,
        "seed_envelope": _seed_envelope_to_dict(plan.seed_envelope),
        "relationships": [_relationship_to_dict(r) for r in plan.relationships],
        "namespaces": {ns.namespace: _namespace_to_dict(ns) for ns in plan.namespaces},
        "ordering": [_ordering_to_dict(o) for o in plan.ordering],
        "plan_compile": _plan_compile_to_dict(plan.plan_compile),
    }


def _seed_envelope_to_dict(env: SeedEnvelope) -> dict[str, Any]:
    return {
        # Post-S3 plan-schema delta: job_seed is bytes; serialize as a hex
        # string for YAML round-trip (bytes are not natively YAML-typed).
        # Length is always 8 bytes -> 16 hex chars.
        "job_seed": env.job_seed.hex(),
        "per_table": {name: _table_seed_to_dict(ts) for (name, ts) in env.per_table},
    }


def _table_seed_to_dict(ts: TableSeed) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if ts.per_column:
        out["per_column"] = {name: _column_seed_to_dict(cs) for (name, cs) in ts.per_column}
    if ts.per_group:
        out["per_group"] = {name: _group_seed_to_dict(gs) for (name, gs) in ts.per_group}
    return out


def _column_seed_to_dict(cs: ColumnSeed) -> dict[str, Any]:
    out: dict[str, Any] = {
        "namespace": cs.namespace,
        "strategy": cs.strategy,
        "provider": cs.provider,
        "backend_type": cs.backend_type,
        "backend_version": cs.backend_version,
        "cardinality_mode": cs.cardinality_mode,
        "deterministic": cs.deterministic,
    }
    if cs.provider_config:
        out["provider_config"] = dict(cs.provider_config)
    if cs.coherent_with:
        out["coherent_with"] = list(cs.coherent_with)
    # MG-1 S1 (2026-06-01): emit the GDPR technique class onto the
    # plan manifest so the operator can audit which columns map to
    # which class (pseudonymisation / anonymisation / synthetic /
    # passthrough). Omit when unset so legacy plans round-trip.
    if cs.technique_class is not None:
        out["technique_class"] = cs.technique_class
    # MG-3 / M3 (2026-05-31): emit when: onto the manifest only when
    # set; legacy plans omit the field and round-trip unchanged.
    if cs.when is not None:
        out["when"] = cs.when
    # MG-6 D1 (2026-05-31): same round-trip pattern as technique_class.
    if cs.distribution_behavior is not None:
        out["distribution_behavior"] = cs.distribution_behavior
    return out


def _group_seed_to_dict(gs: GroupSeed) -> dict[str, Any]:
    return {
        "namespace": gs.namespace,
        "coherent_columns": list(gs.coherent_columns),
    }


def _relationship_to_dict(rel: PlanRelationship) -> dict[str, Any]:
    out: dict[str, Any] = {
        "parent": {"table": rel.parent.table, "columns": list(rel.parent.columns)},
        "children": [{"table": c.table, "columns": list(c.columns)} for c in rel.children],
        "orphan_policy": rel.orphan_policy,
    }
    if rel.namespace is not None:
        out["namespace"] = rel.namespace
    return out


def _namespace_to_dict(ns: NamespaceBinding) -> dict[str, Any]:
    return {
        "declared_by": [f"{t}.{'__'.join(cols)}" for (t, cols) in ns.declared_by],
    }


def _ordering_to_dict(o: OrderingNode) -> dict[str, Any]:
    return {"table": o.table, "columns": list(o.columns)}


def _plan_compile_to_dict(pc: PlanCompileResult) -> dict[str, Any]:
    return {
        "warnings": list(pc.warnings),
        "errors": list(pc.errors),
        "checks_passed": list(pc.checks_passed),
        "checks_skipped": list(pc.checks_skipped),
    }


# ---------------------------------------------------------------------
# From-dict
# ---------------------------------------------------------------------


def _plan_from_dict(data: dict[str, Any]) -> Plan:
    return Plan(
        plan_version=data["plan_version"],
        seed_protocol_version=data["seed_protocol_version"],
        engine_version=data["engine_version"],
        pipeline_config_hash=data["pipeline_config_hash"],
        profile_hash=data["profile_hash"],
        seed_envelope=_seed_envelope_from_dict(data["seed_envelope"]),
        relationships=tuple(_relationship_from_dict(r) for r in data.get("relationships", [])),
        namespaces=tuple(
            _namespace_from_dict(name, body)
            for (name, body) in (data.get("namespaces") or {}).items()
        ),
        ordering=tuple(_ordering_from_dict(o) for o in data.get("ordering", [])),
        plan_compile=_plan_compile_from_dict(data.get("plan_compile", {})),
    )


def _seed_envelope_from_dict(data: dict[str, Any]) -> SeedEnvelope:
    per_table_raw = data.get("per_table", {}) or {}
    per_table = tuple((name, _table_seed_from_dict(body)) for (name, body) in per_table_raw.items())
    # job_seed serialized as 16-char hex (8 bytes); see _seed_envelope_to_dict.
    return SeedEnvelope(job_seed=bytes.fromhex(data["job_seed"]), per_table=per_table)


def _table_seed_from_dict(data: dict[str, Any]) -> TableSeed:
    per_column_raw = data.get("per_column", {}) or {}
    per_group_raw = data.get("per_group", {}) or {}
    return TableSeed(
        per_column=tuple(
            (name, _column_seed_from_dict(body)) for (name, body) in per_column_raw.items()
        ),
        per_group=tuple(
            (name, _group_seed_from_dict(body)) for (name, body) in per_group_raw.items()
        ),
    )


def _column_seed_from_dict(data: dict[str, Any]) -> ColumnSeed:
    provider_config_raw = data.get("provider_config", {}) or {}
    coherent_with_raw = data.get("coherent_with", []) or []
    return ColumnSeed(
        namespace=data.get("namespace"),
        strategy=data["strategy"],
        provider=data["provider"],
        backend_type=data["backend_type"],
        backend_version=data["backend_version"],
        cardinality_mode=data["cardinality_mode"],
        deterministic=bool(data.get("deterministic", False)),
        provider_config=tuple(sorted(provider_config_raw.items())),
        coherent_with=tuple(coherent_with_raw),
        # MG-1 S1 (2026-06-01): legacy plans without the field
        # deserialize as None; new plans round-trip the class.
        technique_class=data.get("technique_class"),
        # MG-3 / M3 (2026-05-31): same round-trip pattern as the
        # technique class; legacy plans default to None.
        when=data.get("when"),
        # MG-6 D1 (2026-05-31): same pattern.
        distribution_behavior=data.get("distribution_behavior"),
    )


def _group_seed_from_dict(data: dict[str, Any]) -> GroupSeed:
    return GroupSeed(
        namespace=data["namespace"],
        coherent_columns=tuple(data.get("coherent_columns", []) or []),
    )


def _relationship_from_dict(data: dict[str, Any]) -> PlanRelationship:
    parent_data = data["parent"]
    parent = PlanRelationshipEnd(table=parent_data["table"], columns=tuple(parent_data["columns"]))
    children = tuple(
        PlanRelationshipEnd(table=c["table"], columns=tuple(c["columns"]))
        for c in data.get("children", [])
    )
    return PlanRelationship(
        parent=parent,
        children=children,
        orphan_policy=data["orphan_policy"],
        namespace=data.get("namespace"),
    )


def _namespace_from_dict(name: str, body: dict[str, Any]) -> NamespaceBinding:
    declared_by_raw = body.get("declared_by", []) or []
    declared_by: list[tuple[str, tuple[str, ...]]] = []
    for entry in declared_by_raw:
        if isinstance(entry, str) and "." in entry:
            table, col_part = entry.split(".", 1)
            cols = tuple(col_part.split("__")) if "__" in col_part else (col_part,)
            declared_by.append((table, cols))
    return NamespaceBinding(
        namespace=name,
        declared_by=tuple(declared_by),
    )


def _ordering_from_dict(data: dict[str, Any]) -> OrderingNode:
    return OrderingNode(table=data["table"], columns=tuple(data["columns"]))


def _plan_compile_from_dict(data: dict[str, Any]) -> PlanCompileResult:
    return PlanCompileResult(
        checks_passed=tuple(data.get("checks_passed", []) or []),
        checks_skipped=tuple(data.get("checks_skipped", []) or []),
        warnings=tuple(data.get("warnings", []) or []),
        errors=tuple(data.get("errors", []) or []),
    )

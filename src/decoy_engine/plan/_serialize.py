"""YAML serialization for Plan.

Determinism contract: same Plan -> byte-identical YAML. PyYAML's
`safe_dump` with `sort_keys=False` preserves the order the dataclass
declared. Tuples serialize as lists; ColumnSeed.provider_config
(a tuple of pairs) serializes as a dict-shaped block.

`plan_to_yaml(plan)` produces the manifest-ready string.
`plan_from_yaml(s)` parses it back; round-trip equality holds.

Namespace declared_by wire format (M1 fix, session 11):
  Serialized as a list of [table, [col1, col2, ...]] entries.
  The old "table.col1__col2" string format used '__' as a column
  separator, which was ambiguous when column names themselves contained
  '__'.  The structured list format is unambiguous.  A legacy 'table.col'
  fallback (no '__' in col) is accepted on deserialization for plans
  serialized by S1-era builds before this fix.
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
        "job_seed": env.job_seed,
        "per_table": {name: _table_seed_to_dict(ts) for (name, ts) in env.per_table},
    }


def _table_seed_to_dict(ts: TableSeed) -> dict[str, Any]:
    out: dict[str, Any] = {"table_seed": ts.table_seed}
    if ts.per_column:
        out["per_column"] = {name: _column_seed_to_dict(cs) for (name, cs) in ts.per_column}
    if ts.per_group:
        out["per_group"] = {name: _group_seed_to_dict(gs) for (name, gs) in ts.per_group}
    return out


def _column_seed_to_dict(cs: ColumnSeed) -> dict[str, Any]:
    out: dict[str, Any] = {
        "column_seed": cs.column_seed,
        "namespace": cs.namespace,
        "strategy": cs.strategy,
        "provider": cs.provider,
        "backend_type": cs.backend_type,
        "backend_version": cs.backend_version,
        "cardinality_mode": cs.cardinality_mode,
    }
    if cs.provider_config:
        out["provider_config"] = dict(cs.provider_config)
    if cs.coherent_with:
        out["coherent_with"] = list(cs.coherent_with)
    return out


def _group_seed_to_dict(gs: GroupSeed) -> dict[str, Any]:
    return {
        "group_seed": gs.group_seed,
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
    # Wire format: list of [table, [col1, col2, ...]] entries.
    # The old 'table.col1__col2' string format was ambiguous when column names
    # contained '__'; this structured form is unambiguous (M1 fix, session 11).
    return {
        "declared_by": [[t, list(cols)] for (t, cols) in ns.declared_by],
        "seed": ns.seed,
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
    return SeedEnvelope(job_seed=int(data["job_seed"]), per_table=per_table)


def _table_seed_from_dict(data: dict[str, Any]) -> TableSeed:
    per_column_raw = data.get("per_column", {}) or {}
    per_group_raw = data.get("per_group", {}) or {}
    return TableSeed(
        table_seed=int(data["table_seed"]),
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
        column_seed=int(data["column_seed"]),
        namespace=data.get("namespace"),
        strategy=data["strategy"],
        provider=data["provider"],
        backend_type=data["backend_type"],
        backend_version=data["backend_version"],
        cardinality_mode=data["cardinality_mode"],
        provider_config=tuple(sorted(provider_config_raw.items())),
        coherent_with=tuple(coherent_with_raw),
    )


def _group_seed_from_dict(data: dict[str, Any]) -> GroupSeed:
    return GroupSeed(
        group_seed=int(data["group_seed"]),
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
        if isinstance(entry, list) and len(entry) == 2:
            # Canonical format: [table, [col1, col2, ...]].
            # Emitted by plan_to_yaml since the M1 fix (session 11).
            # Column names containing '__' are preserved as-is.
            table, cols_raw = entry
            if isinstance(table, str) and isinstance(cols_raw, list) and cols_raw:
                cols = tuple(str(c) for c in cols_raw)
                declared_by.append((table, cols))
        elif isinstance(entry, str) and "." in entry:
            # Legacy fallback: 'table.col' string (pre-M1, single-column only).
            # Only safe to parse when col part has no '__' -- anything with '__'
            # was either an ambiguous composite encoding or a column name
            # with '__' in it (indistinguishable in the old format). Drop
            # ambiguous entries; check_namespace_ambiguity catches them at
            # next plan-compile on the new format.
            table_part, col_part = entry.split(".", 1)
            if col_part and "__" not in col_part:
                declared_by.append((table_part, (col_part,)))
            # else: ambiguous legacy entry; skip silently
    return NamespaceBinding(
        namespace=name,
        declared_by=tuple(declared_by),
        seed=int(body["seed"]),
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

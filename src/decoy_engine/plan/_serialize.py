"""YAML serialization for Plan.

Determinism contract: same Plan -> byte-identical YAML. PyYAML's
`safe_dump` with `sort_keys=False` preserves the order the dataclass
declared. Tuples serialize as lists; ColumnSeed.provider_config
(a tuple of pairs) serializes as a dict-shaped block.

`plan_to_yaml(plan)` produces the manifest-ready string.
`plan_from_yaml(s)` parses it back; round-trip equality holds.

DPS Scope B (guide section 4.7) adds the `generation` block. It is NOT a
blind round trip: `plan_from_yaml` recomputes both the embedded-snapshot
digests and the `dp_verification` receipt from the pinned bytes rather
than trusting the serialized values, so a hand-edited manifest cannot
smuggle in bytes or a budget receipt the compiler never actually
verified. This is the same "read once, verify from bytes" posture guide
section 4.7 requires of `compile_plan` itself, applied again at load
time because a YAML file is untrusted input the moment it leaves the
process that wrote it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import yaml

from decoy_engine.plan._checks import check_statistical_columns
from decoy_engine.plan._checks_dp import verify_dp_snapshots
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._generation import ReadSnapshot, build_generation_plan
from decoy_engine.plan._types import (
    ColumnSeed,
    DpVerification,
    GenerationPlan,
    GroupSeed,
    NamespaceBinding,
    OrderingNode,
    PinnedSnapshot,
    PinnedStatisticalSpec,
    Plan,
    PlanCompileResult,
    PlanRelationship,
    PlanRelationshipEnd,
    SeedEnvelope,
    TableSeed,
    unfreeze_json,
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
    out = {
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
    # DPS Scope B: omitted entirely for a Plan with no generate_columns
    # (guide section 4.7); a plan_version 1 manifest never had this key,
    # so round-tripping "no generation" as "key absent" keeps both
    # versions' YAML shape identical for that case.
    if plan.generation is not None:
        out["generation"] = _generation_plan_to_dict(plan.generation)
    return out


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
    # DE-11 (2026-07-13): same omit-when-None round-trip pattern; legacy
    # plans (compiled before this field existed) deserialize both as None.
    if cs.pool_size is not None:
        out["pool_size"] = cs.pool_size
    if cs.scale is not None:
        out["scale"] = cs.scale
    # DE-02 (6b): emit vault only when set, so legacy plans round-trip unchanged.
    if cs.vault:
        out["vault"] = True
    # Codex P1 PROVENANCE IS EVIDENCE, NOT PLAN STATE: code-corpus provenance
    # is deliberately NEVER stamped onto the plan manifest. HC-1 slice 1
    # originally called corpus_provenance_for_manifest here, which loaded
    # whatever corpus happened to be on disk at plan_to_yaml time -- making
    # the plan artifact non-deterministic (a swapped/absent corpus silently
    # changed or dropped the block) and it never round-tripped
    # (_column_seed_from_dict ignores unknown keys). The HC-1 spec requires
    # provenance "surfaced in output/evidence", not in the reproducible plan
    # config; it lives only in execution evidence
    # (ExecutionResult.quality_metrics['code_set_corpora'], stamped from the
    # actually-loaded corpus at run time -- see execution/_strategies/_code_set.py).
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


def _pinned_snapshot_to_dict(ps: PinnedSnapshot) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source_path": ps.source_path,
        "sha256": ps.sha256,
        "payload_b64": ps.payload_b64,
    }
    if ps.release_id is not None:
        out["release_id"] = ps.release_id
    return out


def _pinned_statistical_spec_to_dict(spec: PinnedStatisticalSpec) -> dict[str, Any]:
    return {
        "table_name": spec.table_name,
        "column_name": spec.column_name,
        "snapshot_index": spec.snapshot_index,
        "spec": unfreeze_json(spec.spec),
    }


def _dp_verification_to_dict(dv: DpVerification) -> dict[str, Any]:
    return {
        "scope": dv.scope,
        "release_ids": list(dv.release_ids),
        "epsilon_total": dv.epsilon_total,
        "delta_total": dv.delta_total,
    }


def _generation_plan_to_dict(gp: GenerationPlan) -> dict[str, Any]:
    out: dict[str, Any] = {
        "config_json": gp.config_json,
        "snapshots": [_pinned_snapshot_to_dict(s) for s in gp.snapshots],
        "statistical_specs": [_pinned_statistical_spec_to_dict(s) for s in gp.statistical_specs],
    }
    if gp.dp_verification is not None:
        out["dp_verification"] = _dp_verification_to_dict(gp.dp_verification)
    return out


# ---------------------------------------------------------------------
# From-dict
# ---------------------------------------------------------------------


def _plan_from_dict(data: dict[str, Any]) -> Plan:
    generation_raw = data.get("generation")
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
        generation=(
            _generation_plan_from_dict(generation_raw) if generation_raw is not None else None
        ),
    )


def _pinned_snapshot_from_dict(data: dict[str, Any]) -> tuple[PinnedSnapshot, ReadSnapshot]:
    """Decode one embedded snapshot and revalidate its digest against the
    embedded bytes themselves (guide section 4.7: "revalidate embedded
    digests... during deserialization"). A YAML file is untrusted input;
    the stored `sha256` field is a claim, not evidence, so this recomputes
    it from `payload_b64` and rejects a mismatch rather than trusting the
    file's own say-so. Also returns the `ReadSnapshot` view so
    `verify_dp_snapshots` can be re-run against the SAME verified bytes,
    exactly like a fresh compile would.

    Raises:
        PlanCompileError: ``code='dp_pinned_snapshot_digest_mismatch'``
            when the recomputed digest disagrees with the stored one.
    """
    source_path = data["source_path"]
    stored_sha256 = data["sha256"]
    raw = base64.b64decode(data["payload_b64"])
    recomputed_sha256 = hashlib.sha256(raw).hexdigest()
    if recomputed_sha256 != stored_sha256:
        raise PlanCompileError(
            code="dp_pinned_snapshot_digest_mismatch",
            path=f"<generation.snapshots source_path={source_path!r}>",
            message=(
                f"embedded snapshot {source_path!r} recomputes to sha256="
                f"{recomputed_sha256!r}, but the manifest declares sha256="
                f"{stored_sha256!r}. A Plan's pinned bytes are verified evidence, "
                "not a trusted claim; a mismatch means the manifest was edited or "
                "corrupted after compile."
            ),
        )
    pinned_snapshot = PinnedSnapshot(
        source_path=source_path,
        sha256=stored_sha256,
        payload_b64=data["payload_b64"],
        release_id=data.get("release_id"),
    )
    parsed = json.loads(raw)
    read_snapshot = ReadSnapshot(
        path=source_path,
        sha256=stored_sha256,
        parsed=parsed if isinstance(parsed, dict) else {},
        raw=raw,
    )
    return pinned_snapshot, read_snapshot


def _require_statistical_snapshots_pinned(
    config: dict[str, Any], pinned: dict[str, ReadSnapshot]
) -> None:
    """HIGH H-A: refuse a deserialized `generation` block whose config
    references a `snapshot_file` the pinned set does not cover, BEFORE
    `check_statistical_columns` ever runs.

    `check_statistical_columns` is shared with `compile_plan`, where a
    path absent from `pinned` genuinely means "the read pass couldn't
    open it" and a direct-read fallback re-derives the right unreadable
    verdict (guide section 4.7). At LOAD time that fallback is wrong: an
    absent path means a serialized Plan's `generation.snapshots` no
    longer covers a `snapshot_file` its own `config_json` references --
    a malformed manifest, not a missing file -- so falling back would
    make deserialization a filesystem read plus an existence oracle over
    a path named in this untrusted document, contradicting "read once and
    pinned, never reopened." This check runs first and refuses instead,
    leaving `check_statistical_columns`'s own fallback path unreachable
    here."""
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("generate_columns", []) or []:
            if not isinstance(col_entry, dict) or col_entry.get("type") != "statistical":
                continue
            snapshot_file = col_entry.get("snapshot_file")
            col_name = col_entry.get("name", "?")
            if snapshot_file and str(snapshot_file) not in pinned:
                raise PlanCompileError(
                    code="statistical_snapshot_not_pinned",
                    path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
                    message=(
                        f"statistical column {col_name!r} references snapshot_file "
                        f"{snapshot_file!r}, not among this Plan's pinned snapshots -- "
                        "a malformed manifest, not a fallback to a fresh filesystem read."
                    ),
                )


def _generation_plan_from_dict(data: dict[str, Any]) -> GenerationPlan:
    """Decode the `generation` block, revalidating everything the guide
    requires rather than trusting the serialized values verbatim.

    Every embedded snapshot's digest is recomputed (raises on mismatch,
    `_pinned_snapshot_from_dict`). H5 (guide section 4.7): the previous
    version of this function stopped there for `statistical_specs` --
    `_pinned_statistical_spec_from_dict` only refroze the serialized
    `spec` dict, so a manifest whose `statistical_specs[i].spec`
    disagreed with `snapshots[i].payload_b64` passed every check and
    generation would sample from the untrusted `spec`, not the verified
    bytes. This function no longer trusts `data["statistical_specs"]` at
    all: it RE-RUNS `check_statistical_columns` -- the same compile-time
    pass that built `column_specs` in the first place -- against the
    revalidated pinned bytes and the embedded config, then rebuilds the
    WHOLE `GenerationPlan` from that recomputation via `build_generation_
    plan`, exactly as a fresh `compile_plan` would. `dp_verification` is
    recomputed the same way it always was, from those same revalidated
    bytes (guide section 4.7: "revalidate embedded digests and DP
    receipts during deserialization").

    HIGH H-A: `_require_statistical_snapshots_pinned` runs first, so a
    config-referenced snapshot absent from the pinned set is refused
    before `check_statistical_columns`'s own compile-time fallback (a
    direct filesystem read) can ever run against this untrusted manifest."""
    decoded = [_pinned_snapshot_from_dict(s) for s in data.get("snapshots", []) or []]
    pinned_by_path: dict[str, ReadSnapshot] = {read.path: read for _pinned, read in decoded}

    config = json.loads(data["config_json"])
    _require_statistical_snapshots_pinned(config, pinned_by_path)
    dp_verified_columns, dp_verification = verify_dp_snapshots(config, pinned_by_path)
    column_specs = check_statistical_columns(config, pinned_by_path, dp_verified_columns)
    rebuilt = build_generation_plan(
        config,
        pinned_by_path,
        column_specs=column_specs,
        dp_verification=dp_verification,
    )
    if rebuilt is None:
        # A manifest that serialized a `generation` block in the first
        # place always has at least one generate table; this guards the
        # (unreachable through normal compile_plan output) degenerate case
        # defensively rather than raising an unrelated AttributeError.
        return GenerationPlan(
            config_json=data["config_json"],
            snapshots=(),
            statistical_specs=(),
            dp_verification=dp_verification,
        )
    return rebuilt


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
        # DE-11 (2026-07-13): same omit-when-None pattern.
        pool_size=data.get("pool_size"),
        scale=data.get("scale"),
        # DE-02 (6b): legacy plans omit vault -> False.
        vault=bool(data.get("vault", False)),
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

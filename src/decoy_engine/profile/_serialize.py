"""JSON serialization for Profile and helpers for canonical hashing.

Profile round-trips through JSON: dump produces a string, load rebuilds a
Profile whose __eq__ holds against the original. datetime is serialized
as ISO-8601 per datetime.isoformat() / datetime.fromisoformat(); enum
values serialize as their string values.

_data_shape_bytes produces the canonical byte stream that feeds
profile_hash. It includes only the data-shape fields (schema_version,
tables, relationships) and excludes sidecar metadata. Sort_keys=True +
ensure_ascii=True + separators=(",", ":") give a stable byte stream for
SHA-256 input; this is not full JCS (RFC 8785) but is sufficient for
hashing immutable structures with simple value types.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from decoy_engine.profile._types import (
    ColumnProfile,
    PIIClass,
    Profile,
    Relationship,
    TableProfile,
)


def profile_to_json(profile: Profile) -> str:
    """Serialize a Profile to a JSON string."""
    return json.dumps(_profile_to_dict(profile), ensure_ascii=True, sort_keys=True)


def profile_from_json(s: str) -> Profile:
    """Deserialize a JSON string back into a Profile.

    Raises ValueError if the JSON shape does not match the expected
    Profile schema. The error message names the offending field where
    possible.
    """
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"profile_from_json: invalid JSON: {exc}") from exc
    return _profile_from_dict(data)


def _data_shape_bytes(profile: Profile) -> bytes:
    """Canonical byte stream over the data-shape fields only.

    Used as the SHA-256 input for profile_hash. Excludes profiled_at,
    decoy_engine_version, and profile_seed by construction.
    """
    payload = {
        "schema_version": profile.schema_version,
        "tables": [_table_to_dict(t) for t in profile.tables],
        "relationships": [_relationship_to_dict(r) for r in profile.relationships],
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _profile_to_dict(profile: Profile) -> dict[str, Any]:
    return {
        "schema_version": profile.schema_version,
        "tables": [_table_to_dict(t) for t in profile.tables],
        "relationships": [_relationship_to_dict(r) for r in profile.relationships],
        "profiled_at": profile.profiled_at.isoformat(),
        "decoy_engine_version": profile.decoy_engine_version,
        "profile_seed": profile.profile_seed,
    }


def _table_to_dict(table: TableProfile) -> dict[str, Any]:
    return {
        "name": table.name,
        "row_count": table.row_count,
        "columns": [_column_to_dict(c) for c in table.columns],
    }


def _column_to_dict(column: ColumnProfile) -> dict[str, Any]:
    # Hand-listed (L3 from slice-1 review) rather than dataclasses.asdict +
    # spot fixes. New ColumnProfile fields should not silently change the
    # wire shape; adding a field here is the deliberate part of a schema
    # change.
    return {
        "name": column.name,
        "dtype": column.dtype,
        "row_count": column.row_count,
        "null_count": column.null_count,
        "distinct_count": column.distinct_count,
        "sampled": column.sampled,
        "is_candidate_key_sampled": column.is_candidate_key_sampled,
        "declared_pk": column.declared_pk,
        "is_fk": column.is_fk,
        "fk_target": list(column.fk_target) if column.fk_target is not None else None,
        "pii_class": column.pii_class.value if column.pii_class is not None else None,
    }


def _relationship_to_dict(rel: Relationship) -> dict[str, Any]:
    return {
        "parent_table": rel.parent_table,
        "parent_columns": list(rel.parent_columns),
        "child_table": rel.child_table,
        "child_columns": list(rel.child_columns),
        "namespace": rel.namespace,
    }


def _profile_from_dict(data: dict[str, Any]) -> Profile:
    return Profile(
        schema_version=data["schema_version"],
        tables=tuple(_table_from_dict(t) for t in data["tables"]),
        relationships=tuple(_relationship_from_dict(r) for r in data["relationships"]),
        profiled_at=datetime.fromisoformat(data["profiled_at"]),
        decoy_engine_version=data["decoy_engine_version"],
        profile_seed=data.get("profile_seed"),
    )


def _table_from_dict(data: dict[str, Any]) -> TableProfile:
    return TableProfile(
        name=data["name"],
        row_count=data["row_count"],
        columns=tuple(_column_from_dict(c) for c in data["columns"]),
    )


def _column_from_dict(data: dict[str, Any]) -> ColumnProfile:
    fk_target_raw = data.get("fk_target")
    fk_target = (fk_target_raw[0], fk_target_raw[1]) if fk_target_raw is not None else None
    pii_class_raw = data.get("pii_class")
    pii_class = PIIClass(pii_class_raw) if pii_class_raw is not None else None
    return ColumnProfile(
        name=data["name"],
        dtype=data["dtype"],
        row_count=data["row_count"],
        null_count=data["null_count"],
        distinct_count=data.get("distinct_count"),
        sampled=data["sampled"],
        is_candidate_key_sampled=data["is_candidate_key_sampled"],
        declared_pk=data["declared_pk"],
        is_fk=data["is_fk"],
        fk_target=fk_target,
        pii_class=pii_class,
    )


def _relationship_from_dict(data: dict[str, Any]) -> Relationship:
    return Relationship(
        parent_table=data["parent_table"],
        parent_columns=tuple(data["parent_columns"]),
        child_table=data["child_table"],
        child_columns=tuple(data["child_columns"]),
        namespace=data.get("namespace"),
    )

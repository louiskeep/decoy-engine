"""Pattern: Git content addressing + RFC 8785 canonical JSON (Git objects, GPL-2.0 docs; RFC 8785). See: https://git-scm.com/book/en/v2/Git-Internals-Git-Objects.

Strict canonical encoding for the `decoy-publication/v1` descriptor and `result_id`.

Pattern: Git content addressing (names derive from content, trees reference objects)
and RFC 8785-style canonical JSON (sorted keys, fixed separators, no NaN/Infinity).
We hash the descriptor's own bytes externally (`descriptor_digest`) so there is no
self-referential hash. Parsing is strict: duplicate keys, non-finite numbers, unknown
schema versions, unsafe paths and forged digests are all refused.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from decoy_engine.errors import DecoyError
from decoy_engine.publication._types import ArtifactRef, CheckResult

SCHEMA = "decoy-publication/v1"
EVIDENCE_SCHEMA = "decoy-publication-evidence/v1"
DESCRIPTOR_NAME = "_decoy_publication.json"
EVIDENCE_NAME = "_decoy_evidence_core.json"
RESERVED_NAMES = frozenset({DESCRIPTOR_NAME, EVIDENCE_NAME})
MAX_DESCRIPTOR_BYTES = 8 * 1024 * 1024

_RESULT_DOMAIN = b"decoy-publication-result/v1\x00"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ROLES = frozenset({"table", "sidecar", "quarantine"})


class CodecError(DecoyError, ValueError):
    """The bytes are not a valid descriptor/evidence document."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CodecError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise CodecError(f"non-finite number {name} is not allowed")


def parse_strict(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_DESCRIPTOR_BYTES:
        raise CodecError("document exceeds the size bound")
    try:
        doc = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_no_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodecError(f"not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise CodecError("document must be a JSON object")
    return doc


def safe_relative_path(path: str) -> str:
    """One contained, non-reserved path component (publication units are flat)."""
    if (
        not path
        or path in {".", ".."}
        or "/" in path
        or "\\" in path
        or "\x00" in path
        or path.startswith(".")
        or path in RESERVED_NAMES
    ):
        raise CodecError(f"unsafe artifact path {path!r}")
    return path


def _entry_dict(e: ArtifactRef) -> dict[str, Any]:
    return asdict(e)


def sorted_unique(entries: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    ordered = tuple(sorted(entries, key=lambda e: e.path))
    paths = [e.path for e in ordered]
    if len(set(paths)) != len(paths):
        raise CodecError("duplicate artifact path in inventory")
    return ordered


def compute_result_id(entries: Iterable[ArtifactRef]) -> str:
    """Physical identity: roles, paths, sizes, digests, counts, schemas. Nothing else."""
    inv = [_entry_dict(e) for e in sorted_unique(entries)]
    return sha256_hex(_RESULT_DOMAIN + canonical_bytes(inv))


def _check_entry(raw: Any) -> ArtifactRef:
    if not isinstance(raw, dict):
        raise CodecError("artifact entry must be an object")
    allowed = {"role", "path", "size", "sha256", "rows", "schema_digest"}
    if set(raw) - allowed or not {"role", "path", "size", "sha256"} <= set(raw):
        raise CodecError("artifact entry has unexpected or missing fields")
    size, rows = raw["size"], raw.get("rows")
    if raw["role"] not in _ROLES:
        raise CodecError(f"unknown artifact role {raw['role']!r}")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise CodecError("artifact size must be a non-negative integer")
    if rows is not None and (not isinstance(rows, int) or isinstance(rows, bool) or rows < 0):
        raise CodecError("artifact rows must be a non-negative integer")
    if not isinstance(raw["sha256"], str) or not _SHA.match(raw["sha256"]):
        raise CodecError("artifact sha256 must be 64 lowercase hex characters")
    sd = raw.get("schema_digest")
    if sd is not None and (not isinstance(sd, str) or not _SHA.match(sd)):
        raise CodecError("schema_digest must be 64 lowercase hex characters")
    return ArtifactRef(raw["role"], safe_relative_path(raw["path"]), size, raw["sha256"], rows, sd)


def evidence_core_bytes(
    *,
    result_id: str,
    policy_fingerprint: str | None,
    checks: Iterable[CheckResult],
    inventory_digest: str,
) -> bytes:
    """Pre-publication evidence core: binds result_id and validation decisions, and
    deliberately excludes the eventual receipt (F33 acyclic ordering)."""
    return canonical_bytes(
        {
            "schema": EVIDENCE_SCHEMA,
            "result_id": result_id,
            "inventory_digest": inventory_digest,
            "validation_policy_fingerprint": policy_fingerprint,
            "checks": [
                {"check_id": c.check_id, "disposition": c.disposition, "required": c.required}
                for c in sorted(checks, key=lambda c: c.check_id)
            ],
        }
    )


def descriptor_bytes(
    *,
    unit: str,
    destination: str,
    job_id: str | None,
    operation_id: str,
    attempt_id: str,
    entries: Iterable[ArtifactRef],
    policy_fingerprint: str | None,
    evidence: bytes,
    predecessor_digest: str | None = None,
    backup_ref: str | None = None,
) -> bytes:
    ordered = sorted_unique(entries)
    return canonical_bytes(
        {
            "schema": SCHEMA,
            "unit": unit,
            "destination": destination,
            "job_id": job_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "result_id": compute_result_id(ordered),
            "artifacts": [_entry_dict(e) for e in ordered],
            "validation_policy_fingerprint": policy_fingerprint,
            "evidence": {
                "path": EVIDENCE_NAME,
                "size": len(evidence),
                "sha256": sha256_hex(evidence),
            },
            "lineage": {"predecessor_digest": predecessor_digest, "backup_ref": backup_ref},
        }
    )


def parse_descriptor(data: bytes) -> dict[str, Any]:
    """Validate shape and recompute `result_id`; a forged id or inventory is refused."""
    doc = parse_strict(data)
    if doc.get("schema") != SCHEMA:
        raise CodecError(f"unknown descriptor schema {doc.get('schema')!r}")
    for key in ("unit", "destination", "operation_id", "attempt_id", "result_id"):
        if not isinstance(doc.get(key), str):
            raise CodecError(f"descriptor field {key!r} must be a string")
    arts = doc.get("artifacts")
    if not isinstance(arts, list):
        raise CodecError("descriptor artifacts must be an array")
    entries = sorted_unique(_check_entry(a) for a in arts)
    if [_entry_dict(e) for e in entries] != arts:
        raise CodecError("artifact inventory is not in canonical order")
    if compute_result_id(entries) != doc["result_id"]:
        raise CodecError("result_id does not match the artifact inventory")
    ev = doc.get("evidence")
    if (
        not isinstance(ev, dict)
        or ev.get("path") != EVIDENCE_NAME
        or not isinstance(ev.get("sha256"), str)
        or not _SHA.match(ev["sha256"])
        or not isinstance(ev.get("size"), int)
    ):
        raise CodecError("descriptor evidence reference is malformed")
    if canonical_bytes(doc) != data:
        raise CodecError("descriptor is not in canonical form")
    doc["_entries"] = entries
    return doc

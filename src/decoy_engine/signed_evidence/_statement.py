"""EvidenceStatementV1: the strictly typed, canonical statement that gets signed.

Pattern: RFC 8785 JCS canonical bytes are the signed form and the received bytes must equal them
(plan 1.4 rule 3); the in-toto Statement v1 shape (subject digests plus typed predicate) informs
the artifact inventory, without its envelope. The schema is closed: unknown members fail, arrays
that carry set semantics are sorted, and every value has exactly one spelling (integers within
the JCS safe range, decimal strings only beyond it), so two producers cannot sign different bytes
for the same facts.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from . import _strict_json as sj
from ._canonical import canonical_bytes
from ._errors import SignedEvidenceError
from ._time import parse_utc

SCHEMA_ID = "decoy.evidence.statement/v1"
MAX_SAFE_INT = sj.MAX_SAFE_INT
MAX_ARTIFACTS = 10_000
MAX_CHECKS = 50_000
MAX_MAP_ENTRIES = 256
MAX_METRICS = 64
MAX_WARNINGS = 256
MAX_PATH_CHARS = 1024
MAX_COMPONENT_CHARS = 255

STATUSES = ("PASS", "FAIL", "INCONCLUSIVE", "NOT_APPLICABLE")
SEVERITIES = ("info", "warning", "error")
DISPOSITIONS = ("INCLUDED", "WITHHELD", "UNAVAILABLE")
LEGACY_SCHEMA_VERSIONS = frozenset({"r3.1", "cli-local-1"})

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FP_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")
_ALGO = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ENGINE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_METHOD = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_BIG = re.compile(r"^[1-9][0-9]{0,29}$")
_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$")


def _bad(code: str, status: str = "MALFORMED") -> SignedEvidenceError:
    return SignedEvidenceError(code, status=status)


def _schema() -> SignedEvidenceError:
    return _bad("STATEMENT_SCHEMA_INVALID")


# ------------------------------------------------------------------ typed carriers


@dataclass(frozen=True, slots=True)
class Fingerprint:
    algorithm: str
    version: str
    value: str


@dataclass(frozen=True, slots=True)
class NamedFingerprint:
    name: str
    algorithm: str
    version: str
    value: str


@dataclass(frozen=True, slots=True)
class SourceRef:
    snapshot_id: str
    fingerprint: Fingerprint


@dataclass(frozen=True, slots=True)
class Publication:
    state: str
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    id: str
    path: str
    media_type: str
    bytes: int | None
    sha256: str | None
    disposition: str


@dataclass(frozen=True, slots=True)
class PolicyRef:
    id: str
    version: str
    digest: str


@dataclass(frozen=True, slots=True)
class Coverage:
    rows_scanned: int
    rows_total: int
    columns: int


@dataclass(frozen=True, slots=True)
class Limits:
    sampled: bool
    sample_rows: int | None


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    check_id: str
    subject_kind: str
    subject_id: str
    method: str
    method_version: str
    status: str
    severity: str
    reason_code: str | None
    coverage: Coverage
    limits: Limits
    metrics: Mapping[str, int | bool | str]


@dataclass(frozen=True, slots=True)
class EvidenceStatementV1:
    statement_id: str
    bundle_id: str
    job_id: str
    attempt_id: str
    tenant: str
    signer: str
    engine_name: str
    engine_version: str
    created_at: str
    source: SourceRef
    fingerprints: tuple[NamedFingerprint, ...]
    counts: Mapping[str, int]
    dispositions: Mapping[str, str]
    warnings: tuple[str, ...]
    publication: Publication
    artifacts: tuple[ArtifactEntry, ...]
    policy: PolicyRef
    checks: tuple[CheckOutcome, ...]


# ---------------------------------------------------------------- field validators


def _obj(value: Any, members: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _schema()
    extra = set(value) - set(members)
    if extra:
        raise _bad("STATEMENT_UNKNOWN_FIELD")
    if set(members) - set(value):
        raise _bad("STATEMENT_MISSING_FIELD")
    return value


def _match(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _schema()
    return value


def _count(value: Any) -> int:
    """One spelling per value: an int inside the safe range, a decimal string only beyond it."""
    if isinstance(value, bool):
        raise _schema()
    if isinstance(value, int):
        if not 0 <= value <= MAX_SAFE_INT:
            raise _schema()
        return value
    if isinstance(value, str) and _BIG.fullmatch(value):
        big = int(value)
        if big <= MAX_SAFE_INT:
            raise _schema()
        return big
    raise _schema()


def _opt_count(value: Any) -> int | None:
    return None if value is None else _count(value)


def _metric(value: Any) -> int | bool | str:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise _schema()
        return value
    if isinstance(value, str) and len(value) <= 64 and _DECIMAL.fullmatch(value) and value != "-0":
        if "." in value or abs(int(value)) > MAX_SAFE_INT:
            return value
    raise _schema()


def _created_at(value: Any) -> str:
    if not isinstance(value, str) or parse_utc(value) is None:
        raise _schema()
    return value


def check_relative_path(value: Any) -> str:
    """Validate an artifact path exactly as the statement schema does; raises on any unsafe form."""
    return _safe_path(value)


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_PATH_CHARS:
        raise _bad("STATEMENT_PATH_INVALID")
    if value.startswith("/") or "\\" in value:
        raise _bad("STATEMENT_PATH_INVALID")
    for comp in value.split("/"):
        if (
            not comp
            or comp in (".", "..")
            or len(comp) > MAX_COMPONENT_CHARS
            or comp != comp.strip(" ")
            or ":" in comp
            or any(unicodedata.category(ch).startswith("C") for ch in comp)
        ):
            raise _bad("STATEMENT_PATH_INVALID")
    return value


# ------------------------------------------------------------------ section parsers


def _fingerprint(node: Any) -> Fingerprint:
    o = _obj(node, ("algorithm", "version", "value"))
    return Fingerprint(
        _match(o["algorithm"], _ALGO), _match(o["version"], _ID), _match(o["value"], _FP_VALUE)
    )


def _named_fingerprints(node: Any) -> tuple[NamedFingerprint, ...]:
    if not isinstance(node, list) or len(node) > MAX_MAP_ENTRIES:
        raise _schema()
    out = []
    for item in node:
        o = _obj(item, ("name", "algorithm", "version", "value"))
        out.append(
            NamedFingerprint(
                _match(o["name"], _NAME),
                _match(o["algorithm"], _ALGO),
                _match(o["version"], _ID),
                _match(o["value"], _FP_VALUE),
            )
        )
    names = [f.name for f in out]
    if names != sorted(set(names)):
        raise _bad("STATEMENT_ARRAY_ORDER")
    return tuple(out)


def _map(node: Any, convert: Any) -> Mapping[str, Any]:
    if not isinstance(node, dict) or len(node) > MAX_MAP_ENTRIES:
        raise _schema()
    return MappingProxyType({_match(k, _NAME): convert(v) for k, v in node.items()})


def _warnings(node: Any) -> tuple[str, ...]:
    if not isinstance(node, list) or len(node) > MAX_WARNINGS:
        raise _schema()
    codes = [_match(c, _CODE) for c in node]
    if codes != sorted(set(codes)):
        raise _bad("STATEMENT_ARRAY_ORDER")
    return tuple(codes)


def _publication(node: Any) -> Publication:
    o = _obj(node, ("state", "reason_code"))
    state = o["state"]
    if state not in ("PUBLISHED", "REJECTED"):
        raise _schema()
    reason = o["reason_code"]
    if reason is not None:
        _match(reason, _CODE)
    if (state == "PUBLISHED") != (reason is None):
        raise _bad("STATEMENT_PUBLICATION_INCOHERENT")
    return Publication(state, reason)


def _artifact(node: Any) -> ArtifactEntry:
    o = _obj(node, ("id", "path", "media_type", "bytes", "sha256", "disposition"))
    disposition = o["disposition"]
    if disposition not in DISPOSITIONS:
        raise _schema()
    aid = _match(o["id"], _ID)
    path = _safe_path(o["path"])
    media = _match(o["media_type"], _MEDIA)
    if disposition == "UNAVAILABLE":
        if o["bytes"] is not None or o["sha256"] is not None:
            raise _schema()
        return ArtifactEntry(aid, path, media, None, None, disposition)
    if o["bytes"] is None or o["sha256"] is None:
        raise _schema()
    return ArtifactEntry(
        aid, path, media, _count(o["bytes"]), _match(o["sha256"], _DIGEST), disposition
    )


def _artifacts(node: Any) -> tuple[ArtifactEntry, ...]:
    if not isinstance(node, list):
        raise _schema()
    if len(node) > MAX_ARTIFACTS:
        raise _bad("STATEMENT_TOO_MANY_ARTIFACTS")
    out = [_artifact(a) for a in node]
    ids = [a.id for a in out]
    if len(set(ids)) != len(ids):
        raise _bad("STATEMENT_ARTIFACT_ID_DUPLICATE")
    paths = [a.path for a in out]
    if len(set(paths)) != len(paths):
        raise _bad("STATEMENT_PATH_DUPLICATE")
    if ids != sorted(ids):
        raise _bad("STATEMENT_ARRAY_ORDER")
    return tuple(out)


def _coverage(node: Any) -> Coverage:
    o = _obj(node, ("rows_scanned", "rows_total", "columns"))
    cov = Coverage(_count(o["rows_scanned"]), _count(o["rows_total"]), _count(o["columns"]))
    if cov.rows_scanned > cov.rows_total:
        raise _bad("STATEMENT_COVERAGE_INVALID")
    return cov


def _check(node: Any) -> CheckOutcome:
    o = _obj(
        node,
        (
            "check_id",
            "subject",
            "method",
            "method_version",
            "status",
            "severity",
            "reason_code",
            "coverage",
            "limits",
            "metrics",
        ),
    )
    subject = _obj(o["subject"], ("kind", "id"))
    if subject["kind"] not in ("artifact", "snapshot"):
        raise _schema()
    if o["status"] not in STATUSES or o["severity"] not in SEVERITIES:
        raise _schema()
    reason = o["reason_code"]
    if reason is not None:
        _match(reason, _CODE)
    limits = _obj(o["limits"], ("sampled", "sample_rows"))
    if not isinstance(limits["sampled"], bool):
        raise _schema()
    metrics = o["metrics"]
    if not isinstance(metrics, dict) or len(metrics) > MAX_METRICS:
        raise _schema()
    return CheckOutcome(
        check_id=_match(o["check_id"], _NAME),
        subject_kind=subject["kind"],
        subject_id=_match(subject["id"], _ID),
        method=_match(o["method"], _METHOD),
        method_version=_match(o["method_version"], _ID),
        status=o["status"],
        severity=o["severity"],
        reason_code=reason,
        coverage=_coverage(o["coverage"]),
        limits=Limits(limits["sampled"], _opt_count(limits["sample_rows"])),
        metrics=MappingProxyType({_match(k, _NAME): _metric(v) for k, v in metrics.items()}),
    )


def _checks(node: Any) -> tuple[CheckOutcome, ...]:
    if not isinstance(node, list):
        raise _schema()
    if len(node) > MAX_CHECKS:
        raise _bad("STATEMENT_TOO_MANY_CHECKS")
    out = [_check(c) for c in node]
    keys = [(c.check_id, c.subject_kind, c.subject_id) for c in out]
    if keys != sorted(keys):
        raise _bad("STATEMENT_ARRAY_ORDER")
    return tuple(out)


_TOP = (
    "schema",
    "bundle_id",
    "job_id",
    "attempt_id",
    "issuer",
    "engine",
    "created_at",
    "source",
    "fingerprints",
    "counts",
    "dispositions",
    "warnings",
    "publication",
    "artifacts",
    "policy",
    "checks",
)


def _validate(doc: Any, statement_id: str) -> EvidenceStatementV1:
    if not isinstance(doc, dict):
        raise _schema()
    schema = doc.get("schema")
    if schema is None:
        if doc.get("schema_version") in LEGACY_SCHEMA_VERSIONS:
            raise _bad("LEGACY_INTEGRITY_ONLY", "UNSUPPORTED")
        raise _bad("STATEMENT_MISSING_FIELD")
    if schema != SCHEMA_ID:
        raise _bad("STATEMENT_SCHEMA_UNSUPPORTED", "UNSUPPORTED")
    o = _obj(doc, _TOP)
    issuer = _obj(o["issuer"], ("tenant", "signer"))
    engine = _obj(o["engine"], ("name", "version"))
    source = _obj(o["source"], ("snapshot_id", "fingerprint"))
    policy = _obj(o["policy"], ("id", "version", "digest"))
    artifacts = _artifacts(o["artifacts"])
    publication = _publication(o["publication"])
    included = [a for a in artifacts if a.disposition == "INCLUDED"]
    # PUBLISHED carries every artifact; REJECTED carries none (it may carry zero artifacts).
    if publication.state == "PUBLISHED" and len(included) != len(artifacts):
        raise _bad("STATEMENT_PUBLICATION_INCOHERENT")
    if publication.state == "REJECTED" and included:
        raise _bad("STATEMENT_PUBLICATION_INCOHERENT")
    return EvidenceStatementV1(
        statement_id=statement_id,
        bundle_id=_match(o["bundle_id"], _ID),
        job_id=_match(o["job_id"], _ID),
        attempt_id=_match(o["attempt_id"], _ID),
        tenant=_match(issuer["tenant"], _ID),
        signer=_match(issuer["signer"], _ID),
        engine_name=_match(engine["name"], _ENGINE_NAME),
        engine_version=_match(engine["version"], _ID),
        created_at=_created_at(o["created_at"]),
        source=SourceRef(_match(source["snapshot_id"], _ID), _fingerprint(source["fingerprint"])),
        fingerprints=_named_fingerprints(o["fingerprints"]),
        counts=_map(o["counts"], _count),
        dispositions=_map(o["dispositions"], lambda v: _match(v, _CODE)),
        warnings=_warnings(o["warnings"]),
        publication=publication,
        artifacts=artifacts,
        policy=PolicyRef(
            _match(policy["id"], _ID),
            _match(policy["version"], _ID),
            _match(policy["digest"], _DIGEST),
        ),
        checks=_checks(o["checks"]),
    )


def parse_statement_ex(raw: bytes) -> tuple[EvidenceStatementV1, bool]:
    """Strict parse + closed-schema validation; also reports whether the bytes are canonical.

    Canonicality is a separate fact from validity so a verifier can report an authentic but
    non-canonical statement as exactly that.
    """
    try:
        doc = sj.parse_strict(raw)
    except SignedEvidenceError as exc:
        raise SignedEvidenceError(f"STATEMENT_{exc.code}", status="MALFORMED") from None
    parsed = _validate(doc, "sha256:" + hashlib.sha256(raw).hexdigest())
    return parsed, canonical_bytes(doc) == bytes(raw)


def parse_statement(raw: bytes) -> EvidenceStatementV1:
    parsed, canonical = parse_statement_ex(raw)
    if not canonical:
        raise _bad("STATEMENT_NOT_CANONICAL", "NON_CANONICAL")
    return parsed


def build_statement_bytes(draft: Mapping[str, Any]) -> bytes:
    """Canonical bytes for a draft after the same validation a verifier will apply."""
    raw = canonical_bytes(draft)
    parse_statement(raw)
    return raw

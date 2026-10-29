"""RequiredCatalog: the independently trusted minimum set of checks a release must carry.

Pattern: policy-as-pinned-input (in-toto layout / TUF root of trust): the verifier is handed the
required set by its operator and compares the producer's signed policy digest against it, so a
producer cannot satisfy strict verification by signing a smaller catalog. A catalog with no
required check is refused outright: a vacuous requirement list would pass anything.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from . import _strict_json as sj
from ._canonical import canonical_bytes
from ._errors import SignedEvidenceError

SCHEMA_ID = "decoy.required-catalog/v1"
MAX_REQUIREMENTS = 256
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_REQ_KEYS = (
    "check_id",
    "scope",
    "media_types",
    "min_rows_scanned",
    "require_full_scan",
    "allow_empty_subject",
    "allow_warning",
    "optional",
    "not_applicable_reason_codes",
)


@dataclass(frozen=True, slots=True)
class RequiredCheck:
    check_id: str
    scope: str
    media_types: frozenset[str] | None
    min_rows_scanned: int
    require_full_scan: bool
    allow_empty_subject: bool
    allow_warning: bool
    optional: bool
    not_applicable_reason_codes: frozenset[str]


@dataclass(frozen=True, slots=True)
class RequiredCatalog:
    id: str
    version: str
    empty_output_allowed: bool
    requirements: tuple[RequiredCheck, ...]
    digest: str

    @property
    def has_required_snapshot_check(self) -> bool:
        return any(r.scope == "snapshot" and not r.optional for r in self.requirements)

    @classmethod
    def from_bytes(cls, raw: bytes) -> RequiredCatalog:
        try:
            doc = sj.parse_strict(raw, max_bytes=256 * 1024)
        except SignedEvidenceError as exc:
            raise SignedEvidenceError(f"CATALOG_{exc.code}") from None
        return _build(doc)


def _bad(code: str) -> SignedEvidenceError:
    return SignedEvidenceError(code)


def _closed(node: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(node, dict) or set(node) != set(keys):
        raise _bad("CATALOG_SCHEMA_INVALID")
    return node


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise _bad("CATALOG_SCHEMA_INVALID")
    return v


def _requirement(node: Any) -> RequiredCheck:
    o = _closed(node, _REQ_KEYS)
    if not isinstance(o["check_id"], str) or not _NAME.fullmatch(o["check_id"]):
        raise _bad("CATALOG_SCHEMA_INVALID")
    if o["scope"] not in ("each_artifact", "snapshot"):
        raise _bad("CATALOG_SCHEMA_INVALID")
    media = o["media_types"]
    if media is not None:
        if (
            o["scope"] != "each_artifact"
            or not isinstance(media, list)
            or not media
            or not all(isinstance(m, str) and _MEDIA.fullmatch(m) for m in media)
        ):
            raise _bad("CATALOG_SCHEMA_INVALID")
        media = frozenset(media)
    rows = o["min_rows_scanned"]
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise _bad("CATALOG_SCHEMA_INVALID")
    na = o["not_applicable_reason_codes"]
    if not isinstance(na, list) or not all(isinstance(c, str) and _CODE.fullmatch(c) for c in na):
        raise _bad("CATALOG_SCHEMA_INVALID")
    return RequiredCheck(
        check_id=o["check_id"],
        scope=o["scope"],
        media_types=media,
        min_rows_scanned=rows,
        require_full_scan=_bool(o["require_full_scan"]),
        allow_empty_subject=_bool(o["allow_empty_subject"]),
        allow_warning=_bool(o["allow_warning"]),
        optional=_bool(o["optional"]),
        not_applicable_reason_codes=frozenset(na),
    )


def _build(doc: Any) -> RequiredCatalog:
    o = _closed(doc, ("schema", "id", "version", "empty_output_allowed", "requirements"))
    if o["schema"] != SCHEMA_ID:
        raise _bad("CATALOG_SCHEMA_UNSUPPORTED")
    for key in ("id", "version"):
        if not isinstance(o[key], str) or not _ID.fullmatch(o[key]):
            raise _bad("CATALOG_SCHEMA_INVALID")
    reqs = o["requirements"]
    if not isinstance(reqs, list) or len(reqs) > MAX_REQUIREMENTS:
        raise _bad("CATALOG_SCHEMA_INVALID")
    parsed = tuple(_requirement(r) for r in reqs)
    ids = [r.check_id for r in parsed]
    if len(set(ids)) != len(ids):
        raise _bad("CATALOG_DUPLICATE_REQUIREMENT")
    if not any(not r.optional for r in parsed):
        raise _bad("CATALOG_NO_REQUIRED_CHECKS")
    return RequiredCatalog(
        id=o["id"],
        version=o["version"],
        empty_output_allowed=_bool(o["empty_output_allowed"]),
        requirements=parsed,
        digest="sha256:" + hashlib.sha256(canonical_bytes(doc)).hexdigest(),
    )

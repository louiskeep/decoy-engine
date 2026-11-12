"""The ``decoy-publication/v1`` descriptor: one field declaration, its writer and its strict reader.

Pattern: Git content addressing plus RFC 8785 canonical JSON (Git object model, GPL-2.0 docs; RFC 8785, IETF). See: https://www.rfc-editor.org/rfc/rfc8785.

Every published unit carries ``_decoy_publication.json``. Its bytes are canonical JSON (sorted
keys, fixed separators, ASCII, no NaN), so equal content has equal bytes and
``descriptor_digest`` is a plain SHA-256 of them. ``result_id`` names the output content alone,
as Git names an object by its bytes: a domain-separated SHA-256 of the canonical artifact list.
It leaves out ``operation_id`` (the retry handle, Delta Lake's transaction identifier) and the
vault, which is judged by its rows, never by a digest of them.

One declaration (``DESCRIPTOR_FIELDS`` and the two nested ones) drives both directions, so the
writer cannot emit what the reader refuses. The reader is the persisted-reader contract: exact
version tag, every field present, no unknown field, exact built-in JSON types (``bool`` is never
an ``int``), bounded counts, closed tokens, ``re.fullmatch`` shapes, relative paths through the
engine's one component validator, no duplicate key, no non-finite number, canonical bytes, a
``result_id`` that recomputes, and at most 1 MiB. A refusal raises ``ValueError`` whose text is
``descriptor_refused:<check>`` with a closed check token and nothing from the bytes, raised
outside any parse handler so no exception context carries them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final, Literal, NoReturn, Protocol

from decoy_engine.fileaccess._component import is_valid_component
from decoy_engine.publication._types import (
    OPERATION_ID_PATTERN,
    ROLES,
    UNIT_IMMUTABLE_DIRECTORY,
    UNITS,
    ArtifactSummary,
    Role,
    Unit,
    is_count,
    is_hex64,
    is_operation_id,
)
from decoy_engine.release import __version__

__all__ = [
    "ARTIFACT_FIELDS",
    "DESCRIPTOR_FIELDS",
    "DESCRIPTOR_NAME",
    "MAX_DESCRIPTOR_BYTES",
    "REFUSAL_PREFIX",
    "RESULT_DOMAIN",
    "SCHEMA",
    "VAULT_FIELDS",
    "Artifact",
    "Check",
    "Descriptor",
    "DescriptorVault",
    "build_descriptor",
    "compute_result_id",
    "decode_descriptor",
    "describe_declaration",
    "descriptor_digest",
    "encode_descriptor",
]

SCHEMA: Final = "decoy-publication/v1"
DESCRIPTOR_NAME: Final = "_decoy_publication.json"
MAX_DESCRIPTOR_BYTES: Final = 1 << 20
RESULT_DOMAIN: Final = b"decoy-publication-result/v1\x00"
REFUSAL_PREFIX: Final = "descriptor_refused:"

Check = Literal[
    "oversize",
    "not_json",
    "duplicate_key",
    "non_finite",
    "not_object",
    "schema_missing",
    "schema_unknown",
    "missing_field",
    "unknown_field",
    "wrong_type",
    "out_of_range",
    "bad_token",
    "bad_path",
    "artifacts_order",
    "result_id_mismatch",
    "not_canonical",
]

_VERSION_PATTERN: Final = r"[0-9][0-9A-Za-z.+!_-]{0,63}"
_VERSION = re.compile(_VERSION_PATTERN)
_HEX_PATTERN: Final = r"[0-9a-f]{64}"
# A JSON integer longer than this cannot be a count; refusing it before int() keeps a hostile
# digit run from costing quadratic time.
_MAX_INT_DIGITS: Final = 20


def _refuse(check: Check) -> NoReturn:
    raise ValueError(REFUSAL_PREFIX + check)


@dataclass(frozen=True)
class Artifact:
    """One measured file of the unit. ``path`` is relative to the unit root."""

    path: str
    role: Role
    rows: int
    sha256: str
    size: int

    def summary(self) -> ArtifactSummary:
        return ArtifactSummary(role=self.role, size=self.size, rows=self.rows, sha256=self.sha256)


@dataclass(frozen=True)
class DescriptorVault:
    """The vault a unit was bound with: its digest, byte size and entry count."""

    sha256: str
    size: int
    entry_count: int


@dataclass(frozen=True)
class Descriptor:
    """The typed descriptor; the same model is written and read."""

    schema: str
    unit: Unit
    operation_id: str
    result_id: str
    artifacts: tuple[Artifact, ...]
    vault: DescriptorVault | None
    engine_version: str


class _Spec(Protocol):
    def load(self, raw: Any) -> Any: ...
    def dump(self, value: Any) -> Any: ...
    def describe(self) -> str: ...


@dataclass(frozen=True)
class _Exact:
    value: str

    def load(self, raw: Any) -> str:
        if type(raw) is not str or raw != self.value:
            _refuse("schema_unknown")
        return raw

    def dump(self, value: Any) -> Any:
        return value

    def describe(self) -> str:
        return f"exact:{self.value}"


@dataclass(frozen=True)
class _OneOf:
    values: tuple[str, ...]

    def load(self, raw: Any) -> str:
        if type(raw) is not str:
            _refuse("wrong_type")
        if raw not in self.values:
            _refuse("bad_token")
        return raw

    def dump(self, value: Any) -> Any:
        return value

    def describe(self) -> str:
        return "one_of:" + "|".join(self.values)


@dataclass(frozen=True)
class _Shaped:
    pattern: str
    test: Any  # (object) -> bool, the one shape check for this kind of value

    def load(self, raw: Any) -> str:
        if type(raw) is not str:
            _refuse("wrong_type")
        if not self.test(raw):
            _refuse("bad_token")
        return raw

    def dump(self, value: Any) -> Any:
        return value

    def describe(self) -> str:
        return f"fullmatch:{self.pattern}"


class _Count:
    def load(self, raw: Any) -> int:
        if type(raw) is not int:
            _refuse("wrong_type")
        if not is_count(raw):
            _refuse("out_of_range")
        return raw

    def dump(self, value: Any) -> Any:
        return value

    def describe(self) -> str:
        return "count"


class _RelPath:
    def load(self, raw: Any) -> str:
        if type(raw) is not str:
            _refuse("wrong_type")
        # Each component through the engine's one validator: no empty, `.`, `..` or NUL part,
        # so no absolute path and no escape from the unit.
        if raw == DESCRIPTOR_NAME or not all(is_valid_component(p) for p in raw.split("/")):
            _refuse("bad_path")
        return raw

    def dump(self, value: Any) -> Any:
        return value

    def describe(self) -> str:
        return "rel_path"


@dataclass(frozen=True)
class _List:
    item: _Spec

    def load(self, raw: Any) -> tuple[Any, ...]:
        if type(raw) is not list:
            _refuse("wrong_type")
        return tuple(self.item.load(entry) for entry in raw)

    def dump(self, value: Any) -> Any:
        if type(value) is not tuple:
            _refuse("wrong_type")
        return [self.item.dump(entry) for entry in value]

    def describe(self) -> str:
        return f"list:{self.item.describe()}"


@dataclass(frozen=True)
class _Nullable:
    item: _Spec

    def load(self, raw: Any) -> Any:
        return None if raw is None else self.item.load(raw)

    def dump(self, value: Any) -> Any:
        return None if value is None else self.item.dump(value)

    def describe(self) -> str:
        return f"nullable:{self.item.describe()}"


@dataclass(frozen=True)
class _Object:
    cls: type
    fields: tuple[tuple[str, _Spec], ...]

    def load(self, raw: Any) -> Any:
        if type(raw) is not dict:
            _refuse("wrong_type")
        names = {name for name, _ in self.fields}
        if not names <= raw.keys():
            _refuse("missing_field")
        if not raw.keys() <= names:
            _refuse("unknown_field")
        return self.cls(**{name: spec.load(raw[name]) for name, spec in self.fields})

    def dump(self, value: Any) -> dict[str, Any]:
        if type(value) is not self.cls:
            _refuse("wrong_type")
        return {name: spec.dump(getattr(value, name)) for name, spec in self.fields}

    def describe(self) -> str:
        inner = ",".join(f"{name}={spec.describe()}" for name, spec in self.fields)
        return f"object:({inner})"


_HEX = _Shaped(_HEX_PATTERN, is_hex64)
_SCHEMA_TAG = _Exact(SCHEMA)

ARTIFACT_FIELDS: Final[tuple[tuple[str, _Spec], ...]] = (
    ("path", _RelPath()),
    ("role", _OneOf(ROLES)),
    ("rows", _Count()),
    ("sha256", _HEX),
    ("size", _Count()),
)
VAULT_FIELDS: Final[tuple[tuple[str, _Spec], ...]] = (
    ("sha256", _HEX),
    ("size", _Count()),
    ("entry_count", _Count()),
)
_ARTIFACT = _Object(Artifact, ARTIFACT_FIELDS)
DESCRIPTOR_FIELDS: Final[tuple[tuple[str, _Spec], ...]] = (
    ("schema", _SCHEMA_TAG),
    ("unit", _OneOf(UNITS)),
    ("operation_id", _Shaped(OPERATION_ID_PATTERN, is_operation_id)),
    ("result_id", _HEX),
    ("artifacts", _List(_ARTIFACT)),
    ("vault", _Nullable(_Object(DescriptorVault, VAULT_FIELDS))),
    ("engine_version", _Shaped(_VERSION_PATTERN, lambda v: _VERSION.fullmatch(v) is not None)),
)
_DOCUMENT = _Object(Descriptor, DESCRIPTOR_FIELDS)


def describe_declaration() -> tuple[tuple[str, str], ...]:
    """The wire shape as text, for the schema snapshot."""
    return tuple((name, spec.describe()) for name, spec in DESCRIPTOR_FIELDS)


def _canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _ordered(artifacts: Iterable[Artifact]) -> tuple[Artifact, ...]:
    return tuple(sorted(artifacts, key=lambda a: a.path))


def compute_result_id(artifacts: Iterable[Artifact]) -> str:
    """The content identity of a unit: its artifacts in path order, nothing else."""
    listed = [_ARTIFACT.dump(a) for a in _ordered(artifacts)]
    return hashlib.sha256(RESULT_DOMAIN + _canonical(listed)).hexdigest()


def descriptor_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_document(raw: Any) -> Descriptor:
    """The declaration plus the document rules; the writer and the reader both end here."""
    descriptor: Descriptor = _DOCUMENT.load(raw)
    paths = [a.path for a in descriptor.artifacts]
    if any(a >= b for a, b in pairwise(paths)):
        _refuse("artifacts_order")
    if compute_result_id(descriptor.artifacts) != descriptor.result_id:
        _refuse("result_id_mismatch")
    return descriptor


def build_descriptor(
    *,
    operation_id: str,
    artifacts: Iterable[Artifact],
    vault: DescriptorVault | None,
    unit: Unit = UNIT_IMMUTABLE_DIRECTORY,
    engine_version: str = __version__,
) -> Descriptor:
    """A checked descriptor for measured ``artifacts``, sorted, with its ``result_id``."""
    listed = list(artifacts)
    for artifact in listed:
        _ARTIFACT.load(_ARTIFACT.dump(artifact))
    ordered = _ordered(listed)
    descriptor = Descriptor(
        schema=SCHEMA,
        unit=unit,
        operation_id=operation_id,
        result_id=compute_result_id(ordered),
        artifacts=ordered,
        vault=vault,
        engine_version=engine_version,
    )
    return _check_document(_DOCUMENT.dump(descriptor))


def encode_descriptor(descriptor: Descriptor) -> bytes:
    """The canonical bytes of ``descriptor``, refused on the same rules the reader applies."""
    document = _DOCUMENT.dump(descriptor)
    _check_document(document)
    data = _canonical(document)
    if len(data) > MAX_DESCRIPTOR_BYTES:
        _refuse("oversize")
    return data


def _parse(data: bytes) -> Any:
    """Strict JSON: the hooks record a defect and parsing goes on, so nothing raises inside."""
    found: list[Check] = []

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in items}) != len(items):
            found.append("duplicate_key")
        return dict(items)

    def constant(_name: str) -> None:
        found.append("non_finite")

    def integer(text: str) -> int | None:
        if len(text.lstrip("-")) > _MAX_INT_DIGITS:
            found.append("out_of_range")
            return None
        return int(text)

    parsed: Any = None
    malformed = False
    try:
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_int=integer,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        malformed = True
    if malformed:
        _refuse("not_json")
    if found:
        _refuse(found[0])
    return parsed


def decode_descriptor(data: bytes) -> Descriptor:
    """Read a stored descriptor, refusing any malformed record (see the module docstring)."""
    if type(data) is not bytes:
        _refuse("wrong_type")
    if len(data) > MAX_DESCRIPTOR_BYTES:
        _refuse("oversize")
    raw = _parse(data)
    if type(raw) is not dict:
        _refuse("not_object")
    if "schema" not in raw:
        _refuse("schema_missing")
    _SCHEMA_TAG.load(raw["schema"])
    descriptor = _check_document(raw)
    if _canonical(_DOCUMENT.dump(descriptor)) != data:
        _refuse("not_canonical")
    return descriptor

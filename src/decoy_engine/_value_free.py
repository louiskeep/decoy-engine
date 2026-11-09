"""Value-free leaf types and the one read-model deriver, shared by every versioned document.

A record or run result is written by one version and read by another. Each string leaf is a named
type whose validator keeps data out (no cell value, absolute path or free text), and each carries
a :class:`ValueFree` marker so a guard can walk a model tree and fail on any other string leaf.
:func:`derive_read` turns a strict write model into its tolerant read model: unknown fields are
ignored at every level and every enum or ``Literal`` becomes a checked :data:`Token`, so a value a
later minor added still reads while free text does not.
"""

from __future__ import annotations

import re
import types
from collections.abc import Callable, Collection
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from annotated_types import Ge
from packaging.version import InvalidVersion
from packaging.version import Version as PackagingVersion
from pydantic import AfterValidator, BaseModel, Field, Strict, create_model

from decoy_engine.errors import ConfigRuleError
from decoy_engine.reasons import approve_name, approve_ref

__all__ = [
    "Count",
    "Hex64",
    "InvocationId",
    "RelPath",
    "SafeName",
    "SafeRef",
    "Sha256",
    "Token",
    "UtcTimestamp",
    "ValueFree",
    "Version",
    "derive_read",
    "has_kind",
]


class ValueFree:
    """Marks a leaf type whose validator keeps the value free of data. ``kind`` names it."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        self.kind = kind


def check(kind: str, test: Callable[[str], bool]) -> Callable[[str], str]:
    """A validator that passes ``value`` through when ``test`` holds; the error text is fixed."""

    def validate(value: str) -> str:
        if not test(value):
            raise ConfigRuleError(f"not a valid {kind}")
        return value

    return validate


def matches(pattern: str) -> Callable[[str], bool]:
    compiled = re.compile(pattern)
    return lambda value: compiled.fullmatch(value) is not None


def _relative_path(value: str) -> bool:
    if not value or len(value) > 256 or value.startswith("/"):
        return False
    return all(
        part not in (".", "..")
        and approve_name(part) is not None
        and re.fullmatch(r"[A-Za-z]:", part) is None
        for part in value.split("/")
    )


# The read-side stand-in for every enum: any approved identifier, so a value added in a later
# minor reads, but free text does not.
Token = Annotated[
    str,
    ValueFree("token"),
    AfterValidator(check("token", lambda v: approve_ref(v) is not None)),
]
SafeName = Annotated[
    str,
    ValueFree("safe_name"),
    AfterValidator(check("safe_name", lambda v: approve_name(v) is not None)),
]
SafeRef = Annotated[
    str,
    ValueFree("safe_ref"),
    AfterValidator(check("safe_ref", lambda v: approve_ref(v) is not None)),
]
Sha256 = Annotated[
    str, ValueFree("sha256"), AfterValidator(check("sha256", matches(r"sha256:[0-9a-f]{64}")))
]
Hex64 = Annotated[str, ValueFree("hex64"), AfterValidator(check("hex64", matches(r"[0-9a-f]{64}")))]
_MAX_VERSION_LEN = 64


def _canonical_release(value: str) -> bool:
    """A PEP 440 version in its own canonical spelling, release segment of two or more parts.

    ``packaging`` is the parser; ``str(parsed) == value`` refuses the spellings it would normalize
    (a ``v`` prefix, uppercase, padding, surrounding whitespace, a trailing newline). An epoch is
    refused: Decoy never releases one, so ``1!2.0`` is not a producer version.
    """
    if len(value) > _MAX_VERSION_LEN:
        return False
    try:
        parsed = PackagingVersion(value)
    except InvalidVersion:
        return False
    return str(parsed) == value and parsed.epoch == 0 and len(parsed.release) >= 2


Version = Annotated[str, ValueFree("version"), AfterValidator(check("version", _canonical_release))]
UtcTimestamp = Annotated[
    str,
    ValueFree("utc_timestamp"),
    AfterValidator(check("utc_timestamp", matches(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z"))),
]
InvocationId = Annotated[
    str,
    ValueFree("invocation_id"),
    AfterValidator(
        check(
            "invocation_id",
            matches(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
        )
    ),
]
RelPath = Annotated[str, ValueFree("rel_path"), AfterValidator(check("rel_path", _relative_path))]

# Strict: pydantic's lax mode turns ``False`` into 0 and ``"1"`` into 1.
Count = Annotated[int, Strict(), Ge(0)]


def has_kind(meta: Collection[Any], kind: str) -> bool:
    return any(isinstance(m, ValueFree) and m.kind == kind for m in meta)


def _is_union(origin: object) -> bool:
    return origin is Union or origin is types.UnionType


class _Deriver:
    def __init__(
        self,
        base: type[BaseModel],
        substitute: Callable[[list[Any]], Any | None] | None,
        required_kinds: Collection[str],
        exact_fields: Collection[str],
        module: str,
    ) -> None:
        self.base = base
        self.module = module
        self.substitute = substitute
        self.required_kinds = tuple(required_kinds)
        self.exact_fields = frozenset(exact_fields)
        self.cache: dict[type, type[BaseModel]] = {}

    def annotation(self, ann: Any) -> Any:
        origin = get_origin(ann)
        if origin is Annotated:
            base, *meta = get_args(ann)
            if self.substitute is not None:
                replaced = self.substitute(meta)
                if replaced is not None:
                    return replaced
            return Annotated[(self.annotation(base), *meta)]
        if _is_union(origin):
            return Union[tuple(self.annotation(a) for a in get_args(ann))]  # noqa: UP007
        if origin in (list, tuple, set, frozenset, dict):
            args = tuple(a if a is Ellipsis else self.annotation(a) for a in get_args(ann))
            return origin[args]
        if origin is Literal or (isinstance(ann, type) and issubclass(ann, Enum)):
            return Token
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return self.model(ann)
        return ann

    def model(self, model: type[BaseModel]) -> type[BaseModel]:
        if model in self.cache:
            return self.cache[model]
        fields: dict[str, Any] = {}
        for name, info in model.model_fields.items():
            ann: Any = info.annotation
            if info.metadata:
                ann = Annotated[(ann, *info.metadata)]
            tagged = any(has_kind(info.metadata, k) for k in self.required_kinds)
            # A tag's writer default must not stand in for a document that never stated it.
            default: Any = ... if tagged or info.is_required() else info.default
            # Only a Literal is held exact: a field of the same name that is a model still derives.
            keep = name in self.exact_fields and get_origin(info.annotation) is Literal
            read_ann = ann if keep else self.annotation(ann)
            fields[name] = (read_ann, Field(default, alias=info.alias))
        read = create_model(
            f"{model.__name__}Read", __base__=self.base, __module__=self.module, **fields
        )
        self.cache[model] = read
        return read


def derive_read(
    model: type[BaseModel],
    *,
    base: type[BaseModel],
    substitute: Callable[[list[Any]], Any | None] | None = None,
    required_kinds: Collection[str] = (),
    exact_fields: Collection[str] = (),
    module: str,
) -> type[BaseModel]:
    """The tolerant read model of ``model``, built on ``base`` (which sets ``extra="ignore"``).

    Every enum and ``Literal`` becomes :data:`Token`; named value-free types keep their checks.
    ``substitute`` gets an ``Annotated`` field's metadata and returns the read annotation to use in
    its place, or ``None`` to keep going (run results swap reason payloads this way).
    ``required_kinds`` names ``ValueFree`` kinds that must be present on read even when the writer
    has a default. ``exact_fields`` keeps the named ``Literal`` fields as written, for a schema tag or a
    producer id that a reader must match, not tolerate. ``module`` is the caller's ``__name__``:
    derived models are stamped with it so a public read model keeps its import path and pickles.
    """
    return _Deriver(base, substitute, required_kinds, exact_fields, module).model(model)

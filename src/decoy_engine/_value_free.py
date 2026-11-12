"""The value-free marker and the one read-model deriver, shared by every versioned document.

A record or run result is written by one version and read by another. Each string leaf is a named
type whose validator keeps data out (no cell value, absolute path or free text), and each carries
a :class:`ValueFree` marker so a guard can walk a model tree and fail on any other string leaf.
The named leaf types and their one validator factory live in ``run_result/_model.py``, the
reviewed site of the fixed error text. :func:`derive_read` turns a strict write model into its
tolerant read model: unknown fields are ignored at every level and every enum or ``Literal``
becomes the checked ``Token`` leaf it is given, so a value a later minor added still reads while
free text does not. A validator both sides must run lives on a :class:`SharedRules` class
(validators only: no fields, no config) that the write model inherits, and the read model
inherits it too; a validator put straight on a write model fails the derivation at import
instead of being dropped.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Collection
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from annotated_types import Ge
from pydantic import BaseModel, Field, Strict, create_model

__all__ = [
    "Count",
    "SharedRules",
    "ValueFree",
    "derive_read",
    "has_kind",
]


class SharedRules(BaseModel):
    """Base of a rules class: validators a write model and its derived read model both run."""


class ValueFree:
    """Marks a leaf type whose validator keeps the value free of data. ``kind`` names it."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        self.kind = kind


# Strict: pydantic's lax mode turns ``False`` into 0 and ``"1"`` into 1.
Count = Annotated[int, Strict(), Ge(0)]


def has_kind(meta: Collection[Any], kind: str) -> bool:
    return any(isinstance(m, ValueFree) and m.kind == kind for m in meta)


def _validator_names(model: type[BaseModel]) -> set[str]:
    deco = model.__pydantic_decorators__
    groups = (deco.validators, deco.field_validators, deco.root_validators, deco.model_validators)
    return {name for group in groups for name in group}


def _owner(model: type[BaseModel], name: str) -> type:
    return next(c for c in model.__mro__ if name in vars(c))


def _shared_rules(model: type[BaseModel]) -> tuple[type[SharedRules], ...]:
    """The rules classes of ``model`` (MRO order), after a fail-closed check of its validators."""
    rules = tuple(
        c for c in model.__mro__ if issubclass(c, SharedRules) and SharedRules in c.__bases__
    )
    for rule in rules:
        if rule.model_fields:
            raise TypeError(f"{rule.__name__} is a rules class and declares fields")
        if rule.model_config:
            raise TypeError(f"{rule.__name__} is a rules class and sets model_config")
    # By owner, not name: a same-named validator on the model would run on the write side only.
    unshared = sorted(n for n in _validator_names(model) if _owner(model, n) not in rules)
    if unshared:
        raise TypeError(f"{model.__name__} has validators outside a SharedRules class: {unshared}")
    return rules


def _is_union(origin: object) -> bool:
    return origin is Union or origin is types.UnionType


class _Deriver:
    def __init__(
        self,
        base: type[BaseModel],
        token: Any,
        substitute: Callable[[list[Any]], Any | None] | None,
        required_kinds: Collection[str],
        exact_fields: Collection[str],
        module: str,
    ) -> None:
        self.base = base
        self.token = token
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
            return self.token
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return self.model(ann)
        return ann

    def model(self, model: type[BaseModel]) -> type[BaseModel]:
        if model in self.cache:
            return self.cache[model]
        rules = _shared_rules(model)
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
            f"{model.__name__}Read",
            __base__=(self.base, *rules),
            __module__=self.module,
            **fields,
        )
        self.cache[model] = read
        return read


def derive_read(
    model: type[BaseModel],
    *,
    base: type[BaseModel],
    token: Any,
    substitute: Callable[[list[Any]], Any | None] | None = None,
    required_kinds: Collection[str] = (),
    exact_fields: Collection[str] = (),
    module: str,
) -> type[BaseModel]:
    """The tolerant read model of ``model``, built on ``base`` (which sets ``extra="ignore"``).

    Every enum and ``Literal`` becomes ``token`` (the checked ``Token`` leaf, which lives with the one validator factory in ``run_result/_model.py``); named value-free types keep their checks.
    ``substitute`` gets an ``Annotated`` field's metadata and returns the read annotation to use in
    its place, or ``None`` to keep going (run results swap reason payloads this way).
    ``required_kinds`` names ``ValueFree`` kinds that must be present on read even when the writer
    has a default. ``exact_fields`` keeps the named ``Literal`` fields as written, for a schema tag or a
    producer id that a reader must match, not tolerate. The read model also inherits each
    ``SharedRules`` class of ``model``; ``TypeError`` if ``model`` holds a validator that no such
    class owns, or a rules class declares fields or config. ``module`` is the caller's ``__name__``:
    derived models are stamped with it so a public read model keeps its import path and pickles.
    """
    return _Deriver(base, token, substitute, required_kinds, exact_fields, module).model(model)

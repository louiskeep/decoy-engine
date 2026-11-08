"""Derive the tolerant read model of a strict payload model.

A record is written by one version and read by another. The write model refuses unknown fields so
a producer cannot slip in a free-text field; the read model ignores fields a later version added
under the same tag, nested models included, and keeps every type check on the fields it knows.
"""

from __future__ import annotations

import types
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, create_model

__all__ = ["read_model_of"]


class _ReadBase(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)


def _is_union(origin: object) -> bool:
    return origin is Union or origin is types.UnionType


def _annotation(ann: Any, cache: dict[type, type]) -> Any:
    origin = get_origin(ann)
    if origin is Annotated:
        base, *meta = get_args(ann)
        return Annotated[(_annotation(base, cache), *meta)]
    if _is_union(origin):
        return Union[tuple(_annotation(a, cache) for a in get_args(ann))]  # noqa: UP007
    if origin in (list, tuple, set, frozenset, dict):
        args = tuple(a if a is Ellipsis else _annotation(a, cache) for a in get_args(ann))
        return origin[args]
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return read_model_of(ann, cache)
    return ann


def read_model_of(model: type[BaseModel], cache: dict[type, type] | None = None) -> type[BaseModel]:
    """``model`` with unknown fields ignored at every level."""
    cache = {} if cache is None else cache
    if model in cache:
        return cache[model]
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        ann: Any = info.annotation
        if info.metadata:
            ann = Annotated[(ann, *info.metadata)]
        default: Any = ... if info.is_required() else info.default
        fields[name] = (_annotation(ann, cache), Field(default, alias=info.alias))
    read = create_model(f"{model.__name__}Read", __base__=_ReadBase, __module__=__name__, **fields)
    cache[model] = read
    return read

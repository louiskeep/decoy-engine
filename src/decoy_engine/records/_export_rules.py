"""The default-export rule of a record kind: how its payload is proven free of paths and values.

Every writable kind declares one (``KindSpec.export_rule``; a spec without one is refused), so a
new kind cannot ship an export that only strips what someone remembered to name. A rule takes the
payload and returns the version safe to export plus whether it removed a remote locator, or
``None`` when it cannot prove the payload safe, in which case the record is withheld.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["ExportRule", "file_name", "pointer_rule"]

ExportRule = Callable[[dict[str, Any]], "tuple[dict[str, Any], bool] | None"]

_SEPARATORS = re.compile(r"[\\/]+")


def file_name(value: str) -> str:
    parts = [p for p in _SEPARATORS.split(value) if p]
    return parts[-1] if parts else value


def _reduce(node: Any, segments: list[str]) -> bool:
    """Reduce the strings the pointer ``segments`` reach to file names. True when one changed."""
    head, rest = segments[0], segments[1:]
    if isinstance(node, list) and head == "*":
        children: list[Any] = list(node)
    elif isinstance(node, dict) and head == "*":
        children = list(node.values())
    elif isinstance(node, dict) and head in node:
        if not rest:
            value = node[head]
            if isinstance(value, str) and file_name(value) != value:
                node[head] = file_name(value)
                return True
            return False
        children = [node[head]]
    else:
        return False
    if not rest:
        return False
    return any([_reduce(child, rest) for child in children])


def _drop(node: Any, segments: list[str]) -> bool:
    """Remove the set values the pointer ``segments`` reaches. True when one was removed."""
    head, rest = segments[0], segments[1:]
    if head == "*":
        if isinstance(node, list):
            children: list[Any] = list(node)
        elif isinstance(node, dict):
            children = list(node.values())
        else:
            return False
        return any([_drop(child, rest) for child in children])
    if not isinstance(node, dict) or head not in node:
        return False
    if rest:
        return _drop(node[head], rest)
    if node[head] is None:
        return False
    del node[head]
    return True


def pointer_rule(
    path_fields: tuple[str, ...],
    locator_fields: tuple[str, ...] = (),
    *,
    check: Callable[[Mapping[str, Any]], bool] | None = None,
    scrub: Callable[[dict[str, Any]], bool] | None = None,
) -> ExportRule:
    """A rule from JSON-pointer lists: path fields are reduced to file names and locator fields
    removed. ``check`` must hold for the payload as stored. ``scrub`` edits a copy in place for
    what the pointers cannot say (free text, loose structures) and returns False when what is left
    cannot be proven value-free."""

    def rule(payload: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
        if check is not None and not check(payload):
            return None
        out = copy.deepcopy(payload)
        if scrub is not None and not scrub(out):
            return None
        for pointer in path_fields:
            _reduce(out, pointer.split("/")[2:])
        removed = any([_drop(out, pointer.split("/")[2:]) for pointer in locator_fields])
        return out, removed

    return rule

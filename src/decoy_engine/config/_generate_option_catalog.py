"""Read-only views of the generate-option registry: the public catalog and the editor schema.

``generate_option_catalog`` lists each type's option names for callers such as ``decoy explain``,
and ``closed_key_schema`` closes each type's key set in the JSON schema ``decoy schema`` prints.
Both read ``GENERATE_OPTIONS`` and ``CONDITIONAL_REQUIRED`` in ``_generate_options``, so a new
type or option shows up in both with no second list. Neither holds a check callable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from decoy_engine.config._generate_options import (
    COMMON_OPTIONS,
    CONDITIONAL_REQUIRED,
    GENERATE_OPTIONS,
    INTERNAL_KEYS,
    TypeOptions,
)

__all__ = [
    "POOLED_TYPE",
    "GenerateTypeOptions",
    "closed_key_schema",
    "generate_option_catalog",
]


@dataclass(frozen=True)
class GenerateTypeOptions:
    """Public, read-only view of one generate type's option names.

    Names only: the registry's validator callables stay private. `common` is the set every type
    may carry beyond its own keys (`COMMON_OPTIONS`).
    """

    type: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    common: tuple[str, ...]
    # selector key -> selector value -> keys that value requires, e.g.
    # {"family": {"uniform": ("low", "high")}}. Empty for a type with no conditional rule.
    required_when: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    # The subset of `required` that is checked at plan compile, not at config parse.
    required_at_plan: tuple[str, ...] = ()


# The one type that takes `pooled`; `GenerateColumnConfig` refuses it elsewhere.
POOLED_TYPE = "faker"


def closed_key_schema(schema: dict[str, Any]) -> None:
    """JSON-schema hook: one ``if type then {closed key set}`` branch per generate type.

    The schema stays open at the top (the model allows extras) and each branch closes it, so an
    editor flags a typo-shaped key without any new model field.
    """
    shared = ["name", "type", *COMMON_OPTIONS, *sorted(INTERNAL_KEYS)]
    schema["allOf"] = [
        {
            "if": {"properties": {"type": {"const": type_}}, "required": ["type"]},
            "then": _closed_branch(type_, spec, shared),
        }
        for type_, spec in GENERATE_OPTIONS.items()
    ]


def _closed_branch(type_: str, spec: TypeOptions, shared: list[str]) -> dict[str, Any]:
    """One type's closed key set, plus a nested ``if selector == value`` branch per table entry."""
    properties: dict[str, Any] = {key: {} for key in [*shared, *spec.keys]}
    # `model_dump()` stamps `pooled: null` on every column, so other types may carry the null.
    properties["pooled"] = {} if type_ == POOLED_TYPE else {"type": "null"}
    then: dict[str, Any] = {
        "properties": properties,
        "required": [*spec.required, *spec.plan_required],
        "additionalProperties": False,
    }
    nested: list[dict[str, Any]] = []
    for selector, by_value in CONDITIONAL_REQUIRED.get(type_, {}).items():
        for value, keys in by_value.items():
            inner: dict[str, Any] = {"required": list(keys)}
            if spec.closed_by == selector:
                foreign = sorted({k for ks in by_value.values() for k in ks} - set(keys))
                if foreign:
                    inner["not"] = {"anyOf": [{"required": [k]} for k in foreign]}
            nested.append(
                {
                    "if": {"properties": {selector: {"const": value}}, "required": [selector]},
                    "then": inner,
                }
            )
    if nested:
        then["allOf"] = nested
    return then


def generate_option_catalog() -> Mapping[str, GenerateTypeOptions]:
    """Every generate type with its required, optional and common option names, read from
    `GENERATE_OPTIONS` so a new type or option shows up here with no second list."""
    common = tuple(COMMON_OPTIONS)
    return {
        type_: GenerateTypeOptions(
            type=type_,
            required=(*spec.required, *spec.plan_required),
            optional=tuple(
                key for key in spec.keys if key not in (*spec.required, *spec.plan_required)
            ),
            common=common,
            required_when={
                selector: dict(by_value)
                for selector, by_value in CONDITIONAL_REQUIRED.get(type_, {}).items()
            },
            required_at_plan=spec.plan_required,
        )
        for type_, spec in GENERATE_OPTIONS.items()
    }

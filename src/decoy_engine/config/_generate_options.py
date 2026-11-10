"""The generate-column option registry: which keys each column type reads and what shape they take.

``GenerateColumnConfig`` keeps ``extra="allow"`` because the options ride flat on the column and
the seed fingerprint hashes every dumped key (``generators/derivation.py``), so declaring them as
fields would change output bytes. This registry gives the same closed key set without touching
the dump: ``option_problems`` reads the extras and never writes them.

The registry is the config layer's authority; ``tests/sentry/defects/test_generate_option_reads.py``
checks it against the keys the generators read. It holds no pyarrow or Faker import. Whatever
needs them (locale, provider and kwargs names, category typing) is a plan check.

Refusal text is the registered reason's fixed summary, so a submitted value never reaches a
message. Check functions return ``(relative location, reason code)`` pairs.
"""

from __future__ import annotations

import ast
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic_core import InitErrorDetails, PydanticCustomError

from decoy_engine import reasons
from decoy_engine.config._generate_unique import unique_flag

__all__ = [
    "COMMON_OPTIONS",
    "GENERATE_OPTIONS",
    "INTERNAL_KEYS",
    "OptionSpec",
    "TypeOptions",
    "closed_key_schema",
    "option_problems",
    "problem_details",
]

_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
_RANGE = str(reasons.config.GENERATE_OPTION_RANGE)
_VALUE = str(reasons.config.GENERATE_OPTION_VALUE_UNKNOWN)
_WEIGHTS = str(reasons.config.GENERATE_WEIGHTS_INVALID)
_FORMULA = str(reasons.config.GENERATE_FORMULA_INVALID)
UNKNOWN = str(reasons.config.GENERATE_OPTION_UNKNOWN)

Problem = tuple[tuple[str | int, ...], str]
Check = Callable[[Any, Mapping[str, Any]], list[Problem]]

_OK: list[Problem] = []


@dataclass(frozen=True)
class OptionSpec:
    """One option: a static check and, for E2, whether the value names sibling columns.

    ``ref_kind`` is ``None``, ``column``, ``column_list`` or ``expression``.
    """

    check: Check | None = None
    ref_kind: str | None = None


@dataclass(frozen=True)
class TypeOptions:
    """The keys one column type reads, and the ones it cannot run without."""

    keys: Mapping[str, OptionSpec]
    required: tuple[str, ...] = ()


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def _str(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    return _OK if isinstance(value, str) else [((), _TYPE)]


def _bool(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    return _OK if type(value) is bool else [((), _TYPE)]


def _mapping(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    # Keys become keyword names or column names, and the plan stores them as JSON, so a
    # non-string key would fail at run or be coerced; refuse it here.
    if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
        return [((), _TYPE)]
    return _OK


def _int_at_least(minimum: int | None, *, nonzero: bool = False) -> Check:
    def check(value: Any, _: Mapping[str, Any]) -> list[Problem]:
        if not _is_int(value):
            return [((), _TYPE)]
        if (minimum is not None and value < minimum) or (nonzero and value == 0):
            return [((), _RANGE)]
        return _OK

    return check


def _str_list(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    if not isinstance(value, list):
        return [((), _TYPE)]
    return [((i,), _TYPE) for i, item in enumerate(value) if not isinstance(item, str)]


def _locale(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    if isinstance(value, str):
        return _OK
    if isinstance(value, list) and value:
        return _str_list(value, extras)
    return [((), _TYPE)]


def _null_probability(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    if not _is_number(value):
        return [((), _TYPE)]
    return _OK if 0 <= value <= 1 else [((), _RANGE)]


def _determinism(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    if not isinstance(value, str):
        return [((), _TYPE)]
    return _OK if value in ("stable", "fresh") else [((), _VALUE)]


def _distribution(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    if not isinstance(value, str):
        return [((), _TYPE)]
    return _OK if value in ("random", "weighted", "sequential", "fitted") else [((), _VALUE)]


def _formula(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    if not isinstance(value, str):
        return [((), _TYPE)]
    try:
        ast.parse(value.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return [((), _FORMULA)]
    return _OK


def _categories(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    return _OK if isinstance(value, list) else [((), _TYPE)]


def _weight_ok(w: Any) -> bool:
    """A non-negative weight the generators can turn into a finite float.

    Ints compare directly (`math.isfinite` overflows on a huge int); an int too large for a
    float is refused because the samplers call `float()` on it.
    """
    if type(w) is float:
        return math.isfinite(w) and w >= 0
    if type(w) is int:
        if w < 0:
            return False
        try:
            float(w)
        except OverflowError:
            return False
        return True
    return False


def _weights(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    """A list of non-negative finite int or float with a positive sum (bool is not a number)."""
    if not isinstance(value, list):
        return [((), _TYPE)]
    bad: list[Problem] = [((i,), _WEIGHTS) for i, w in enumerate(value) if not _weight_ok(w)]
    if bad:
        return bad
    categories = extras.get("categories")
    # Categorical weights pair with categories; a reference's pair with the parent (plan check).
    if extras.get("type") == "categorical" and isinstance(categories, list):
        if len(value) != len(categories):
            return [((), _WEIGHTS)]
    try:
        total = math.fsum(value)
    except OverflowError:
        return [((), _WEIGHTS)]
    return _OK if math.isfinite(total) and total > 0 else [((), _WEIGHTS)]


_S = OptionSpec(_str)
_B = OptionSpec(_bool)
_I = OptionSpec(_int_at_least(None))
_M = OptionSpec(_mapping)

COMMON_OPTIONS: Mapping[str, OptionSpec] = {
    "null_probability": OptionSpec(_null_probability),
    "determinism": OptionSpec(_determinism),
    # Accepted on `sequence`, `categorical`, `faker` and `reference`; the type rule is
    # `_generate_unique.unique_type_problems`, run by the column validator.
    "unique": OptionSpec(unique_flag),
}

# Engine-internal keys the generators stamp or read that a config may carry. None today.
INTERNAL_KEYS: frozenset[str] = frozenset()

GENERATE_OPTIONS: Mapping[str, TypeOptions] = {
    "faker": TypeOptions(
        {
            "faker_type": _S,
            "locale": OptionSpec(_locale),
            "faker_kwargs": _M,
        },
        required=("faker_type",),
    ),
    "sequence": TypeOptions(
        {
            "start": _I,
            "step": OptionSpec(_int_at_least(None, nonzero=True)),
            "prefix": _S,
            "suffix": _S,
            "pad_length": OptionSpec(_int_at_least(0)),
        },
        required=("start",),
    ),
    "categorical": TypeOptions(
        {"categories": OptionSpec(_categories), "weights": OptionSpec(_weights)},
        required=("categories",),
    ),
    "formula": TypeOptions(
        {"formula": OptionSpec(_formula), "references": OptionSpec(_str_list)},
        required=("formula",),
    ),
    "reference": TypeOptions(
        {
            "reference_table": _S,
            "reference_column": _S,
            "distribution": OptionSpec(_distribution),
            "weights": OptionSpec(_weights),
            "min_per_parent": OptionSpec(_int_at_least(0)),
            "max_per_parent": OptionSpec(_int_at_least(0)),
            "snapshot_file": _S,
            "source_column": _S,
        },
        required=("reference_table", "reference_column"),
    ),
    "statistical": TypeOptions(
        {
            "snapshot_file": _S,
            "source_column": _S,
            "high_cardinality": _B,
            "allow_real_categories": _B,
            "other_mode": _S,
            "condition_on": OptionSpec(_str, ref_kind="column"),
        },
        required=("snapshot_file",),
    ),
    "derived": TypeOptions(
        {
            "expression": OptionSpec(_str, ref_kind="expression"),
            "bounds": _M,
            "null_propagation": _S,
        },
        required=("expression",),
    ),
    "derived_aggregate": TypeOptions(
        {"op": _S, "column": OptionSpec(_str, ref_kind="column")},
    ),
    "grouped_series": TypeOptions(
        {
            "group_by": OptionSpec(_str, ref_kind="column"),
            "order_by": OptionSpec(_str, ref_kind="column"),
            "generator": _S,
            "step": _I,
            "max_step": _I,
            "start": _I,
        },
        required=("group_by", "order_by"),
    ),
    "windowed_date": TypeOptions(
        {
            "anchor": OptionSpec(_str, ref_kind="column"),
            "max_days": _I,
            "min_days": _I,
            "distribution": _S,
        },
        required=("anchor", "max_days"),
    ),
    "group_key": TypeOptions(
        {"group_by": OptionSpec(_str, ref_kind="column"), "length": _I, "prefix": _S},
        required=("group_by",),
    ),
}

# Optional keys whose readers treat None as "not given" (`col.get(k)`, `col.get(k) or default`, or
# an explicit None test). A None on any other key reaches `int(None)`, `str(None)` or a literal
# comparison at run time, so it gets `generate_option_type`.
_NONE_MEANS_ABSENT = frozenset(
    {
        "weights",
        "locale",
        "faker_kwargs",
        "null_probability",
        "unique",
        "references",
        "min_per_parent",
        "max_per_parent",
        "bounds",
        "high_cardinality",
        "allow_real_categories",
        "condition_on",
        "other_mode",
        "source_column",
    }
)

# A required key counts as given when it is not None, so `start: 0` and `max_days: 0` are valid.
_NONE_MEANS_MISSING = frozenset({"start", "max_days"})


def _missing(key: str, value: Any) -> bool:
    return value is None if key in _NONE_MEANS_MISSING else not value


def option_problems(type_: str, extras: Mapping[str, Any]) -> list[Problem]:
    """Every problem with a column's flat options, in a stable order, as (location, code).

    Reads ``extras`` and never changes it. A `None` value means "not given" only for the keys in
    ``_NONE_MEANS_ABSENT`` (their readers treat None as absent); a required key with None is
    missing, and any other key with None is a type error. Nothing here quotes a submitted value.
    """
    spec = GENERATE_OPTIONS[type_]
    found: list[Problem] = []
    check_input = {**extras, "type": type_}
    for key, value in extras.items():
        option = spec.keys.get(key) or COMMON_OPTIONS.get(key)
        if option is None:
            if key not in INTERNAL_KEYS:
                found.append(((key,), UNKNOWN))
            continue
        if key in spec.required and _missing(key, value):
            continue
        if value is None:
            if key not in _NONE_MEANS_ABSENT:
                found.append(((key,), _TYPE))
            continue
        if option.check is None:
            continue
        found.extend(((key, *rel), code) for rel, code in option.check(value, check_input))
    given_missing = [k for k in spec.required if _missing(k, extras.get(k))]
    found.extend(((k,), "missing") for k in given_missing)
    if (
        type_ == "reference"
        and extras.get("distribution") == "weighted"
        and extras.get("weights") is None
    ):
        found.append((("weights",), "missing"))
    return found


def problem_details(problems: list[tuple[tuple[str | int, ...], str]]) -> list[InitErrorDetails]:
    """Pydantic line errors for ``problems``: fixed text per code, no input kept."""
    details: list[InitErrorDetails] = []
    for loc, code in problems:
        kind: Any = "missing"
        if code != "missing":
            kind = PydanticCustomError(code, reasons.lookup(code)[0].summary)
        details.append({"type": kind, "loc": loc, "input": None})
    return details


def closed_key_schema(schema: dict[str, Any]) -> None:
    """JSON-schema hook: one ``if type then {closed key set}`` branch per generate type.

    The schema stays open at the top (the model allows extras) and each branch closes it, so an
    editor flags a typo-shaped key without any new model field.
    """
    shared = ["name", "type", "pooled", *COMMON_OPTIONS, *sorted(INTERNAL_KEYS)]
    schema["allOf"] = [
        {
            "if": {"properties": {"type": {"const": type_}}, "required": ["type"]},
            "then": {
                "properties": {key: {} for key in [*shared, *spec.keys]},
                "required": list(spec.required),
                "additionalProperties": False,
            },
        }
        for type_, spec in GENERATE_OPTIONS.items()
    ]

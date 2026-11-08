"""The one place that decides which profile fields hold cell values, and removes them.

`ColumnProfile.to_dict()` stays the raw form (the platform persists and rebuilds it).
Anything that writes a profile for a person to read or commit goes through
`profile_document`, and anything that reads a saved profile back for display goes through
`redact_profile_dict`. Both are driven by `VALUE_FIELDS`; every other field reachable from
`ColumnProfile` is listed in `VALUE_FREE_FIELDS`, and the type walk refuses a field that is
in neither, so a new value-bearing field cannot ship unclassified.

Pattern: classification registry plus default-deny serializer (Google Cloud DLP
`include_quote`, ydata-profiling `sensitive=True`, Microsoft Presidio `replace` operator).
See: https://cloud.google.com/sensitive-data-protection/docs/reference/rest/v2/InspectConfig.
Findings omit the quote unless asked (DLP); counts and shape survive while values go
(ydata); a redacted entry keeps its position as a fixed placeholder (Presidio).
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import functools
import math
import types
from collections.abc import Iterator, Mapping
from typing import Any, Final, Union, get_args, get_origin, get_type_hints

from decoy_engine.profiling.types import ColumnProfile


class Rule(enum.Enum):
    NULL = "null"
    EMPTY_LIST = "empty_list"
    PLACEHOLDER = "placeholder"
    PLACEHOLDER_EACH = "placeholder_each"


REDACTED: Final = "[redacted]"
VALUE_POLICY_KEY: Final = "value_policy"
REDACTED_COLUMNS_KEY: Final = "redacted_columns"
POLICY_REDACTED: Final = "flagged-redacted/v1"
POLICY_RAW: Final = "raw"
PII_CANDIDATE_THRESHOLD: Final = 0.6

VALUE_FIELDS: Final[Mapping[tuple[str, str], Rule]] = types.MappingProxyType(
    {
        ("FieldStats", "min_value"): Rule.NULL,
        ("FieldStats", "max_value"): Rule.NULL,
        ("FieldStats", "date_min"): Rule.NULL,
        ("FieldStats", "date_max"): Rule.NULL,
        ("FieldStats", "mode_value"): Rule.NULL,
        ("FieldStats", "sample_invalid"): Rule.EMPTY_LIST,
        ("TopValue", "value"): Rule.PLACEHOLDER,
        ("DetectorMatch", "sample_misses"): Rule.EMPTY_LIST,
        # Categorical labels are cells and numeric bin edges come from the real min and max.
        ("Distribution", "labels"): Rule.PLACEHOLDER_EACH,
        ("Distribution", "min"): Rule.NULL,
        ("Distribution", "max"): Rule.NULL,
        # The note repeats the literal for known dates and interpolates string sentinels.
        ("SentinelFlag", "value"): Rule.PLACEHOLDER,
        ("SentinelFlag", "note"): Rule.PLACEHOLDER,
    }
)


def _free(type_name: str, *names: str) -> set[tuple[str, str]]:
    return {(type_name, n) for n in names}


VALUE_FREE_FIELDS: Final[frozenset[tuple[str, str]]] = frozenset(
    _free(
        "ColumnProfile",
        "source_label", "row_count", "sample_strategy", "sample_row_cap", "fields",
        "reid_risk_columns", "reid_risk_score", "quasi_identifier_groups", "k_anonymity",
        "engine_version", "generated_at",
    )
    | _free(
        "FieldStats",
        "name", "inferred_type", "dtype_raw", "row_count", "null_count", "null_rate",
        "distinct_count", "unique_rate", "is_likely_unique", "mean_value", "min_length",
        "max_length", "avg_length", "date_format", "invalid_count", "casing_pattern",
        "format_pattern", "alphabet", "value_set_size_class", "numeric_range_class",
        "mode_freq", "top_values", "detector_matches", "sentinels", "pii_score",
        "distribution", "detection_trail", "domain",
    )
    | _free("TopValue", "count", "pct")
    | _free(
        "DetectorMatch",
        "detector_id", "match_rate", "format_pattern", "confidence", "domain",
    )
    | _free("Distribution", "kind", "data", "mean")
    | _free("SentinelFlag", "kind", "count")
    # Signal text holds detector ids and header names, not cells.
    | _free("DetectionSignal", "signal", "confidence", "winner", "ml", "skipped")
)  # fmt: skip

_ITEM: Final = "*"
_Steps = tuple[str, ...]


class UnclassifiedProfileFieldError(TypeError):
    """A field reachable from the profile root is in neither registry, or in both."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.pairs = pairs
        super().__init__(f"profile fields not classified exactly once: {pairs}")


def _unwrap(hint: Any) -> tuple[Any, _Steps]:
    """Strip `X | None` and `list[...]` layers, returning the inner type and its item steps."""
    steps: _Steps = ()
    while True:
        origin = get_origin(hint)
        if origin in (Union, types.UnionType):
            args = [a for a in get_args(hint) if a is not type(None)]
            if len(args) != 1:
                return hint, steps
            hint = args[0]
        elif origin is list:
            hint, steps = get_args(hint)[0], (*steps, _ITEM)
        else:
            return hint, steps


def _profile_types(root: type) -> dict[_Steps, type]:
    """Dict path of every dataclass reachable from `root` through its type hints.

    Raises `UnclassifiedProfileFieldError` when a reached field is in neither registry or in both.
    """
    found: dict[_Steps, type] = {}
    bad: list[tuple[str, str]] = []

    def visit(cls: type, path: _Steps) -> None:
        found[path] = cls
        hints = get_type_hints(cls)
        for f in dataclasses.fields(cls):
            pair = (cls.__name__, f.name)
            if (pair in VALUE_FIELDS) == (pair in VALUE_FREE_FIELDS):
                bad.append(pair)
            inner, steps = _unwrap(hints[f.name])
            if dataclasses.is_dataclass(inner) and isinstance(inner, type):
                visit(inner, (*path, f.name, *steps))

    visit(root, ())
    if bad:
        raise UnclassifiedProfileFieldError(bad)
    return found


@functools.cache
def profile_types() -> Mapping[_Steps, type]:
    """Dict path (`("fields", "*", "top_values", "*")`) to dataclass, from `ColumnProfile`."""
    return types.MappingProxyType(_profile_types(ColumnProfile))


def _is_score(value: Any) -> bool:
    return type(value) is float and math.isfinite(value) and 0.0 <= value <= 1.0


def is_value_sensitive(field: Mapping[str, Any]) -> bool:
    """True when a profiled column's values must not leave the process by default.

    Any detector hit, a PII score at the candidate threshold, or a near-unique column. A
    missing or ill-typed input counts as sensitive, so a hand-edited file fails closed.
    """
    if not isinstance(field, Mapping):
        return True
    matches = field.get("detector_matches")
    score = field.get("pii_score")
    unique = field.get("is_likely_unique")
    if type(matches) is not list or not _is_score(score) or type(unique) is not bool:
        return True
    return bool(matches) or score >= PII_CANDIDATE_THRESHOLD or unique


def _containers(node: Any, steps: _Steps, parent: Any, key: Any) -> Iterator[dict[str, Any]]:
    """Yield the dicts at `steps` below `node`, replacing an off-shape location in `parent`."""
    if node is None:
        return
    if not steps:
        if isinstance(node, dict):
            yield node
        else:
            parent[key] = REDACTED
        return
    step, rest = steps[0], steps[1:]
    if step == _ITEM:
        if not isinstance(node, list):
            parent[key] = REDACTED
            return
        for i, item in enumerate(node):
            yield from _containers(item, rest, node, i)
    elif not isinstance(node, dict):
        parent[key] = REDACTED
    elif step in node:
        yield from _containers(node[step], rest, node, step)


def _apply(container: dict[str, Any], rules: Mapping[str, Rule]) -> None:
    for name, rule in rules.items():
        if name not in container or container[name] is None:
            continue
        current = container[name]
        if rule is Rule.NULL:
            container[name] = None
        elif rule is Rule.EMPTY_LIST:
            container[name] = []
        elif rule is Rule.PLACEHOLDER_EACH and isinstance(current, list):
            container[name] = [REDACTED] * len(current)
        else:
            container[name] = REDACTED


@functools.cache
def _field_plan() -> tuple[tuple[_Steps, Mapping[str, Rule]], ...]:
    """(steps below one `fields[*]` entry, rules) for every type that has value fields."""
    plan = []
    for path, cls in profile_types().items():
        rules = {f: r for (t, f), r in VALUE_FIELDS.items() if t == cls.__name__}
        if rules and path[:2] == ("fields", _ITEM):
            plan.append((path[2:], types.MappingProxyType(rules)))
    return tuple(plan)


def _redact_field(fields: list[Any], index: int) -> None:
    for steps, rules in _field_plan():
        for container in _containers(fields[index], steps, fields, index):
            _apply(container, rules)


def redact_profile_dict(doc: Mapping[str, Any], *, include_values: bool = False) -> dict[str, Any]:
    """A copy of a profile document with the value-sensitive columns' cell values removed.

    Never mutates `doc` and never trusts its `value_policy` marker, so a second pass is a
    no-op and a forged or legacy file is still redacted. `include_values=True` keeps every
    value and adds a `raw` policy only when the document has none.
    """
    if not isinstance(doc, Mapping):
        raise TypeError(f"a profile document is a JSON object, not {type(doc).__name__}")
    out = copy.deepcopy(dict(doc))
    fields = out.get("fields")
    names: list[str] = []
    if isinstance(fields, list):
        for i, field in enumerate(fields):
            if not is_value_sensitive(field):
                continue
            name = field.get("name") if isinstance(field, Mapping) else None
            names.append(name if isinstance(name, str) else REDACTED)
            if not include_values:
                _redact_field(fields, i)
    elif fields is not None and not include_values:
        out["fields"] = REDACTED
    if include_values:
        out.setdefault(VALUE_POLICY_KEY, POLICY_RAW)
    else:
        out[VALUE_POLICY_KEY] = POLICY_REDACTED
    out[REDACTED_COLUMNS_KEY] = names
    return out


def profile_document(profile: ColumnProfile, *, include_values: bool = False) -> dict[str, Any]:
    """The JSON document to write for a profile: `to_dict()` passed through the redactor."""
    return redact_profile_dict(profile.to_dict(), include_values=include_values)

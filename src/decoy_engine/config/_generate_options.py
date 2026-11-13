"""The generate-column option registry: which keys each column type reads and what shape they take.

``GenerateColumnConfig`` keeps ``extra="allow"`` because the options ride flat on the column and
the seed fingerprint hashes every dumped key (``generators/derivation.py``), so declaring them as
fields would change output bytes. This registry gives the same closed key set without touching
the dump: ``option_problems`` reads the extras and never writes them.

The registry is the config layer's authority; ``tests/sentry/defects/test_generate_option_reads.py``
checks it against the keys the generators read. It holds no pyarrow or Faker import. Whatever
needs them (locale, provider and kwargs names, category typing) is a plan check.
The public catalog and the editor-schema hook read it from ``_generate_option_catalog``.

Refusal text is the registered reason's fixed summary, so a submitted value never reaches a
message. Check functions return ``(relative location, reason code)`` pairs.

The registry is also the one place that says which columns an option reads (``OptionSpec.ref_kind``
with ``ref_context_key`` and ``ref_phase``). ``iter_generate_input_refs`` resolves those
descriptors to table and column edges; the plan check, numeric typing and the reproducibility
report consume the edges and hold no option-key table of their own.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic_core import InitErrorDetails, PydanticCustomError

from decoy_engine import reasons
from decoy_engine.config._generate_type_checks import (
    OUTPUT_TYPE_KINDS,
    by_entries,
    clip_bound,
    constant_value,
    finite,
    high_bound,
    output_type_value,
    weights_list,
)
from decoy_engine.config._generate_unique import unique_flag
from decoy_engine.errors import PipelineValidationError
from decoy_engine.expressions import formula_is_evaluable

__all__ = [
    "COMMON_OPTIONS",
    "CONDITIONAL_REQUIRED",
    "GENERATE_OPTIONS",
    "INTERNAL_KEYS",
    "OptionSpec",
    "TypeOptions",
    "generate_family_problems",
    "option_problems",
    "problem_details",
    "refuse_unlisted_generate_families",
]

_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
_RANGE = str(reasons.config.GENERATE_OPTION_RANGE)
_VALUE = str(reasons.config.GENERATE_OPTION_VALUE_UNKNOWN)
_FORMULA = str(reasons.config.GENERATE_FORMULA_INVALID)
UNKNOWN = str(reasons.config.GENERATE_OPTION_UNKNOWN)

Problem = tuple[tuple[str | int, ...], str]
# A check may add fixed, value-free hint text after the code's summary.
Found = Problem | tuple[tuple[str | int, ...], str, str]
Check = Callable[[Any, Mapping[str, Any]], Sequence[Found]]

_OK: list[Problem] = []


@dataclass(frozen=True)
class OptionSpec:
    """One option: a static check and which columns the value reads.

    ``ref_kind`` is ``None`` or how the value names columns: ``column``, ``column_list`` and
    ``expression`` name columns of the same table. ``column_via_reference`` names a column of the
    table that the `reference` column named by ``ref_context_key`` points at, and
    ``column_in_named_table`` a column of the table that key names; without the companion key
    both fall back to the same table. ``ref_phase`` is ``inline`` (read while the column is
    drawn, so a same-table input must be generated earlier) or ``post_fill`` (read after the
    source table is complete). The plan check refuses an input that is missing or not ready: a
    column read before it exists is a null, an empty aggregate or a single group.
    """

    check: Check | None = None
    ref_kind: str | None = None
    ref_context_key: str | None = None
    ref_phase: str = "inline"


@dataclass(frozen=True)
class TypeOptions:
    """The keys one column type reads, and the ones it cannot run without.

    ``closed_by`` names a selector key of ``CONDITIONAL_REQUIRED`` whose chosen value also closes
    the key set: a key listed under another value of that selector is then refused as unknown.

    ``plan_required`` keys are required too, but config parse leaves them alone: the type's
    transform refuses a missing one at plan compile with its own code (for example
    ``derived_aggregate_op_missing``), and moving that refusal to config parse would change the
    code a caller sees. The catalog and the editor schema still list them as required.

    ``one_of`` names keys of which exactly one must be given (a `lookup` column's `file` or
    `codeset`): none is ``missing`` on the first, and each one after the first given is
    ``generate_option_value_unknown``.

    ``together`` names keys that are given all or none (a conditional categorical's
    `condition_on` and `by`): when one is given, each other one not given is ``missing``.
    """

    keys: Mapping[str, OptionSpec]
    required: tuple[str, ...] = ()
    closed_by: str | None = None
    plan_required: tuple[str, ...] = ()
    one_of: tuple[str, ...] = ()
    together: tuple[str, ...] = ()


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def _str(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    return _OK if isinstance(value, str) else [((), _TYPE)]


def _fitted_only_str(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    """Reference-column snapshot keys mean something only for a fitted fan-out."""
    if extras.get("distribution") != "fitted":
        return [((), UNKNOWN)]
    return _str(value, extras)


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
        tree = ast.parse(value.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return [((), _FORMULA)]
    # Syntax the runtime evaluator cannot run (lambda, walrus) would generate all nulls.
    return _OK if formula_is_evaluable(tree) else [((), _FORMULA)]


def _categories(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    return _OK if isinstance(value, list) else [((), _TYPE)]


def _family(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    if not isinstance(value, str):
        return [((), _TYPE)]
    return _OK if value in CONDITIONAL_REQUIRED["distribution"]["family"] else [((), _VALUE)]


def generate_family_problems(config: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(dotted path, reason code)`` for each `distribution` column of a raw config dict whose
    `family` is missing from the table.

    The one family gate for a raw dict: `compile_plan` and `run_config_only_checks` call it, so a
    family the validator refuses (a deferred one) cannot reach a run by skipping validation. It
    reads the same `_family` check `option_problems` runs, quotes nothing, and never raises.
    """
    found: list[tuple[str, str]] = []
    tables = config.get("tables")
    for table in tables if isinstance(tables, list) else []:
        columns = table.get("generate_columns") if isinstance(table, dict) else None
        for col in columns if isinstance(columns, list) else []:
            if not isinstance(col, dict) or col.get("type") != "distribution":
                continue
            for _, code in _family(col.get("family"), col):
                path = f"tables.{table.get('name', '?')}.generate_columns.{col.get('name', '?')}.family"
                found.append((path, code))
    return found


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

# The one table of family-dependent keys: type -> selector key -> selector value -> the keys that
# value requires. `option_problems` reads required keys from it (the `reference` weights rule
# included), a closed type (`TypeOptions.closed_by`) refuses every key another value lists, the
# catalog shows it as `required_when` and `closed_key_schema` builds its branches from it.
CONDITIONAL_REQUIRED: Mapping[str, Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "reference": {"distribution": {"weighted": ("weights",)}},
    "distribution": {
        "family": {
            "uniform": ("low", "high"),
        }
    },
}

_N = OptionSpec(finite)
_OUT = OptionSpec(output_type_value)
_IN_CHILD = OptionSpec(
    _str, ref_kind="column_in_named_table", ref_context_key="from_table", ref_phase="post_fill"
)

_BASE_OPTIONS: Mapping[str, TypeOptions] = {
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
        {
            "categories": OptionSpec(_categories),
            "weights": OptionSpec(weights_list),
            # An earlier column whose value picks the `by` entry the row draws from.
            "condition_on": OptionSpec(_str, ref_kind="column"),
            "by": OptionSpec(by_entries),
        },
        required=("categories",),
        together=("condition_on", "by"),
    ),
    "formula": TypeOptions(
        {
            "formula": OptionSpec(_formula),
            "references": OptionSpec(_str_list, ref_kind="column_list", ref_phase="post_fill"),
        },
        required=("formula",),
    ),
    "reference": TypeOptions(
        {
            "reference_table": _S,
            "reference_column": _S,
            "distribution": OptionSpec(_distribution),
            "weights": OptionSpec(weights_list),
            "min_per_parent": OptionSpec(_int_at_least(0)),
            "max_per_parent": OptionSpec(_int_at_least(0)),
            # A self-reference only; its range depends on the table's row count, so
            # `_generate_relational.self_reference_problems` checks it and its scope.
            "root_count": _I,
            "snapshot_file": OptionSpec(_fitted_only_str),
            "source_column": OptionSpec(_fitted_only_str),
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
        {
            "op": _S,
            "column": _IN_CHILD,
            # A child table whose `via` references this one: `op` is then taken per parent row,
            # after the child exists (`_generate_relational.aggregate_problems`).
            "from_table": _S,
            "via": _IN_CHILD,
        },
        plan_required=("op", "column"),
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
            "anchor": OptionSpec(
                _str, ref_kind="column_via_reference", ref_context_key="anchor_via"
            ),
            # An earlier `reference` column of this table: `anchor` is then its parent's column.
            "anchor_via": OptionSpec(_str, ref_kind="column"),
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
    "constant": TypeOptions({"value": OptionSpec(constant_value)}, required=("value",)),
    # GEN-TYPES E3: a weighted list from a file (pinned at compile) or a shipped code set. The
    # code set's name is checked at plan time (`plan.checks._lookup_tables`): config sits below
    # `transforms` in the import layers and so cannot read its registry.
    "lookup": TypeOptions(
        {"file": _S, "codeset": _S, "value_column": _S, "weight_column": _S},
        one_of=("file", "codeset"),
    ),
    "distribution": TypeOptions(
        {
            "family": OptionSpec(_family),
            "low": _N,
            "high": OptionSpec(high_bound),
            "min": OptionSpec(clip_bound("min")),
            "max": OptionSpec(clip_bound("max")),
        },
        required=("family",),
        closed_by="family",
    ),
}

# `output_type` is added from `OUTPUT_TYPE_KINDS`, the one table of the types that take it, so the
# option set and the per-type allowed kinds cannot drift apart.
GENERATE_OPTIONS: Mapping[str, TypeOptions] = {
    name: replace(opts, keys={**opts.keys, "output_type": _OUT})
    if name in OUTPUT_TYPE_KINDS
    else opts
    for name, opts in _BASE_OPTIONS.items()
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
        "min",
        "max",
    }
)

# A required key counts as given when it is not None, so `start: 0`, `max_days: 0` and a constant
# `value: 0` or `value: false` are valid. Keys that a `CONDITIONAL_REQUIRED` value requires follow
# the same rule (`mean: 0` is given).
_NONE_MEANS_MISSING = frozenset({"start", "max_days", "value"})


def _missing(key: str, value: Any) -> bool:
    return value is None if key in _NONE_MEANS_MISSING else not value


def _family_keys(type_: str, extras: Mapping[str, Any]) -> tuple[tuple[str, ...], frozenset[str]]:
    """The keys the chosen selector value requires, and the keys it makes foreign.

    A selector that is absent or names a value outside the table adds nothing: the selector's own
    check reports it. Foreign keys apply only to a type that is ``closed_by`` that selector.
    """
    required: list[str] = []
    foreign: set[str] = set()
    for selector, by_value in CONDITIONAL_REQUIRED.get(type_, {}).items():
        chosen = extras.get(selector)
        if not isinstance(chosen, str) or chosen not in by_value:
            continue
        required.extend(by_value[chosen])
        if GENERATE_OPTIONS[type_].closed_by == selector:
            foreign |= {k for keys in by_value.values() for k in keys if k not in by_value[chosen]}
    return tuple(required), frozenset(foreign)


def _given(key: str, extras: Mapping[str, Any]) -> bool:
    return key in extras and (extras[key] is not None or key not in _NONE_MEANS_ABSENT)


def option_problems(type_: str, extras: Mapping[str, Any]) -> list[Found]:
    """Every problem with a column's flat options, in a stable order, as (location, code).

    Reads ``extras`` and never changes it. A `None` value means "not given" only for the keys in
    ``_NONE_MEANS_ABSENT`` (their readers treat None as absent); a required key with None is
    missing, and any other key with None is a type error. Nothing here quotes a submitted value.
    """
    spec = GENERATE_OPTIONS[type_]
    found: list[Found] = []
    check_input = {**extras, "type": type_}
    family_required, foreign = _family_keys(type_, extras)
    for key, value in extras.items():
        if key in foreign:
            found.append(((key,), UNKNOWN))
            continue
        option = spec.keys.get(key) or COMMON_OPTIONS.get(key)
        if option is None:
            if key not in INTERNAL_KEYS:
                found.append(((key,), UNKNOWN))
            continue
        if key in spec.required and _missing(key, value):
            continue
        if key in family_required and value is None:
            continue
        if value is None:
            if key not in _NONE_MEANS_ABSENT:
                found.append(((key,), _TYPE))
            continue
        if option.check is None:
            continue
        found.extend(((key, *p[0]), *p[1:]) for p in option.check(value, check_input))
    given_missing = [k for k in spec.required if _missing(k, extras.get(k))]
    found.extend(((k,), "missing") for k in given_missing)
    found.extend(((k,), "missing") for k in family_required if extras.get(k) is None)
    given = [k for k in spec.one_of if extras.get(k) is not None]
    if spec.one_of and not given:
        found.append(((spec.one_of[0],), "missing"))
    found.extend(((k,), _VALUE) for k in given[1:])
    if any(_given(k, extras) for k in spec.together):
        found.extend(((k,), "missing") for k in spec.together if not _given(k, extras))
    return found


_Loc = tuple[str | int, ...]


def problem_details(
    problems: Sequence[tuple[_Loc, str] | tuple[_Loc, str, str]],
) -> list[InitErrorDetails]:
    """Pydantic line errors for ``problems``: fixed text per code, no input kept.

    A third item is value-free text (table and column names) added after the summary.
    """
    details: list[InitErrorDetails] = []
    for problem in problems:
        loc, code = problem[0], problem[1]
        kind: Any = "missing"
        if code != "missing":
            text = " ".join((reasons.lookup(code)[0].summary, *problem[2:]))
            kind = PydanticCustomError(code, text)
        details.append({"type": kind, "loc": loc, "input": None})
    return details


def refuse_unlisted_generate_families(config: Mapping[str, Any]) -> None:
    """Raise `PipelineValidationError` for the first `distribution` column with a listed-out family.

    `compile_plan` and `run_config_only_checks` call this beside their other config-only checks, so
    a raw dict that never went through `PipelineConfig` cannot reach a draw with a deferred family.
    It reads `generate_family_problems`, the function the validator's checker also backs. The
    message is the registry's fixed summary (no config value is quoted) and nothing is chained.
    """
    for path, code in generate_family_problems(config):
        summary = reasons.lookup(code)[0].summary
        if code == str(reasons.config.GENERATE_OPTION_TYPE):
            raise PipelineValidationError(
                summary, path=path, code=reasons.config.GENERATE_OPTION_TYPE
            )
        raise PipelineValidationError(
            summary, path=path, code=reasons.config.GENERATE_OPTION_VALUE_UNKNOWN
        )

"""Value-free config problems from a Pydantic ``ValidationError``.

Pydantic renders data into its errors in several places: the rendered ``msg`` (the union tag
echo, validator text built from the input), ``input`` and ``ctx``. This module never reads those.
It keeps what names the problem (the ``loc`` path, the error type) and writes the text from a fixed
table keyed by error type, interpolating only schema-derived ``ctx`` keys. Names (config keys,
table and column names, file names, patterns) are shown; data values, literals taken from data
and secrets are not (OWASP error handling: a generic message plus a stable code, never reflected
input; CWE-209).

Validator text is trusted only for the exact type ``ConfigRuleError``. A custom error is trusted
only through its registered reason code, whose fixed summary replaces the raise-site text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic_core import ValidationError

from decoy_engine import reasons
from decoy_engine.errors import ConfigRuleError

_GENERIC = "Value is not valid."

# Error types with no placeholder in the library template, or whose placeholders are all
# input-derived (the parser's own error text), so the row is fixed text.
_FIXED: dict[str, str] = {
    "no_such_attribute": "Object has no such attribute.",
    "json_invalid": "Invalid JSON.",
    "json_type": "JSON input should be string, bytes or bytearray",
    "needs_python_object": "Cannot check this method when validating from json",
    "recursion_loop": "Recursion error - cyclic reference detected",
    "missing": "Field required",
    "frozen_field": "Field is frozen",
    "frozen_instance": "Instance is frozen",
    "extra_forbidden": "Extra inputs are not permitted",
    "invalid_key": "Keys should be strings",
    "get_attribute_error": "Error extracting attribute.",
    "model_attributes_type": "Input should be a valid dictionary or object to extract fields from",
    "default_factory_not_called": "The default factory uses validated data, but a validation "
    "error occurred",
    "none_required": "Input should be None",
    "finite_number": "Input should be a finite number",
    "iterable_type": "Input should be iterable",
    "iteration_error": "Error iterating over object.",
    "string_type": "Input should be a valid string",
    "string_sub_type": "Input should be a string, not an instance of a subclass of str",
    "string_unicode": "Input should be a valid string, unable to parse raw data as unicode",
    "string_not_ascii": "String should contain only ASCII characters",
    "mapping_type": "Input should be a valid mapping",
    "dict_type": "Input should be a valid dictionary",
    "list_type": "Input should be a valid list",
    "tuple_type": "Input should be a valid tuple",
    "set_type": "Input should be a valid set",
    "set_item_not_hashable": "Set items should be hashable",
    "bool_type": "Input should be a valid boolean",
    "bool_parsing": "Input should be a valid boolean, unable to interpret input",
    "int_type": "Input should be a valid integer",
    "int_parsing": "Input should be a valid integer, unable to parse string as an integer",
    "int_parsing_size": "Unable to parse input string as an integer, exceeded maximum size",
    "int_from_float": "Input should be a valid integer, got a number with a fractional part",
    "float_type": "Input should be a valid number",
    "float_parsing": "Input should be a valid number, unable to parse string as a number",
    "bytes_type": "Input should be a valid bytes",
    "bytes_invalid_encoding": "Data should be valid in the expected encoding",
    "assertion_error": "Assertion failed.",
    "missing_sentinel_error": "Input should be the 'MISSING' sentinel",
    "date_type": "Input should be a valid date",
    "date_parsing": "Input should be a valid date in the format YYYY-MM-DD",
    "date_from_datetime_parsing": "Input should be a valid date or datetime",
    "date_from_datetime_inexact": "Datetimes provided to dates should have zero time",
    "date_past": "Date should be in the past",
    "date_future": "Date should be in the future",
    "time_type": "Input should be a valid time",
    "time_parsing": "Input should be in a valid time format",
    "datetime_type": "Input should be a valid datetime",
    "datetime_parsing": "Input should be a valid datetime",
    "datetime_object_invalid": "Invalid datetime object",
    "datetime_from_date_parsing": "Input should be a valid datetime or date",
    "datetime_past": "Input should be in the past",
    "datetime_future": "Input should be in the future",
    "timezone_naive": "Input should not have timezone info",
    "timezone_aware": "Input should have timezone info",
    "timezone_offset": "Timezone offset is not the required one",
    "time_delta_type": "Input should be a valid timedelta",
    "time_delta_parsing": "Input should be a valid timedelta",
    "frozen_set_type": "Input should be a valid frozenset",
    "callable_type": "Input should be callable",
    "arguments_type": "Arguments must be a tuple, list or a dictionary",
    "missing_argument": "Missing required argument",
    "unexpected_keyword_argument": "Unexpected keyword argument",
    "missing_keyword_only_argument": "Missing required keyword only argument",
    "unexpected_positional_argument": "Unexpected positional argument",
    "missing_positional_only_argument": "Missing required positional only argument",
    "multiple_argument_values": "Got multiple values for argument",
    "url_type": "URL input should be a string or URL",
    "url_parsing": "Input should be a valid URL",
    "url_syntax_violation": "Input violated strict URL syntax rules",
    "uuid_type": "UUID input should be a string, bytes or UUID object",
    "uuid_parsing": "Input should be a valid UUID",
    "decimal_type": "Decimal input should be an integer, float, string or Decimal object",
    "decimal_parsing": "Input should be a valid decimal",
    "complex_type": "Input should be a valid python complex object, a number, or a valid "
    "complex string",
    "complex_str_parsing": "Input should be a valid complex string",
}

# Error types whose template names schema-derived limits or expected sets. Each row lists the
# only ``ctx`` keys it may format. ``tag``, ``error``, ``input``, ``actual_length``,
# ``tz_actual`` and the other input-derived keys never appear here.
_FORMATTED: dict[str, tuple[str, tuple[str, ...]]] = {
    "model_type": (
        "Input should be a valid dictionary or instance of {class_name}",
        ("class_name",),
    ),
    "dataclass_type": (
        "Input should be a dictionary or an instance of {class_name}",
        ("class_name",),
    ),
    "dataclass_exact_type": ("Input should be an instance of {class_name}", ("class_name",)),
    "greater_than": ("Input should be greater than {gt}", ("gt",)),
    "greater_than_equal": ("Input should be greater than or equal to {ge}", ("ge",)),
    "less_than": ("Input should be less than {lt}", ("lt",)),
    "less_than_equal": ("Input should be less than or equal to {le}", ("le",)),
    "multiple_of": ("Input should be a multiple of {multiple_of}", ("multiple_of",)),
    "too_short": (
        "{field_type} should have at least {min_length} item{expected_plural}",
        ("field_type", "min_length"),
    ),
    "too_long": (
        "{field_type} should have at most {max_length} item{expected_plural}",
        ("field_type", "max_length"),
    ),
    "string_too_short": (
        "String should have at least {min_length} character{expected_plural}",
        ("min_length",),
    ),
    "string_too_long": (
        "String should have at most {max_length} character{expected_plural}",
        ("max_length",),
    ),
    "string_pattern_mismatch": ("String should match pattern '{pattern}'", ("pattern",)),
    "enum": ("Input should be {expected}", ("expected",)),
    "literal_error": ("Input should be {expected}", ("expected",)),
    "bytes_too_short": (
        "Data should have at least {min_length} byte{expected_plural}",
        ("min_length",),
    ),
    "bytes_too_long": (
        "Data should have at most {max_length} byte{expected_plural}",
        ("max_length",),
    ),
    "is_instance_of": ("Input should be an instance of {class}", ("class",)),
    "is_subclass_of": ("Input should be a subclass of {class}", ("class",)),
    "union_tag_invalid": (
        "Input tag does not match any of the expected tags: {expected_tags} "
        "(found using {discriminator})",
        ("expected_tags", "discriminator"),
    ),
    "union_tag_not_found": (
        "Unable to extract tag using discriminator {discriminator}",
        ("discriminator",),
    ),
    "url_too_long": (
        "URL should have at most {max_length} character{expected_plural}",
        ("max_length",),
    ),
    "url_scheme": ("URL scheme should be {expected_schemes}", ("expected_schemes",)),
    "uuid_version": ("UUID version {expected_version} expected", ("expected_version",)),
    "decimal_max_digits": (
        "Decimal input should have no more than {max_digits} digit{expected_plural} in total",
        ("max_digits",),
    ),
    "decimal_max_places": (
        "Decimal input should have no more than {decimal_places} decimal place{expected_plural}",
        ("decimal_places",),
    ),
    "decimal_whole_digits": (
        "Decimal input should have no more than {whole_digits} digit{expected_plural} "
        "before the decimal point",
        ("whole_digits",),
    ),
}

# ``value_error`` is the validator-text row: see ``_value_error_text``.
_VALUE_ERROR = "value_error"

POLICY_TYPES: frozenset[str] = frozenset(_FIXED) | frozenset(_FORMATTED) | {_VALUE_ERROR}


@dataclass(frozen=True)
class ConfigProblem:
    """One structural problem: a stable ``code``, where it is, and value-free ``text``."""

    code: str
    location: str
    text: str


def _location(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for seg in loc:
        if isinstance(seg, int):
            parts.append(f"[{seg}]")
        else:
            parts.append(("." if parts else "") + str(seg))
    return "".join(parts)


def _value_error_text(ctx: dict[str, Any]) -> str:
    err = ctx.get("error")
    if type(err) is ConfigRuleError:
        return err.message
    return _GENERIC


def _text_for(err_type: str, ctx: dict[str, Any]) -> str:
    if err_type == _VALUE_ERROR:
        return _value_error_text(ctx)
    if err_type in _FIXED:
        return _FIXED[err_type]
    row = _FORMATTED[err_type]
    template, keys = row
    try:
        values = {k: ctx[k] for k in keys}
    except KeyError:
        return _GENERIC
    # Pydantic derives the plural suffix while rendering and leaves it out of ``ctx``.
    count = next((v for v in values.values() if isinstance(v, int)), 1)
    return template.format(expected_plural="" if count == 1 else "s", **values)


def _problem(err: Mapping[str, Any]) -> ConfigProblem:
    err_type = str(err["type"])
    ctx = err.get("ctx") or {}
    location = _location(tuple(err["loc"]))
    if err_type in POLICY_TYPES:
        return ConfigProblem(err_type, location, _text_for(err_type, ctx))
    specs = reasons.lookup(err_type)
    if specs:
        return ConfigProblem(err_type, location, specs[0].summary)
    # An unregistered custom type may be author- or data-derived, so it reports as a plain value error.
    return ConfigProblem(_VALUE_ERROR, location, _GENERIC)


def validation_problems(exc: ValidationError) -> tuple[ConfigProblem, ...]:
    """The value-free problems in ``exc``, in Pydantic's order.

    ``location`` joins the original ``loc`` segments (``tables[0].name``). Nothing input-derived
    is read: not ``input``, not the rendered ``msg``, not ``ctx`` beyond the allowlisted keys.
    """
    return tuple(_problem(e) for e in exc.errors(include_url=False, include_input=False))


def yaml_error_text(exc: Exception) -> str:
    """A leak-safe one-line description of a YAML parse failure.

    PyYAML's message and snippet echo the offending source line, which can hold a password or
    PII. Only the integer line and column of the error mark are read. Accepts any exception so a
    broad ``except`` cannot pass a non-YAML error's text through.
    """
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    if isinstance(line, int) and isinstance(column, int):
        # PyYAML marks are 0-indexed; editors count from 1.
        return f"could not parse YAML: invalid syntax at line {line + 1}, column {column + 1}"
    return "could not parse YAML: the file is not valid YAML"

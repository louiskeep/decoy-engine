"""Catalog of the ``config`` namespace, part 2."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import UNRELEASED

__all__ = [
    "GENERATE_COLUMN_DUPLICATE",
    "GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT",
    "GENERATE_FORMULA_INVALID",
    "GENERATE_OPTION_RANGE",
    "GENERATE_OPTION_TYPE",
    "GENERATE_OPTION_UNKNOWN",
    "GENERATE_OPTION_VALUE_UNKNOWN",
    "GENERATE_OUTPUT_TYPE_UNSUPPORTED",
    "GENERATE_PER_PARENT_BOUNDS_CONFLICT",
    "GENERATE_PER_PARENT_INFEASIBLE",
    "GENERATE_PER_PARENT_NULL_CONFLICT",
    "GENERATE_SELF_REFERENCE_KEY_INVALID",
    "GENERATE_SELF_REFERENCE_OPTION_UNSUPPORTED",
    "GENERATE_UNIQUE_CONFLICT",
    "GENERATE_UNIQUE_DOMAIN_TOO_SMALL",
    "GENERATE_UNIQUE_UNSUPPORTED",
    "GENERATE_WEIGHTS_INVALID",
    "PIPELINE_NAME_INVALID",
    "VARIABLE_UNKNOWN_SCOPE",
    "VARIABLE_UNRESOLVED",
]

_d = _catalog.config("decoy_engine.validation_result")

VARIABLE_UNKNOWN_SCOPE = _d("variable.unknown_scope", "Variable unknown scope.")
VARIABLE_UNRESOLVED = _d("variable.unresolved", "Variable unresolved.")

# Summaries double as the refusal text the config layer shows, so they never name a value.
_d = _catalog.config("PipelineValidationError")

GENERATE_COLUMN_DUPLICATE = _d(
    "generate_column_duplicate",
    "Two generate columns in one table have the same name.",
    since="1.13.0",
)
GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT = _d(
    "generate_fitted_reference_bounds_conflict",
    "Generate reference column per-parent bounds cannot be combined with distribution fitted.",
    since=UNRELEASED,
)
GENERATE_FORMULA_INVALID = _d(
    "generate_formula_invalid",
    "Generate column formula is not a valid expression.",
    since="1.13.0",
)
GENERATE_OPTION_RANGE = _d(
    "generate_option_range",
    "Generate column option is outside its allowed range.",
    since="1.13.0",
)
GENERATE_OPTION_TYPE = _d(
    "generate_option_type",
    "Generate column option has the wrong type.",
    since="1.13.0",
)
GENERATE_OPTION_UNKNOWN = _d(
    "generate_option_unknown",
    "Generate column option is not known for this column type.",
    since="1.13.0",
)
GENERATE_OPTION_VALUE_UNKNOWN = _d(
    "generate_option_value_unknown",
    "Generate column option value is not one of the allowed choices.",
    since="1.13.0",
)
GENERATE_OUTPUT_TYPE_UNSUPPORTED = _d(
    "generate_output_type_unsupported",
    "Generate column type or target cannot carry the requested output type.",
    since="1.23.0",
)
GENERATE_PER_PARENT_BOUNDS_CONFLICT = _d(
    "generate_per_parent_bounds_conflict",
    "Generate reference column min_per_parent is greater than its max_per_parent.",
    since=UNRELEASED,
)
GENERATE_PER_PARENT_INFEASIBLE = _d(
    "generate_per_parent_infeasible",
    "Generate reference column per-parent bounds cannot hold for its parent keys and row count.",
    since=UNRELEASED,
)
GENERATE_PER_PARENT_NULL_CONFLICT = _d(
    "generate_per_parent_null_conflict",
    "Generate column null_probability cannot be combined with a per-parent option.",
    since=UNRELEASED,
)
GENERATE_SELF_REFERENCE_KEY_INVALID = _d(
    "generate_self_reference_key_invalid",
    "Generate self-reference column must name an earlier sequence column of its own table with "
    "no null_probability.",
    since=UNRELEASED,
)
GENERATE_SELF_REFERENCE_OPTION_UNSUPPORTED = _d(
    "generate_self_reference_option_unsupported",
    "Generate self-reference column takes distribution random and an optional max_per_parent only.",
    since=UNRELEASED,
)
GENERATE_UNIQUE_CONFLICT = _d(
    "generate_unique_conflict",
    "Generate column unique cannot be combined with another option on the same column.",
    since="1.17.0",
)
GENERATE_UNIQUE_DOMAIN_TOO_SMALL = _d(
    "generate_unique_domain_too_small",
    "Generate column unique needs more distinct values than its domain holds.",
    since="1.17.0",
)
GENERATE_UNIQUE_UNSUPPORTED = _d(
    "generate_unique_unsupported",
    "Generate column type does not support unique.",
    since="1.17.0",
)
GENERATE_WEIGHTS_INVALID = _d(
    "generate_weights_invalid",
    "Generate column weights must be non-negative numbers with a positive total.",
    since="1.13.0",
)
PIPELINE_NAME_INVALID = _d(
    "pipeline_name_invalid",
    "Pipeline name must be 1 to 64 letters, digits, dot, dash or underscore, start with a letter or "
    "digit, not end in a dot and not be a Windows device name.",
    since="1.12.0",
)

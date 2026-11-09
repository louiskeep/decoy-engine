"""Catalog of the ``config`` namespace, part 2."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = ["PIPELINE_NAME_INVALID", "VARIABLE_UNKNOWN_SCOPE", "VARIABLE_UNRESOLVED"]

_d = _catalog.config("decoy_engine.validation_result")

VARIABLE_UNKNOWN_SCOPE = _d("variable.unknown_scope", "Variable unknown scope.")
VARIABLE_UNRESOLVED = _d("variable.unresolved", "Variable unresolved.")

_d = _catalog.config("PipelineValidationError")

PIPELINE_NAME_INVALID = _d(
    "pipeline_name_invalid",
    "Pipeline name must be 1 to 64 letters, digits, dot, dash or underscore, start with a letter or "
    "digit, not end in a dot and not be a Windows device name.",
    since="1.12.0",
)

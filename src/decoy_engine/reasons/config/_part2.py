"""Catalog of the ``config`` namespace, part 2."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = ["VARIABLE_UNKNOWN_SCOPE", "VARIABLE_UNRESOLVED"]

_d = _catalog.config("decoy_engine.validation_result")

VARIABLE_UNKNOWN_SCOPE = _d("variable.unknown_scope", "Variable unknown scope.")
VARIABLE_UNRESOLVED = _d("variable.unresolved", "Variable unresolved.")

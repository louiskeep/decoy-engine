"""Edge-case test data providers: curated, type-valid awkward values for QA.

Six providers, one output type each: ``edge_case_string`` (ASCII str), ``edge_case_unicode``
(str), ``edge_case_integer`` (int64), ``edge_case_float`` (finite float), ``edge_case_date``
(date) and ``edge_case_datetime`` (UTC-aware datetime). They are reached by name through the
default provider registry with ``strategy: faker``; nothing names them by default.
"""

from __future__ import annotations

from decoy_engine.providers_v2.edge_cases._adapter import EdgeCaseAdapter, capability_row
from decoy_engine.providers_v2.edge_cases._spec import EdgeCase, EdgeCaseSpec
from decoy_engine.providers_v2.edge_cases._text_cases import STRING_SPEC, UNICODE_SPEC
from decoy_engine.providers_v2.edge_cases._value_cases import (
    DATE_SPEC,
    DATETIME_SPEC,
    FLOAT_SPEC,
    INTEGER_SPEC,
)

EDGE_CASE_SPECS: tuple[EdgeCaseSpec, ...] = (
    STRING_SPEC,
    UNICODE_SPEC,
    INTEGER_SPEC,
    FLOAT_SPEC,
    DATE_SPEC,
    DATETIME_SPEC,
)

__all__ = [
    "EDGE_CASE_SPECS",
    "EdgeCase",
    "EdgeCaseAdapter",
    "EdgeCaseSpec",
    "capability_row",
]

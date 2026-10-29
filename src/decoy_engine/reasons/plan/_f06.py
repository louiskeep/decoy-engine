"""Catalog of the ``plan`` namespace: F06 declared parsing, casting and quarantine contracts."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Phase

__all__ = [
    "DECLARED_CAST_CONTRACT_INVALID",
    "DECLARED_CAST_CURRENCY_CODE_INVALID",
    "DECLARED_CAST_DIRECTIVE_UNSUPPORTED",
    "DECLARED_CAST_FORMAT_UNKNOWN",
    "DECLARED_CAST_PRECISION_INVALID",
    "DECLARED_CAST_SCALE_INVALID",
    "DECLARED_CAST_SENTINEL_CONFIG_INVALID",
    "DECLARED_OUTPUT_WIDTH_UNKNOWN",
    "DECLARED_TYPE_STRATEGY_INCOMPATIBLE",
]

_d = _catalog.plan("decoy_engine.declared_casting")
# The same codes reject a config at validation time and a plan at compile time.
_BOTH = (Phase.CONFIGURATION, Phase.COMPILE)

DECLARED_CAST_CONTRACT_INVALID = _d(
    "declared_cast_contract_invalid",
    "A declared parsing and casting contract is inconsistent.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_CAST_PRECISION_INVALID = _d(
    "declared_cast_precision_invalid",
    "A declared decimal precision is outside the supported range.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_CAST_SCALE_INVALID = _d(
    "declared_cast_scale_invalid",
    "A declared decimal scale is outside the supported range.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_CAST_SENTINEL_CONFIG_INVALID = _d(
    "declared_cast_sentinel_config_invalid",
    "A declared sentinel set is empty, duplicated, oversized or of the wrong kind.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_CAST_CURRENCY_CODE_INVALID = _d(
    "declared_cast_currency_code_invalid",
    "A declared currency code is missing or malformed.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_CAST_FORMAT_UNKNOWN = _d(
    "declared_cast_format_unknown",
    "A declared format identifier is unknown or retired.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_CAST_DIRECTIVE_UNSUPPORTED = _d(
    "declared_cast_directive_unsupported",
    "A declared format directive is outside the supported catalog.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_OUTPUT_WIDTH_UNKNOWN = _d(
    "declared_output_width_unknown",
    "A declared output width cannot be proved before execution.",
    phases=_BOTH,
    since="1.1.0",
)
DECLARED_TYPE_STRATEGY_INCOMPATIBLE = _d(
    "declared_type_strategy_incompatible",
    "A declared column type is incompatible with its strategy or output format.",
    phases=_BOTH,
    since="1.1.0",
)

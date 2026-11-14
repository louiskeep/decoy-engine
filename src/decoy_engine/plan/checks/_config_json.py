"""The compile-time refusal of a config value JSON cannot hold.

`pipeline_config_hash` is the first place a compile hashes the config, so it is where a YAML
date or timestamp, NaN, Infinity or an unpaired surrogate is stopped. Pydantic keeps date objects
in provider config and extra column fields, so they reach here as `datetime.date` and
`datetime.datetime`. The canonical encoder would turn a date or an aware timestamp into text, but
a config value that silently changes type is the surprise this refusal exists to prevent: the user
must quote it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from decoy_engine import reasons
from decoy_engine._canonical import CanonicalJsonError, canonical_json_bytes
from decoy_engine.plan._errors import PlanCompileError

__all__ = ["canonical_config_bytes"]


def _temporal_type(value: Any) -> str | None:
    """The type name of the first date or datetime in ``value``, or ``None``."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dt.date):
            return type(item).__name__
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return None


def canonical_config_bytes(semantic_config: dict[str, Any]) -> bytes:
    """The canonical bytes of ``semantic_config``, or a coded, value-free `PlanCompileError`."""
    type_name = _temporal_type(semantic_config)
    if type_name is None:
        try:
            return canonical_json_bytes(semantic_config)
        except CanonicalJsonError as error:
            type_name = error.type_name
    # Raised after the handler closed, so no exception context holds the value.
    message = reasons.REASONS[("config", reasons.config.CONFIG_VALUE_NOT_JSON)].summary
    if type_name:
        message = f"{message} Found type: {type_name}."
    raise PlanCompileError(code=reasons.config.CONFIG_VALUE_NOT_JSON, path=None, message=message)

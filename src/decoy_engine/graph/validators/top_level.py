"""Top-level graph config validation (V2.0-B stage 1).

Validates the shape that every later stage assumes:
  - mode is "graph"
  - nodes is a non-empty list
  - edges, if present, is a list
  - schema_version is a supported int
  - engine, if present, is "pandas" or "hybrid"

Raises ValidationError on the first failure -- top-level shape is the
gate for safely walking nodes/edges, so collecting-mode does not apply
here.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.graph.validators._shared import SUPPORTED_SCHEMA_VERSIONS
from decoy_engine.internal.validator import ValidationError
from decoy_engine.validation_result import CODES


def known_kinds() -> set[str]:
    """All registered op kinds. Lifted out of GraphConfigValidator so the
    modular validators can pass kinds explicitly to per-stage functions
    instead of bundling them onto a class instance.
    """
    from decoy_engine.graph.ops import OPS

    return set(OPS.keys())


def validate_top_level(config: dict[str, Any]) -> None:
    mode = config.get("mode")
    if mode != "graph":
        raise ValidationError(
            f"top-level 'mode' must be 'graph' (got {mode!r})",
            "mode",
            code=CODES.TOP_LEVEL_BAD_MODE,
        )
    if not isinstance(config.get("nodes"), list) or not config["nodes"]:
        raise ValidationError(
            "'nodes' must be a non-empty list",
            "nodes",
            code=CODES.NODES_EMPTY_LIST,
        )
    if "edges" in config and not isinstance(config["edges"], list):
        raise ValidationError(
            "'edges' must be a list",
            "edges",
            code=CODES.EDGES_BAD_TYPE,
        )

    sv = config.get("schema_version", 1)
    if not isinstance(sv, int) or sv not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValidationError(
            f"unsupported schema_version {sv!r} (supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})",
            "schema_version",
            code=CODES.TOP_LEVEL_BAD_SCHEMA_VERSION,
        )

    engine = config.get("engine", "pandas")
    if engine not in ("pandas", "hybrid"):
        raise ValidationError(
            f"'engine' must be 'pandas' or 'hybrid' (got {engine!r})",
            "engine",
            code=CODES.TOP_LEVEL_BAD_ENGINE,
        )

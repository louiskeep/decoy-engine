"""YAML-string -> normalized dict loaders + top-level validators.

Extracted from decoy_engine/graph/runner.py per the Code Organization
Migration Plan §Section 6. Move-only.

Three small helpers that sit at the entry boundary of every graph
runner / validator / preview call:

  - ``_load_yaml``: parse a YAML string into a dict; raises
    ``ConfigError`` with a clean message on bad input.
  - ``_validate_or_raise``: run ``GraphConfigValidator`` and re-raise
    its ``ValidationError`` as ``PipelineValidationError`` so the
    public API exposes one error type.
  - ``_validate_top_level_or_raise``: cheap structural check (mode +
    nodes + edges shapes) used before the deeper validator so common
    typos surface fast.

These are reimported into runner.py and exposed under their old
names — callers ``from decoy_engine.graph.runner import _load_yaml``
keep working unchanged.
"""

from __future__ import annotations

import logging

import yaml

from decoy_engine.exceptions import ConfigError, PipelineValidationError
from decoy_engine.internal.validator import GraphConfigValidator, ValidationError


def _load_yaml(yaml_text: str) -> dict:
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("graph config root must be a mapping")
    return data


def _validate_or_raise(config: dict) -> None:
    quiet = logging.getLogger("decoy_engine.graph.runner")
    if not quiet.handlers:
        quiet.addHandler(logging.NullHandler())
    try:
        GraphConfigValidator(quiet).validate(config)
    except ValidationError as e:
        raise PipelineValidationError(str(e), path=e.path) from e


def _validate_top_level_or_raise(config: dict) -> None:
    if config.get("mode") != "graph":
        raise PipelineValidationError(
            f"top-level 'mode' must be 'graph' (got {config.get('mode')!r})"
        )
    nodes = config.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise PipelineValidationError("'nodes' must be a non-empty list")
    edges = config.get("edges")
    if edges is not None and not isinstance(edges, list):
        raise PipelineValidationError("'edges' must be a list")

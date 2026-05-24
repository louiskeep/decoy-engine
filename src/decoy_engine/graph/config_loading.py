"""YAML-string -> normalized dict loaders + top-level validators.

Extracted from decoy_engine/graph/runner.py per the Code Organization
Migration Plan §Section 6. Move-only.

Three small helpers that sit at the entry boundary of every graph
runner / validator / preview call:

  - ``_load_yaml``: parse a YAML string into a dict; raises
    ``ConfigError`` with a clean message on bad input.
  - ``_validate_or_raise``: run the modular validator stages and
    re-raise the first ``ValidationError`` as
    ``PipelineValidationError`` so the public API exposes one
    error type.
  - ``_validate_top_level_or_raise``: cheap structural check (mode +
    nodes + edges shapes) used before the deeper validator so common
    typos surface fast.

These are reimported into runner.py and exposed under their old
names — callers ``from decoy_engine.graph.runner import _load_yaml``
keep working unchanged.
"""

from __future__ import annotations

import yaml

from decoy_engine.errors import ConfigError, PipelineValidationError, ValidationError
from decoy_engine.graph.validators import (
    known_kinds,
    validate_acyclic,
    validate_cardinality,
    validate_edges,
    validate_file_format_consistency,
    validate_mask_column_reachability,
    validate_nodes,
    validate_nodes_ref_reachability,
    validate_top_level,
)


def _load_yaml(yaml_text: str) -> dict:
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("graph config root must be a mapping")
    return data


def _validate_or_raise(config: dict) -> None:
    try:
        kinds = known_kinds()
        validate_top_level(config)
        nodes = config["nodes"]
        edges = config.get("edges") or []
        validate_nodes(nodes, kinds)
        validate_edges(edges, nodes)
        validate_cardinality(nodes, edges, kinds)
        validate_acyclic(nodes, edges)
        validate_file_format_consistency(nodes, edges, logger=None)
        validate_mask_column_reachability(nodes, edges)
        validate_nodes_ref_reachability(nodes, edges)
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

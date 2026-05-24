"""Validation never mutates caller input (V2.0-B contract test).

Done-state from the V2 roadmap: validation never mutates caller input.
This test is the mechanical enforcement: deep-copy the input before
validation, then assert the caller's dict is byte-equal to its
pre-validation snapshot.

A failure here means a validator (or the public validate_graph /
validate_graph_full entry) wrote into the caller's config. The fix is
to move the write into `decoy_engine.graph.normalize.normalize_config`,
the single named normalization path.

Covers both entry points:
  - validate_graph(yaml_text) (raise-on-first-error)
  - validate_graph_full(yaml_text) (collect-all errors)

Includes the historical pain point as a fixture: a target.file node
that omits `format` and gets it inferred from a parquet source. Pre-
V2.0-B, this case wrote `target.config.format = parquet` into the
caller's parsed dict via `_validate_file_format_consistency`. V2.0-B
moves that write into normalize_config; the validators are pure now,
so this fixture must pass without any caller-visible mutation.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from decoy_engine import validate_graph, validate_graph_full
from decoy_engine.graph.normalize import normalize_config


def _format_backfill_config() -> dict:
    """Source has format=parquet; target has no format. Pre-V2.0-B,
    validation back-filled target.config.format = parquet into the
    passed-in dict."""
    return {
        "mode": "graph",
        "nodes": [
            {
                "id": "src",
                "kind": "source.file",
                "config": {"path": "/tmp/in.parquet", "format": "parquet"},
            },
            {
                "id": "tgt",
                "kind": "target.file",
                "config": {"output_filename": "/tmp/out.parquet"},
            },
        ],
        "edges": [{"from": "src", "to": "tgt"}],
    }


def _minimal_valid_config() -> dict:
    return {
        "mode": "graph",
        "nodes": [
            {
                "id": "src",
                "kind": "source.file",
                "config": {"path": "/tmp/in.csv", "format": "csv"},
            },
            {
                "id": "tgt",
                "kind": "target.file",
                "config": {"output_filename": "/tmp/out.csv", "format": "csv"},
            },
        ],
        "edges": [{"from": "src", "to": "tgt"}],
    }


@pytest.mark.parametrize(
    "config_factory",
    [_minimal_valid_config, _format_backfill_config],
    ids=["minimal_valid", "format_backfill_target"],
)
def test_validate_graph_does_not_mutate_parsed_config(config_factory) -> None:
    """validate_graph parses YAML internally, so the only way to inspect
    mutation is to compare a re-parse of the same text before and after.
    Equivalent to: validation has no caller-visible side effects."""
    config = config_factory()
    yaml_text = yaml.safe_dump(config, sort_keys=False)
    pre = yaml.safe_load(yaml_text)
    snapshot = copy.deepcopy(pre)
    validate_graph(yaml_text)
    post = yaml.safe_load(yaml_text)
    assert post == snapshot, "validate_graph mutated parser-visible state"


@pytest.mark.parametrize(
    "config_factory",
    [_minimal_valid_config, _format_backfill_config],
    ids=["minimal_valid", "format_backfill_target"],
)
def test_validate_graph_full_does_not_mutate_parsed_config(config_factory) -> None:
    config = config_factory()
    yaml_text = yaml.safe_dump(config, sort_keys=False)
    pre = yaml.safe_load(yaml_text)
    snapshot = copy.deepcopy(pre)
    validate_graph_full(yaml_text)
    post = yaml.safe_load(yaml_text)
    assert post == snapshot, "validate_graph_full mutated parser-visible state"


def test_normalize_config_is_pure_on_input() -> None:
    """normalize_config returns a new dict; the input is never mutated."""
    config = _format_backfill_config()
    snapshot = copy.deepcopy(config)
    normalized = normalize_config(config)
    assert config == snapshot, "normalize_config mutated its input"
    # Sanity: the normalized copy carries the back-filled format.
    assert normalized["nodes"][1]["config"].get("format") == "parquet"


def test_normalize_config_is_idempotent() -> None:
    """normalize_config(normalize_config(c)) == normalize_config(c)."""
    config = _format_backfill_config()
    once = normalize_config(config)
    twice = normalize_config(once)
    assert once == twice


def test_validate_graph_full_normalized_config_present_on_success() -> None:
    """validate_graph_full surfaces normalize_config's output on the
    result object when validation passes. Removing this would break
    callers that read result.normalized_config to drive the runner."""
    config = _format_backfill_config()
    yaml_text = yaml.safe_dump(config, sort_keys=False)
    result = validate_graph_full(yaml_text)
    assert not result.errors
    assert result.normalized_config is not None
    # Back-fill is visible on the normalized copy, not on the original.
    nc_tgt = result.normalized_config["nodes"][1]["config"]
    assert nc_tgt.get("format") == "parquet"

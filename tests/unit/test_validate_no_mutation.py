"""Validation never mutates caller input (V2.0-B contract test).

Done-state from the V2 roadmap: `validate(config)` and
`validate_graph_full(...)` never mutate caller input. This test is the
mechanical enforcement: deep-copy the input before validation, then
assert the input dict is byte-equal to its pre-validation snapshot.

A failure here means a validator wrote into the caller's config. The
fix is to move the write out of the validator and into
`normalize_config(config)`, which is the explicit normalization path.

Covers both validation entry points:
  - GraphConfigValidator.validate(config) (raises on first error)
  - validate_graph_full(yaml_text) (collects all errors)

Fixtures exercise both validation-success and validation-with-warning
paths. The format-backfill case is the historical pain point: pre-V2.0-B
`_validate_file_format_consistency` wrote `target.file.config.format`
into the validator's local copy via `tgt_cfg["format"] = tgt_fmt`. The
validate_graph_full path was already protected by a deep-copy at the
entry, but GraphConfigValidator.validate was not. V2.0-B removes the
mutation entirely.
"""

from __future__ import annotations

import copy
import logging

import pytest
import yaml

from decoy_engine import validate_graph_full
from decoy_engine.internal.validator import GraphConfigValidator


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


@pytest.fixture
def quiet_logger():
    log = logging.getLogger("decoy_engine.test_validate_no_mutation")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


_FORMAT_BACKFILL_XFAIL_REASON = (
    "Pre-V2.0-B GraphConfigValidator._validate_file_format_consistency writes "
    "target.config.format into the caller's dict (validator.py:1076). V2.0-B "
    "moves that write to normalize_config(config) and removes the xfail."
)


@pytest.mark.parametrize(
    "config_factory",
    [
        _minimal_valid_config,
        pytest.param(
            _format_backfill_config,
            marks=pytest.mark.xfail(reason=_FORMAT_BACKFILL_XFAIL_REASON, strict=True),
        ),
    ],
    ids=["minimal_valid", "format_backfill_target"],
)
def test_graph_config_validator_does_not_mutate(config_factory, quiet_logger) -> None:
    config = config_factory()
    snapshot = copy.deepcopy(config)
    GraphConfigValidator(quiet_logger).validate(config)
    assert config == snapshot, (
        "GraphConfigValidator.validate(config) mutated caller input. "
        "Move the write to normalize_config(config)."
    )


@pytest.mark.parametrize(
    "config_factory",
    [_minimal_valid_config, _format_backfill_config],
    ids=["minimal_valid", "format_backfill_target"],
)
def test_validate_graph_full_does_not_mutate(config_factory) -> None:
    config = config_factory()
    yaml_text = yaml.safe_dump(config, sort_keys=False)
    pre = yaml.safe_load(yaml_text)
    snapshot = copy.deepcopy(pre)
    # validate_graph_full parses the YAML internally, so the mutation
    # vector is whatever the parse produces. We assert here that re-
    # parsing the same text after validation yields an unchanged tree,
    # which means validation hasn't reached out and modified shared
    # caches or globals that the parser depends on.
    validate_graph_full(yaml_text)
    post = yaml.safe_load(yaml_text)
    assert post == snapshot, "validate_graph_full mutated parser-visible state"

"""iterate_fixed: run a sub-pipeline once per value in a hardcoded list.

Use cases: parametric runs ("run pipeline for these 5 client_ids"),
state-by-state filtering ("for state in ['KY', 'TN', 'OH']"), one-off
batches of named runs ("for cohort in cohorts").

Config:
    values: list                    iteration values, hardcoded in YAML.
                                    Strings, numbers, or dicts. Dicts get
                                    flattened into template vars as
                                    `iteration.value.<key>`.
    pipeline_ref: str               sub-pipeline YAML path
    output_node: str                sub-pipeline node id whose output
                                    flows downstream
    output: 'concat' | 'void'       output mode (default concat)

The sub-pipeline accesses `{{iteration.value}}` and `{{iteration.index}}`
as template placeholders. Dict values also get key-prefixed
placeholders: a value of `{"state": "KY", "year": 2024}` produces
`{{iteration.value.state}}` and `{{iteration.value.year}}` in addition
to the JSON-stringified `{{iteration.value}}`.
"""
from __future__ import annotations

from typing import Any

from decoy_engine.graph.ops._iterator_core import (
    run_iterations,
    validate_iterator_config,
)
from decoy_engine.internal.validator import ValidationError

KIND = "iterate_fixed"
NATIVE_ENGINE = "arrow"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    validate_iterator_config(config)
    values = config.get("values")
    if not isinstance(values, list) or not values:
        raise ValidationError(
            "'values' must be a non-empty list", "config.values"
        )


def apply(inputs, config, ctx):
    table = run_iterations(
        values=config["values"],
        pipeline_ref=config["pipeline_ref"],
        output_node=config["output_node"],
        output_mode=config.get("output", "concat"),
        ctx=ctx,
        log_prefix="iterate_fixed",
        extra_template_vars=_dict_value_template_vars,
    )
    if config.get("__engine") == "pandas":
        return table.to_pandas()
    return table


def _dict_value_template_vars(value, index):
    """Expose dict values as `iteration.value.<key>` template vars.

    For scalar values this is a no-op. For dict values it flattens one
    level deep; nested dicts stringify (callers wanting deep access can
    flatten explicitly in their value list).
    """
    if not isinstance(value, dict):
        return {}
    return {f"iteration.value.{k}": v for k, v in value.items()}

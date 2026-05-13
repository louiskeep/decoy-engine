"""iterate_loop: run a sub-pipeline once per index in a numeric range.

Use case: backfills ("run for each day in the last 30 days"), batch
processing ("for batch in 1..100"), parametric numeric sweeps.

Config:
    start: int                      first iteration value (inclusive)
    end: int                        last iteration value (exclusive)
    step: int = 1                   increment between iterations; must
                                    be non-zero. Negative steps iterate
                                    downward (start > end required).
    pipeline_ref: str               sub-pipeline YAML path
    output_node: str                sub-pipeline node id whose output
                                    flows downstream
    output: 'concat' | 'void'       output mode (default concat)

The sub-pipeline accesses `{{iteration.value}}` (the integer) and
`{{iteration.index}}` (the 0-based count, distinct from `value` when
start != 0).
"""
from __future__ import annotations

from typing import Any

from decoy_engine.graph.ops._iterator_core import (
    run_iterations,
    validate_iterator_config,
)
from decoy_engine.internal.validator import ValidationError

KIND = "iterate_loop"
NATIVE_ENGINE = "arrow"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    validate_iterator_config(config)
    for field in ("start", "end"):
        if field not in config:
            raise ValidationError(
                f"missing required field {field!r}", f"config.{field}"
            )
        if not isinstance(config[field], int) or isinstance(config[field], bool):
            raise ValidationError(
                f"{field!r} must be an int (got {type(config[field]).__name__})",
                f"config.{field}",
            )
    step = config.get("step", 1)
    if not isinstance(step, int) or isinstance(step, bool) or step == 0:
        raise ValidationError(
            "'step' must be a non-zero int", "config.step"
        )
    start, end = config["start"], config["end"]
    if step > 0 and start >= end:
        raise ValidationError(
            f"with positive step, start ({start}) must be < end ({end})",
            "config.start",
        )
    if step < 0 and start <= end:
        raise ValidationError(
            f"with negative step, start ({start}) must be > end ({end})",
            "config.start",
        )


def apply(inputs, config, ctx):
    table = run_iterations(
        values=range(config["start"], config["end"], config.get("step", 1)),
        pipeline_ref=config["pipeline_ref"],
        output_node=config["output_node"],
        output_mode=config.get("output", "concat"),
        ctx=ctx,
        log_prefix="iterate_loop",
    )
    if config.get("__engine") == "pandas":
        return table.to_pandas()
    return table

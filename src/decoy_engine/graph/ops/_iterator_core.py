"""Shared iteration scaffolding for `iterate_files`, `iterate_fixed`,
`iterate_loop`.

Each iterator op is a thin wrapper that produces the list of iteration
values its own way (file listings, hardcoded values, numeric range);
the actual loop body, variable injection, sub-pipeline invocation,
output concat, and error handling all live here.

Why factor it out: keeps the three iterator-op modules under ~50 lines
each (each one just produces its values), and prevents the loop
machinery from drifting between modes. Adding a fourth iterator type
later (GRID, TABLE) means writing a new "produce the values" function
and calling `run_iterations`; the loop body stays identical.

Output handling:
* `output: concat` (default) - returns a pa.Table with all sub-pipeline
  outputs vstacked in input order. Sub-pipelines with mismatched schemas
  raise OpError.
* `output: void` - returns an empty pa.Table. Used when the sub-pipeline
  has its own sink and the iterator runs purely for side effect.

Error handling: fail_fast only in v1. The first sub-pipeline that errors
stops the iteration and raises OpError; remaining iterations do not run.
Adding `continue_on_error` and `aggregate` modes is a v1.1 follow-up
once a customer actually has a use case for partial completion.

Parallelism: serial v1. Threadpool / process-pool are v1.1.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

import pyarrow as pa

from decoy_engine.context import ExecutionContext
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


def run_iterations(
    values: Iterable[Any],
    pipeline_ref: str,
    output_node: str,
    output_mode: str,
    ctx: ExecutionContext | None,
    log_prefix: str = "iterator",
    extra_template_vars: Callable[[Any, int], dict] | None = None,
) -> pa.Table:
    """Execute one sub_pipeline run per value; return concat or empty table.

    Parameters
    ----------
    values
        Iterable of iteration values. Materialized eagerly to a list so
        we can report total count and preserve order for concat.
    pipeline_ref
        Path to the sub-pipeline YAML. Same shape `sub_pipeline.apply()`
        expects.
    output_node
        Node id within the sub-pipeline whose output flows back to the
        iterator's parent graph (when output_mode='concat').
    output_mode
        'concat' or 'void'.
    ctx
        ExecutionContext passed to each sub-pipeline run.
    log_prefix
        Tag for log lines, e.g. 'iterate_files'. Helps trace per-mode
        iteration in mixed pipelines.
    extra_template_vars
        Optional callable `(value, index) -> dict` that produces
        additional template variables for this iteration on top of the
        standard `iteration.value` / `iteration.index`. Iterator modes
        with structured values (a FileMeta, a dict, etc.) use this to
        expose richer fields like `iteration.value.path`.

    Returns
    -------
    pa.Table
        Concatenated sub-pipeline outputs (output_mode='concat') or an
        empty 0-row table (output_mode='void').
    """
    materialized = list(values)
    log = ctx.logger if ctx is not None and ctx.logger is not None else None

    # Import here, not at module top: sub_pipeline.py imports
    # graph.runner which imports graph.ops (us). Top-level import would
    # cycle.
    from decoy_engine.graph.ops.sub_pipeline import apply as sub_pipeline_apply

    outputs: list[pa.Table] = []
    for index, value in enumerate(materialized):
        if log is not None:
            log.info(
                "%s: iteration %d/%d value=%r",
                log_prefix,
                index + 1,
                len(materialized),
                value,
            )

        template_vars: dict[str, Any] = {
            "iteration.value": value,
            "iteration.index": index,
        }
        if extra_template_vars is not None:
            template_vars.update(extra_template_vars(value, index))

        sub_config = {
            "pipeline_ref": pipeline_ref,
            "output_node": output_node,
            "template_vars": template_vars,
        }

        try:
            result_table = sub_pipeline_apply([], sub_config, ctx)
        except OpError as exc:
            raise OpError(
                f"{log_prefix}: iteration {index} (value={value!r}) failed: "
                f"{exc}"
            ) from exc

        if output_mode == "concat":
            if not isinstance(result_table, pa.Table):
                raise OpError(
                    f"{log_prefix}: sub_pipeline did not return a pyarrow "
                    f"Table on iteration {index} (got {type(result_table)})"
                )
            outputs.append(result_table)

    if output_mode == "void":
        # Return an empty table with no columns. Downstream nodes that
        # consume an iterator with output:void should not exist in
        # practice; configured this way to keep the runner's
        # arrow_row_count() reasonable for telemetry.
        return pa.table({})

    if not outputs:
        return pa.table({})

    # pa.concat_tables preserves column order and enforces type
    # compatibility; mismatched schemas raise pyarrow.ArrowInvalid which
    # we surface as OpError.
    try:
        return pa.concat_tables(outputs)
    except Exception as exc:
        raise OpError(
            f"{log_prefix}: cannot concat iteration outputs "
            f"(schemas may differ across iterations): {exc}"
        ) from exc


def validate_iterator_config(config: dict[str, Any]) -> None:
    """Shared field validation: pipeline_ref, output_node, output mode.

    Each iterator op calls this first, then validates its mode-specific
    fields (values, range, source, etc.).
    """
    ref = config.get("pipeline_ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ValidationError(
            "'pipeline_ref' must be a non-empty string", "config.pipeline_ref"
        )
    output_node = config.get("output_node")
    if not isinstance(output_node, str) or not output_node.strip():
        raise ValidationError(
            "'output_node' must be a non-empty string", "config.output_node"
        )
    output_mode = config.get("output", "concat")
    if output_mode not in {"concat", "void"}:
        raise ValidationError(
            f"'output' must be 'concat' or 'void' (got {output_mode!r})",
            "config.output",
        )

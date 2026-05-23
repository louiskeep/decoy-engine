"""sub_pipeline: run another pipeline graph as a single node.

Use case: you have a normalization sequence (drop nulls, fix encoding,
trim whitespace) used by five different masking pipelines. Without
sub_pipeline you copy-paste it into each. With sub_pipeline you write
it once, reference it by path, and any change propagates everywhere.

Config:
    pipeline_ref: str        path to a YAML file containing the sub-graph
    output_node: str         id of the node in the sub-graph whose output
                             flows downstream from this node
    template_vars: dict      optional; key/value pairs substituted into
                             the sub-graph YAML before execution (uses
                             `{{key}}` placeholders). Iterator ops use this
                             to inject `iteration.value` and
                             `iteration.index`.

Input arity: (0, 0). sub_pipeline does not accept upstream inputs in
v1; it is a starting node within the parent graph. Iterator ops handle
the "feed values into each iteration" pattern by injecting them as
template vars rather than as graph inputs. Adding upstream-input passing
requires a way to bind upstream Tables to nodes inside the sub-graph;
that is a v2 enhancement once a customer actually needs it.

Output: the pyarrow.Table from the configured `output_node` of the
sub-pipeline. The runner caches it just like any other op's output.
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "sub_pipeline"
NATIVE_ENGINE = "arrow"  # sub-graph output already crosses the Arrow boundary
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


# Cap on sub_pipeline call depth. Prevents A->B->A->B->... infinite
# recursion from hanging a worker. 32 is far above any legitimate
# orchestration depth; the most reasonable use case (workflow chain of
# composed library transforms) is rarely > 4 deep.
#
# Tracked via contextvar so each thread / asyncio task that the runner
# executes against has its own counter without inter-job contamination.
MAX_SUB_PIPELINE_DEPTH = 32

_sub_pipeline_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_decoy_sub_pipeline_depth", default=0
)


def validate_config(config: dict[str, Any]) -> None:
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
    template_vars = config.get("template_vars")
    if template_vars is not None and not isinstance(template_vars, dict):
        raise ValidationError(
            "'template_vars' must be a dict if provided", "config.template_vars"
        )


def apply(inputs, config, ctx):
    pipeline_ref = config["pipeline_ref"]
    output_node = config["output_node"]
    template_vars = config.get("template_vars") or {}

    # Depth guard: a sub_pipeline calling another sub_pipeline calling
    # another ... eventually loops. We catch it here before the OS does
    # by stack overflow. Reset depth via the contextvar token so concurrent
    # job runners don't pollute each other's counters.
    current_depth = _sub_pipeline_depth.get()
    if current_depth >= MAX_SUB_PIPELINE_DEPTH:
        raise OpError(
            f"sub_pipeline depth limit exceeded ({MAX_SUB_PIPELINE_DEPTH}) at "
            f"{pipeline_ref!r}: likely a sub-pipeline cycle. Check that no "
            f"sub-pipeline in the chain references back into its caller."
        )
    depth_token = _sub_pipeline_depth.set(current_depth + 1)

    yaml_text = _read_sub_yaml(pipeline_ref)
    if template_vars:
        yaml_text = _substitute_template_vars(yaml_text, template_vars)

    # Defer import to avoid a circular dep at module load time: sub_pipeline
    # lives under graph/ops, and graph.runner imports graph/ops/__init__.py
    # which imports this module.
    from decoy_engine.graph.runner import execute_graph_capture

    try:
        result, cache = execute_graph_capture(
            yaml_text, ctx=ctx, keep_nodes=[output_node]
        )
    except Exception as exc:
        raise OpError(
            f"sub_pipeline {pipeline_ref!r} failed during execution: {exc}"
        ) from exc
    finally:
        _sub_pipeline_depth.reset(depth_token)

    if not result["success"]:
        # Pluck the first node that errored for a clearer message than
        # "sub-pipeline failed". Telemetry from inside the sub-run is
        # still available on result["nodes"] for the caller's logger.
        failed = next(
            (n for n in result["nodes"] if n["status"] == "error"), None
        )
        detail = (
            f"node {failed['node_id']!r} ({failed['kind']}): {failed['error']}"
            if failed
            else "no specific node failure recorded"
        )
        raise OpError(f"sub_pipeline {pipeline_ref!r} failed: {detail}")

    table = cache.get(output_node)
    if table is None:
        raise OpError(
            f"sub_pipeline {pipeline_ref!r} produced no output for node "
            f"{output_node!r} (node missing, never ran, or wrote a sink "
            f"with empty result)"
        )
    return _coerce_parent_engine(table, config)


def _coerce_parent_engine(table, config):
    """Return pandas only when the parent graph explicitly forced pandas.

    Direct calls and hybrid graphs keep the Arrow table produced by
    execute_graph_capture. The runner's pandas safety hatch still works
    because it injects ``__engine='pandas'`` before apply().
    """
    if config.get("__engine") == "pandas":
        return table.to_pandas()
    return table


def _read_sub_yaml(pipeline_ref: str) -> str:
    """Read the referenced YAML file from disk.

    Resolves relative to the current working directory. Pipelines that
    want a different anchor should pass an absolute path or set CWD
    before invoking the runner.
    """
    p = Path(pipeline_ref)
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise OpError(f"sub_pipeline ref not found: {pipeline_ref}") from exc
    except OSError as exc:
        raise OpError(
            f"sub_pipeline ref unreadable: {pipeline_ref}: {exc}"
        ) from exc


def _substitute_template_vars(yaml_text: str, template_vars: dict) -> str:
    """Replace `{{key}}` placeholders in the YAML text.

    Uses `string.Template`-style `$identifier` is not what callers
    expect; the YAML world uses `{{ }}` extensively (Helm, GitHub
    Actions, dbt). Stick with `{{key}}` and a simple replace loop:
    cheap, predictable, no shell-escaping concerns.

    Keys are strings, values are coerced via `str()`. Iterator ops
    typically pass {'iteration.value': '...', 'iteration.index': 0}.
    """
    out = yaml_text
    for key, value in template_vars.items():
        placeholder = "{{" + str(key) + "}}"
        out = out.replace(placeholder, str(value))
    return out

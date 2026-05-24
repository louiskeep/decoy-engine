"""Graph runtime facade: validate / run / preview entry points.

These are the only symbols `decoy_engine.graph` exposes to callers
(see `graph/__init__.py`). The contract is documented in
PIPELINE_GRAPH_GUIDE.md.

V2.0-A: this module is a thin facade that wires together the four
extracted components of the graph runtime:

  - graph.planner.build_plan: pre-execution topology, in-edge index,
    consumer counts, engine-mode resolution.
  - graph._fk_validators: FK / column_relationships validation.
  - graph.executor._execute_graph: per-node execution loop with
    cache, lineage emission, error translation.
  - graph.memory_monitor.PeakRSSMonitor: peak-RSS tracking +
    pressure warnings.

Anything execution-shaped lives in those modules; this file only
holds the public entry points and the assembly that calls into them.
A reader who wants to know "how does the runner run a node?" goes to
executor.py, not here.

Sub-milestone history:
  V2.0-A.1: introduced RunState for per-execution state.
  V2.0-A.2: extracted PeakRSSMonitor + check_memory_pressure.
  V2.0-A.3: moved ancestor_node_ids to planner.py.
  V2.0-A.4: moved _validate_column_relationships and friends to
    _fk_validators.py; moved _execute_graph + _build_pool_resolver
    to executor.py. runner.py dropped from ~1,270 LOC to under 300.
"""

import logging

import pyarrow as pa

from decoy_engine.context import ExecutionContext
from decoy_engine.exceptions import PipelineValidationError
from decoy_engine.graph._fk_validators import _validate_column_relationships
from decoy_engine.graph.config_loading import (
    _load_yaml,
    _validate_or_raise,
    _validate_top_level_or_raise,
)
from decoy_engine.graph.executor import _build_pool_resolver, _execute_graph
from decoy_engine.graph.node_exports import _resolve_node_exports
from decoy_engine.graph.planner import ancestor_node_ids
from decoy_engine.graph.types import PreviewResult, RunResult
from decoy_engine.internal.validator import GraphConfigValidator, ValidationError

# Back-compat re-exports for callers that historically reached into
# runner.py for these symbols. The implementations live elsewhere now;
# the names stay importable from `decoy_engine.graph.runner` so the
# refactor is a no-behavior-change move for downstream code.
__all__ = [
    "PreviewResult",
    "RunResult",
    # Back-compat: these symbols moved to executor.py /
    # node_exports.py / config_loading.py but stay importable here.
    "_build_pool_resolver",
    "_execute_graph",
    "_load_yaml",
    "_resolve_node_exports",
    "execute_graph_capture",
    "preview_graph",
    "run_graph",
    "validate_graph",
    "validate_graph_full",
]


def validate_graph(yaml_text: str) -> None:
    """Validate graph YAML. Raises PipelineValidationError on bad config.

    The raised exception carries the optional ``path`` and ``code``
    attributes so a platform caller can map the failure back to a
    specific node / inspector field instead of string-parsing the
    message. New callers should prefer :func:`validate_graph_full`
    which returns a multi-message ``ValidationResult`` instead of
    raising; this raise-style entry stays for backward compatibility.
    """
    config = _load_yaml(yaml_text)
    _quiet_logger = logging.getLogger("decoy_engine.graph.validate")
    if not _quiet_logger.handlers:
        _quiet_logger.addHandler(logging.NullHandler())
    try:
        GraphConfigValidator(_quiet_logger).validate(config)
    except ValidationError as e:
        raise PipelineValidationError(
            str(e),
            path=e.path,
            code=getattr(e, "code", None),
        ) from e


def validate_graph_full(yaml_text: str, *, strict: bool = False):
    """Validate graph YAML and return a non-raising :class:`ValidationResult`.

    Unlike :func:`validate_graph`, this never raises on validation
    failure. Instead it returns a ``ValidationResult`` with structured
    ``errors`` and ``warnings`` lists; callers can render every problem
    at once and map each to a UI field via the stable ``code`` string.

    YAML parse errors (the input isn't valid YAML at all) still raise
    as ``decoy_engine.exceptions.ConfigError`` -- they're an upstream
    problem, not a validation outcome. Use a try/except at the call
    site if needed.

    Each validation stage is tried independently. A top-level structural
    error stops further validation (no safe graph to walk). The nodes
    stage (stage 2, R2.2) collects all per-node errors so a graph with
    multiple bad nodes surfaces every failure in one pass. Edge,
    cardinality, topology, and cross-node stages each capture their first
    failure before moving on. The cross-node semantic checks (format
    consistency, mask column reachability, nodes-ref reachability) run
    independently of each other so all three can surface errors in one
    pass. Within stages 3-8 the first failure still stops that stage.

    ``normalized_config`` is a deep copy of the parsed input with defaults
    applied (e.g. target.file format inferred from the source format). It
    is set only when there are no errors. The original parsed config is
    never mutated.

    ``strict=True`` enables production-mode gating: checks that are
    advisory in lenient mode become blocking errors. Full strict-mode
    coverage (credentials, variable resolution, side-effect policy) is
    Sprint 2 follow-up work; the parameter is accepted now so callers
    can adopt it forward.
    """
    import copy

    from decoy_engine.validation_result import CODES, ValidationResult

    result = ValidationResult()
    config = _load_yaml(yaml_text)
    _quiet_logger = logging.getLogger("decoy_engine.graph.validate")
    if not _quiet_logger.handlers:
        _quiet_logger.addHandler(logging.NullHandler())

    # Deep copy so _validate_file_format_consistency's format back-fill
    # never mutates the locally-parsed config dict. The normalized_config
    # returned to the caller is the deep copy with defaults applied.
    working = copy.deepcopy(config)
    validator = GraphConfigValidator(_quiet_logger)

    def _collect(stage_fn) -> bool:
        """Call stage_fn(). Add its first error to result. Return True iff it passed."""
        try:
            stage_fn()
            return True
        except ValidationError as e:
            result.add_error(
                code=getattr(e, "code", None) or CODES.UNTAGGED,
                message=getattr(e, "raw_message", None) or str(e),
                path=getattr(e, "path", None),
            )
            return False

    # Stage 1: top-level shape. Required before safely extracting nodes/edges.
    if not _collect(lambda: validator._validate_top_level(working)):
        return result

    # Post-stage-1: nodes is a non-empty list, edges is a list or absent.
    nodes = working["nodes"]
    edges = working.get("edges") or []
    kinds = validator._known_kinds()

    # Stage 2: per-node metadata -- collects ALL per-node errors so a graph
    # with multiple bad nodes surfaces all of them in one pass (R2.2).
    node_errors = validator._validate_nodes_collecting(nodes, kinds)
    for _e in node_errors:
        result.add_error(
            code=getattr(_e, "code", None) or CODES.UNTAGGED,
            message=getattr(_e, "raw_message", None) or str(_e),
            path=getattr(_e, "path", None),
        )
    nodes_ok = not node_errors

    # Stages 3-5: graph structure. Each requires the prior stage to pass.
    edges_ok = nodes_ok and _collect(lambda: validator._validate_edges(edges, nodes))
    cardinality_ok = edges_ok and _collect(
        lambda: validator._validate_cardinality(nodes, edges, kinds)
    )
    topology_ok = cardinality_ok and _collect(lambda: validator._validate_acyclic(nodes, edges))

    # Stages 6-8: cross-node semantic checks. Each runs independently so
    # all three can surface errors in one pass. All require a sound acyclic
    # graph to walk.
    if topology_ok:
        _collect(lambda: validator._validate_file_format_consistency(nodes, edges, strict=strict))
        _collect(lambda: validator._validate_mask_column_reachability(nodes, edges))
        _collect(lambda: validator._validate_nodes_ref_reachability(nodes, edges))

    # Stage 9: FK / m2m / multi-parent relationship validation.
    # Independent of stages 6-8 (those check graph mechanics); this
    # checks the column_relationships: block shape and ordering.
    # Requires topology_ok because it needs plan.order for
    # parent-before-child checks.
    if topology_ok:
        _collect(lambda: _validate_column_relationships(working, strict=strict, result=result))

    if not result.errors:
        result.normalized_config = working
    return result


def run_graph(yaml_text: str, ctx: ExecutionContext | None = None) -> RunResult:
    """Execute the DAG end-to-end.

    Returns a RunResult with per-node telemetry. On the first node that
    raises, the runner stops, records the failure, and returns
    success=False. Remaining nodes are not executed.

    FlagPauseSignal is re-raised without wrapping -- the platform runner
    catches it to transition the job to review_pending.
    """
    config = _load_yaml(yaml_text)
    _validate_or_raise(config)
    result, _cache = _execute_graph(config, ctx)
    return result


def execute_graph_capture(
    yaml_text: str,
    ctx: ExecutionContext | None = None,
    keep_nodes: list[str] | None = None,
) -> tuple[RunResult, dict[str, pa.Table]]:
    """Run a graph and return both the telemetry record AND the output
    cache for nodes named in `keep_nodes`.
    """
    config = _load_yaml(yaml_text)
    _validate_or_raise(config)
    result, cache = _execute_graph(config, ctx, keep_nodes=keep_nodes)
    return result, cache


def preview_graph(
    yaml_text: str,
    node_id: str,
    row_limit: int = 50,
    ctx: ExecutionContext | None = None,
) -> PreviewResult:
    """Best-effort sample of `node_id`'s output."""
    from decoy_engine.graph.preview import PreviewPolicy, run_preview

    config = _load_yaml(yaml_text)
    _validate_top_level_or_raise(config)

    nodes = config["nodes"]
    edges = config.get("edges") or []
    if not any(isinstance(n, dict) and n.get("id") == node_id for n in nodes):
        raise PipelineValidationError(f"node {node_id!r} not in graph")

    needed = ancestor_node_ids(nodes, edges, node_id)
    sub_config = {
        **config,
        "nodes": [n for n in nodes if isinstance(n, dict) and n.get("id") in needed],
        "edges": [
            e
            for e in edges
            if isinstance(e, dict)
            and isinstance(e.get("from"), str)
            and isinstance(e.get("to"), str)
            and e["from"].split(".", 1)[0] in needed
            and e["to"] in needed
        ],
    }
    _validate_or_raise(sub_config)

    policy = PreviewPolicy(
        target_node_id=node_id,
        row_limit=max(1, min(int(row_limit), 1000)),
    )
    return run_preview(sub_config, policy, ctx)

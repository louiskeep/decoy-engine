"""TypedDicts for the graph pipeline format.

Shape contract is defined in PIPELINE_GRAPH_GUIDE.md. These types are
internal helpers; callers should not depend on them -- they read/write
the YAML directly.
"""

from typing import Any, Literal, TypedDict


class NodeSpec(TypedDict, total=False):
    id: str
    kind: str
    # Optional human label. Distinct from `id` (which is the stable ref
    # used by edges and validators). Surfaces in run logs so users can
    # tell two `mask` nodes apart by name without consulting the YAML.
    name: str
    config: dict[str, Any]
    position: dict[str, float]


class EdgeSpec(TypedDict):
    # YAML key is `from`; we map to `from_` in code because `from` is a keyword.
    from_: str
    to: str


class GraphConfig(TypedDict, total=False):
    mode: Literal["graph"]
    schema_version: int
    settings: dict[str, Any]
    nodes: list[NodeSpec]
    edges: list[Any]


class NodeRunRecord(TypedDict, total=False):
    node_id: str
    kind: str
    status: Literal["ok", "error"]
    row_count: int | None
    elapsed_ms: int
    error: str | None
    # Per-node exports captured via `ctx.export(key, value)` inside the op.
    # Flat dict of JSON-serializable scalars; downstream nodes reference
    # values via `${nodes.<id>.<key>}` substitution at run time. Keys per
    # op kind documented in PIPELINE_GRAPH_GUIDE.md "Node exports".
    exports: dict[str, Any] | None


class RunResult(TypedDict):
    nodes: list[NodeRunRecord]
    success: bool
    elapsed_ms: int


class PreviewResult(TypedDict, total=False):
    node_id: str
    columns: list[str]
    rows: list[list[Any]]
    applied_chain: list[str]
    row_count: int
    elapsed_ms: int
    error: str | None
    # True when the target node produced more rows than row_limit; the
    # platform can surface this as "showing first N of M rows".
    truncated: bool
    # Reason a node was skipped or its side effect suppressed:
    #   "side-effect-suppressed": target kind started with "target." and
    #       __preview_row_limit caused the op to skip its write.
    #   "gate-blocked": FlagPauseSignal raised on the target node.
    #   None: preview completed normally (possibly truncated but not skipped).
    skip_reason: str | None

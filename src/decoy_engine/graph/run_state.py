"""Runner-internal execution state.

V2.0-A.1 (first sub-milestone of the runner decomposition): collect the
loose locals threaded through _execute_graph into a single named
dataclass. Replaces a kit of floating per-execution variables with a
named structure that subsequent sub-milestones (A.2 memory monitor,
A.3 planner, A.4 executor) can pass around without growing function
signatures.

Why a dedicated dataclass and not just keep them as locals: grep-ability
is correctness. When a function body has 12 floating locals threaded
into 4 helper calls, a change to one of them ripples invisibly. When
the state lives in a named dataclass, the field's read sites and write
sites are findable with a single grep.

Note on the `ctx._current_node_id` and `ctx._exports` discussion in the
V2 plan: those two attributes intentionally stay on ExecutionContext
because they are part of the op contract -- every op calls
`ctx.export(key, value)` which routes the value to the active node's
exports via `ctx._current_node_id`. Promoting them off `ctx` would
require widening every op's signature to take an explicit node_id.
That widening is out of scope for V2.0-A; the executor extraction in
A.4 may revisit the API design, but the no-behavior-change refactor
sub-milestones must leave the op surface alone.

Pattern: explicit per-execution state object (Polars LogicalPlan/
PhysicalPlan pattern; Ray RayContext pattern). State is mutable by
design (the runner writes to it during execution) but its shape is
fixed at instantiation. Citing this in the methodology registry would
be over-engineering; the pattern is too universal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover -- type-checker only
    import pyarrow as pa


@dataclass
class RunState:
    """Per-execution mutable state for the graph runner.

    Constructed at the top of ``_execute_graph`` and threaded through
    helpers. Fields are added as A.2/A.3/A.4 lift code out of the
    runner; today the dataclass is intentionally small so the diff
    that introduces it does not also rewrite the runner's loop.

    Field reference:

      current_node_id: id of the node currently being executed. Set
        immediately before the op's ``apply()`` call and cleared
        after. The runner ALSO mirrors this to ``ctx._current_node_id``
        because ``ctx.export()`` reads from there; the duplication is
        intentional and is documented in the module docstring above.

      success: rolling flag set to False the first time a node fails.
        Used at sprint close to populate ``RunResult.success``.

      records: list of NodeRunRecord entries appended as each node
        finishes. Becomes ``RunResult.nodes`` at the end of the run.

      overall_start: monotonic timestamp captured before the first
        node executes. Used to compute ``RunResult.elapsed_ms``.

      node_outputs: cache of node-id -> output Arrow table. Lives
        alongside the GraphCache (which is the in-flight working set
        the executor pops from). ``node_outputs`` is the post-execute
        survivor set returned to callers that asked for ``keep_nodes``.

      memory_monitor: reference to the active ``_PeakRSSMonitor`` for
        introspection by helpers (currently unused; will be the seam
        A.2 lifts the monitor through).
    """

    current_node_id: str | None = None
    success: bool = True
    records: list = field(default_factory=list)
    overall_start: float = 0.0
    node_outputs: dict[str, pa.Table] = field(default_factory=dict)
    memory_monitor: Any | None = None

    def begin_node(self, node_id: str, ctx: Any) -> None:
        """Mark `node_id` as the currently-executing node.

        Mirrors the id to ``ctx._current_node_id`` so the existing
        ``ctx.export()`` API keeps routing values to the right node.
        The mirror is the load-bearing reason this method exists
        rather than a plain attribute assignment.
        """
        self.current_node_id = node_id
        ctx._current_node_id = node_id

    def end_node(self, ctx: Any) -> None:
        """Clear the current-node marker.

        Called after a node finishes (success or failure). Symmetric
        with begin_node() so a future helper that wants to do
        per-node setup/teardown has obvious hooks.
        """
        self.current_node_id = None
        ctx._current_node_id = None

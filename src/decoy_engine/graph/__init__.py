"""Graph-mode pipeline runtime.

Public symbols:
    validate_graph       - raise PipelineValidationError on bad config (legacy)
    validate_graph_full  - return ValidationResult, never raise (R2.1)
    run_graph            - execute the DAG end-to-end
    preview_graph        - best-effort sample of a single node's output
    RunResult            - shape returned by run_graph
    PreviewResult        - shape returned by preview_graph
"""

from decoy_engine.graph.runner import (
    run_graph,
    preview_graph,
    validate_graph,
    validate_graph_full,
)
from decoy_engine.graph.types import RunResult, PreviewResult

__all__ = [
    "validate_graph",
    "validate_graph_full",
    "run_graph",
    "preview_graph",
    "RunResult",
    "PreviewResult",
]

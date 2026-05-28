"""PostValidationRunner: the opt-in post-execution scan suite (engine-v2 S10).

Post-execution validation is OPT-IN via the `post_validation: true` pipeline flag
(the operating model's two-layer model; cross-sprint contracts R13). Flag off ->
the phase is not entered, no `quality_summary` is produced, zero overhead. Flag on
-> the runner walks the scan suite over the ALREADY-MASKED S9 output, aggregates a
`QualitySummary`, and the orchestrator serializes it into the manifest's
`quality_summary` block.

The runner does NOT re-run masking: it scans `execution_result.outputs` and
compares against `sources`. It owns no check logic that S1-S9 already shipped; the
8 scans land in slices 3-5. This slice ships the scaffolding + the flag gate +
forwards the S5/S9 `QualityWarning` event channel (`execution_result.warnings`,
per R14) + records the phase timing.

Source pattern: SDV's `evaluate.run_diagnostic` (a post-generation quality scan
suite over synthetic output) + NIST SP 800-188 disclosure/audit framing.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from decoy_engine.validation.post._types import QualitySummary

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.execution import ExecutionResult
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


class PostValidationRunner:
    """Runs the opt-in post-execution scan suite and builds the QualitySummary."""

    def run(
        self,
        *,
        plan: Plan,
        execution_result: ExecutionResult,
        sources: Mapping[str, pa.Table],
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        config: dict[str, Any],
    ) -> QualitySummary | None:
        """Scan the masked output if `post_validation` is on, else return None.

        Returns None (phase not entered, zero overhead) when the flag is off.
        When on, returns a `QualitySummary`. Slice 2 ships the scaffolding: the
        scan-populated fields are empty and the runner forwards the QualityWarning
        events + the phase timing; the 8 scans land in slices 3-5.
        """
        if not bool(config.get("post_validation", False)):
            return None

        t0 = time.perf_counter()
        # The 8 scans (slices 3-5) populate distinct_counts / null_counts /
        # fk_validity / duplicate_counts / sampled_values / composite_coherence /
        # failed_checks here, reading execution_result.outputs vs sources.
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return QualitySummary(
            distinct_counts={},
            null_counts={},
            fk_validity={},
            duplicate_counts={},
            sampled_values={},
            quality_warnings=execution_result.warnings,
            composite_coherence={},
            timing_per_phase={"post_validation_phase_ms": elapsed_ms},
        )

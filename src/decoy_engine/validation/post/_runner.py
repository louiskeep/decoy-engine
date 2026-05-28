"""PostValidationRunner: the opt-in post-execution scan suite (engine-v2 S10).

Post-execution validation is OPT-IN via the `post_validation: true` pipeline flag
(the operating model's two-layer model; cross-sprint contracts R13). Flag off ->
the phase is not entered, no `quality_summary` is produced, zero overhead. Flag on
-> the runner builds a `ScanContext`, walks the registered scans (skipping any in
`post_validation_skip`), merges every `ScanOutcome` into one `QualitySummary` at a
single site, sets `failed_checks`, forwards the S5/S9 `QualityWarning` events +
any scan-emitted warnings, and records the phase timing.

The runner does NOT re-run masking: it scans `execution_result.outputs` and
compares against `sources` / `profile`. It owns no check logic that S1-S9 shipped.
Per Dennis's S10 slice-1-2 review the run contract carries `profile` (declared_pk
+ authoritative source stats) + `registry` (CapabilityMatrix: format_regex,
backend_type) in addition to the S9 surface.

Source pattern: SDV's `evaluate.run_diagnostic` (a post-generation quality scan
suite over synthetic output) + NIST SP 800-188 disclosure/audit framing.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.validation.post._checks import SCANS
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome
from decoy_engine.validation.post._types import (
    CompositeCoherenceReport,
    DistinctCount,
    FkValidityReport,
    NullCount,
    QualitySummary,
)

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.execution import ExecutionResult
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


class PostValidationRunner:
    """Runs the opt-in post-execution scan suite and builds the QualitySummary."""

    def run(
        self,
        *,
        plan: Plan,
        execution_result: ExecutionResult,
        sources: Mapping[str, pa.Table],
        profile: Profile,
        registry: ProviderRegistry,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        config: dict[str, Any],
    ) -> QualitySummary | None:
        """Scan the masked output if `post_validation` is on, else return None.

        Returns None (phase not entered, zero overhead) when the flag is off. When
        on, walks the registered scans (minus `post_validation_skip`), merges their
        outcomes into one QualitySummary, sets failed_checks, and forwards the
        QualityWarning events + the phase timing.
        """
        if not bool(config.get("post_validation", False)):
            return None

        t0 = time.perf_counter()
        skip = set(config.get("post_validation_skip", []) or [])
        ctx = ScanContext(
            plan=plan,
            outputs=execution_result.outputs,
            sources=sources,
            profile=profile,
            registry=registry,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
        )

        outcomes: list[ScanOutcome] = []
        for name, scan in SCANS:
            if name in skip:
                continue
            # Slice 5 wraps this in try/except so a crashing scan becomes a failed
            # outcome (with the traceback) rather than a lost manifest.
            outcomes.append(scan(ctx))

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return _merge(outcomes, execution_result.warnings, elapsed_ms)


def _merge(
    outcomes: list[ScanOutcome],
    forwarded_warnings: tuple[QualityWarning, ...],
    elapsed_ms: float,
) -> QualitySummary:
    """Fold every scan's fragment into one QualitySummary (the single merge site)."""
    distinct_counts: dict[str, DistinctCount] = {}
    null_counts: dict[str, NullCount] = {}
    fk_validity: dict[str, FkValidityReport] = {}
    duplicate_counts: dict[str, int] = {}
    sampled_values: dict[str, list[Any]] = {}
    composite_coherence: dict[str, CompositeCoherenceReport] = {}
    scan_warnings: list[QualityWarning] = []
    for outcome in outcomes:
        distinct_counts.update(outcome.distinct_counts)
        null_counts.update(outcome.null_counts)
        fk_validity.update(outcome.fk_validity)
        duplicate_counts.update(outcome.duplicate_counts)
        sampled_values.update(outcome.sampled_values)
        composite_coherence.update(outcome.composite_coherence)
        scan_warnings.extend(outcome.warnings)
    return QualitySummary(
        distinct_counts=distinct_counts,
        null_counts=null_counts,
        fk_validity=fk_validity,
        duplicate_counts=duplicate_counts,
        sampled_values=sampled_values,
        quality_warnings=tuple(forwarded_warnings) + tuple(scan_warnings),
        composite_coherence=composite_coherence,
        timing_per_phase={"post_validation_phase_ms": elapsed_ms},
        failed_checks=tuple(o.name for o in outcomes if o.failed),
    )

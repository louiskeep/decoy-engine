"""`SeamContext`: the per-invocation identity carrier this seam owns (Task 4.2, D1).

Named `SeamContext` rather than `ExecutionContext` on purpose: the latter name
is already the distinct, public, caller-supplied runtime context
(`decoy_engine.context.ExecutionContext`, logger + telemetry). This is an
internal bookkeeping value the characterization and disconnection tests use to
identify which driver/scope/table(s) one adapter call belongs to; it carries
no execution state of its own. The job-scoped mutable state every pandas-hosted
operator shares (`StrategyContext`) is untouched and unwrapped -- adapters
thread the caller's existing `StrategyContext` straight through their
delegate, never a fresh one (plan C2).
"""

from __future__ import annotations

from dataclasses import dataclass

from decoy_engine.execution.physical._types import DriverId, ExecutionScope


@dataclass(frozen=True)
class SeamContext:
    """Identifies one adapter invocation: which driver, at what scope, over
    which table(s). Constructed by a driver adapter's `run(...)` for its own
    bookkeeping (and read by tests); never required input to a delegate call
    and never mutated once built.
    """

    driver_id: DriverId
    scope: ExecutionScope
    tables: tuple[str, ...]

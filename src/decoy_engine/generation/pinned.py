"""Pinned generation: public entry points.

``compile_pinned_plan(config, reference_date)`` resolves a generation job into a ``PinnedPlan``;
``compile_pinned_plan_with_generation`` also returns the ``GenerationPlan`` of the same compile for
window admission. ``fingerprint(plan)`` is the plan's seed-independent identity;
``execute_pinned(plan, seed)`` generates tables whose bytes depend only on the plan, the seed and
the runtime identity. Ordinary ``generate_tables`` is unchanged.
"""

from __future__ import annotations

from decoy_engine.generation._pinned_compile import (
    PinnedCompilation,
    compile_pinned_plan,
    compile_pinned_plan_with_generation,
)
from decoy_engine.generation._pinned_execute import execute_pinned
from decoy_engine.generation._pinned_plan import (
    PinnedColumn,
    PinnedModeError,
    PinnedPlan,
    PinnedStatistical,
    PinnedTable,
    fingerprint,
    runtime_identity,
)

__all__ = [
    "PinnedColumn",
    "PinnedCompilation",
    "PinnedModeError",
    "PinnedPlan",
    "PinnedStatistical",
    "PinnedTable",
    "compile_pinned_plan",
    "compile_pinned_plan_with_generation",
    "execute_pinned",
    "fingerprint",
    "runtime_identity",
]

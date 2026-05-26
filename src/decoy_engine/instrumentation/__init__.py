"""decoy_engine.instrumentation: performance measurement surface.

Public package for engine-side timing and memory instrumentation. The
platform reads timing data via the evidence manifest; the CLI consumes
it via `decoy bench`. Internal compute paths use the helpers here as
opt-in measurement (zero overhead when no collector is active).

See [PERF.BASE.1 sprint spec](../../../../../decoy-platform/docs/v2/sprints/perf-baseline/perf-base-1-instrumentation.md)
for the motivation and the design constraints (overhead under 2%, no
behavior change to existing code paths when instrumentation is off).
"""

from decoy_engine.instrumentation.timing import (
    StrategyTimingRecord,
    TimingCollector,
    get_active_collector,
    rss_kb,
    timed_strategy,
    use_collector,
)

__all__ = [
    "StrategyTimingRecord",
    "TimingCollector",
    "get_active_collector",
    "rss_kb",
    "timed_strategy",
    "use_collector",
]

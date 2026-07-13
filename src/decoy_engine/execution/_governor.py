"""Sprint B4: the RUNTIME GOVERNOR -- the last-line safety net that kills a
job approaching OOM and reroutes it, so an estimator/probe miss (B1a/B2) can
never produce an opaque kernel `rc137`.

`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §3.7, corrected by
§11's §3.7 erratum (read both before touching this module):

  - **Supervisor-kills-CHILD, not an in-process watchdog.** §11 disproves the
    original "sample cgroup `memory.current` in a watchdog thread" design as
    first specified: a Python thread cannot cleanly abort a native
    pandas/pyarrow allocation mid-call (signals/flags are only checked when
    the native call returns control to the interpreter, and a multi-GB step
    allocation blows through an 85%->93% window between two samples). It only
    works when the job runs in a CHILD PROCESS a supervisor can `SIGKILL` from
    the outside -- which is exactly what Sprint 1a's `run_pipeline_isolated`
    already gives us via its `on_spawn: Callable[[int], None]` hook. This
    module is that supervisor: `_RssMonitor` is a background THREAD in the
    DRIVER process, polling the CHILD's `/proc/<pid>/status`, and killing the
    CHILD from outside its address space -- a mechanism no native allocation
    can dodge, unlike a same-process check.
  - **VmRSS, not cgroup `memory.current`.** `memory.current` counts page
    cache, which spikes during the out-of-core route's DuckDB spill and
    Parquet I/O -- exactly the traffic a correctly-behaving bounded job
    generates, so gating on it would false-trip a job that was never at real
    OOM risk. `/proc/<pid>/status`'s `VmRSS` is this ONE process's own
    resident set from the kernel's per-task accounting (proc(5)): it moves
    with the child's real working set, not a whole-cgroup cache figure.
  - **Reroute ladder, not mid-job checkpointing.** The engine is seeded and
    deterministic (spec §3.7), so a killed job is re-run FROM SCRATCH on the
    next bounded route rather than resumed -- losing only wall-clock time,
    never correctness. No checkpoint state is built or read here.
  - **Abort cleanup is free.** A `SIGKILL`ed child is classified `oom_killed`
    by `run_pipeline_isolated` (Sprint 1a's own two-tier classification,
    `_isolated_common.classify_abnormal_exit`), which discards its staging
    directory unconditionally -- nothing is ever committed to the caller's
    `output_dir` for any outcome other than `completed` (`_isolated_run.py`
    module docstring point 4). This module adds NO new abort-cleanup logic;
    it inherits 1a's staging discipline for free by going through
    `run_pipeline_isolated` for every rung.

Ladder: `full_frame` -> (killed) -> `out_of_core` (if eligible) -> (killed)
-> `sequential` (if eligible) -> else FAIL with a clear diagnostic naming the
routes tried and the observed peaks -- never a bare process exit code.

Flag-gated (additive): `use_runtime_governor` (default `False`) composes with
the B1b/B2 routing flags (`use_byte_estimate_routing`, `use_probe_routing`)
the same way they compose with each other -- each is independently off by
default and inert unless explicitly opted into. OFF, `run_job_with_governor`
makes exactly ONE `run_pipeline_isolated` call on the ladder's first rung with
no monitor attached and no reroute on failure: identical to calling
`run_pipeline_isolated` directly today. This module is not wired into
`run_pipeline`'s own routing (`_pipeline_routing.decide_execution_route`) or
the platform queue worker in this sprint -- it is a standalone primitive, the
same "additive unit, production wiring is a later sprint" shape B1a/B1b/B2
shipped in.

Telemetry (B5 prerequisite, not built here): every governor trip -- a
supervisor kill OR a self-detected OOM on any rung -- is by definition an
estimator/probe miss (the estimator predicted this route would fit and it did
not). `GovernorTripRecord` is the structured shape B5's telemetry loop will
consume; `run_job_with_governor` both returns the full trip history
(`GovernorResult.trips`) and, optionally, invokes an `on_trip` callback as
each one happens, so a caller does not have to wait for the whole ladder to
finish to start recording misses.

TB-2 status (`docs/plans/2026-07-12-track-b-completion-program.md`): the 50M
benchmark's governor phase (B6) proved containment (this module already did
its job -- a clean `SIGKILL` + diagnostic, never a wedge) but not
reroute-to-COMPLETION, because two things were true at the time: (1) the
production out-of-core route was not actually memory-bounded yet (TB-1's
`#56`, fixed on `main` -- lazy sources + sink streaming + the `_isolated_
kwargs_with_budget` forwarding below), and (2) B6's fixed benchmark budget
predated that fix and was never recalibrated against it, so it sat below
EVERY route's real need. Neither defect was in this module's reroute LADDER
itself -- `tests/perf/test_governor_reroute_completion.py` is the calibrated,
real-subprocess proof that with TB-1 landed and a properly chosen budget
window, the SAME mechanism here reroutes a genuinely-tripped `full_frame` run
all the way to a completed, FK-consistent `out_of_core` run, no code change
required in this file. Read that test's module docstring for the measured
numbers behind the calibration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from decoy_engine.execution._governor_monitor import _RssMonitor
from decoy_engine.execution._isolated_common import IsolatedRunResult
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from decoy_engine.execution._substrate import require_bool

if TYPE_CHECKING:
    import pyarrow as pa

__all__ = [
    "GovernorResult",
    "GovernorRoute",
    "GovernorTripKind",
    "GovernorTripRecord",
    "run_job_with_governor",
]

GovernorRoute = Literal["full_frame", "out_of_core", "sequential"]

# SC2 priority order (fastest-and-riskiest first): the same ladder
# `decide_execution_route` already prefers when it is confident a job fits,
# just walked in reverse-confidence order on governor trips instead of
# decided once up front.
_DEFAULT_LADDER: tuple[GovernorRoute, ...] = ("full_frame", "out_of_core", "sequential")

# Spec §3.7: "hard threshold (~93%)". There is deliberately no separate soft
# threshold in this sprint -- §11's erratum retired the original soft-abort-
# and-reroute design (an in-process abort cannot preempt a native allocation
# either) in favor of a single supervisor-enforced hard kill; a future sprint
# may add an advisory soft signal once there is a channel to act on it
# in-process without relying on the interpreter checking flags.
_DEFAULT_HARD_THRESHOLD_FRACTION = 0.93

# Spec §3.7: "every few seconds" / task brief: "every ~1-2s". 1.5s splits the
# difference: frequent enough that a governor trip is caught within a bounded
# lag of crossing the threshold, infrequent enough that the polling thread's
# own overhead never competes meaningfully with the child's real work.
_DEFAULT_POLL_INTERVAL_S = 1.5

GovernorTripKind = Literal["governor_kill", "self_oom", "route_ineligible", "crashed"]

# `decide_execution_route` (`_pipeline_routing.py`) raises a `ConfigError`
# with one of these stable, already-load-bearing message fragments when an
# explicit `execution_mode` override names a route the job is NOT eligible
# for -- see its "sequential"/"out_of_core" `execution_mode` branches. A
# `ConfigError` crossing the isolated-run process boundary is not memory-
# shaped (`_isolated_common.is_memory_failure` never matches it), so it comes
# back as a `crashed` `IsolatedRunResult` carrying this exact text in
# `.error`. Matching on it (rather than trying to re-derive eligibility from
# `config`/`sources` here, which would require compiling a `Plan` +
# `RelationshipGraph` this generic wrapper never sees) is the same marker-
# based cross-process classification `_isolated_common`'s own
# `_ABNORMAL_EXIT_MEMORY_MARKERS` already uses for the OOM/crash split -- an
# established pattern in this module family, not a new heuristic.
_INELIGIBLE_ROUTE_MARKERS: dict[GovernorRoute, tuple[str, ...]] = {
    "out_of_core": (
        "is not out-of-core-eligible",
        "is not out-of-core-compatible",
        "no mask-kind table to run through the out-of-core path",
    ),
    "sequential": (
        "is not sequential-eligible",
        "FK graph has a cross-table cycle",
        "no mask-kind table to run through the sequential path",
    ),
    # full_frame has no forced-mode eligibility gate in `decide_execution_
    # route` (it is the universal override), so it never lands here as a
    # rung within the ladder's reroute (only ever the first rung, and it is
    # always "eligible").
    "full_frame": (),
}


def _is_route_ineligible_error(route: GovernorRoute, error: str | None) -> bool:
    if not error:
        return False
    return any(marker in error for marker in _INELIGIBLE_ROUTE_MARKERS.get(route, ()))


@dataclass(frozen=True)
class GovernorTripRecord:
    """One estimator/probe miss: a route attempt that did not complete
    because the governor killed it or it self-OOMed, or because the reroute
    ladder's next rung was not eligible for this job.

    B5 (not built here) recomputes `k_path` from exactly this shape:
    `(route, budget_bytes, observed_peak_mb)` is the "predicted vs actual"
    pair its telemetry loop needs; `trip_kind`/`reroute_to`/`error` are the
    diagnostic context for why the trip happened and what the governor did
    about it.
    """

    route: GovernorRoute
    budget_bytes: int
    observed_peak_mb: float | None
    trip_kind: GovernorTripKind
    reroute_to: GovernorRoute | None
    error: str | None


GovernorOutcome = Literal["completed", "exhausted"]


@dataclass(frozen=True)
class GovernorResult:
    """What `run_job_with_governor` returns: either a completed run (with the
    route that finally succeeded and the full trip history that preceded
    it), or an exhausted ladder with a human-readable diagnostic -- NEVER a
    bare subprocess return code standing in for "the job died."

    `result` is `None` when the reroute ladder itself exhausted (every rung
    tried, per-rung failures recorded in `trips`). LOW-1: the flag-off
    (`use_runtime_governor=False`) exhausted path has no trip history, so
    it carries the ONE rung's own `IsolatedRunResult` here instead --
    callers keep its `outcome`/`peak_rss_mb`/`error` rather than losing it.
    """

    outcome: GovernorOutcome
    final_route: GovernorRoute | None
    result: IsolatedRunResult | None
    trips: tuple[GovernorTripRecord, ...] = ()
    diagnostic: str | None = None


def _next_rung(ladder: tuple[GovernorRoute, ...], index: int) -> GovernorRoute | None:
    return ladder[index + 1] if index + 1 < len(ladder) else None


def _exhausted_diagnostic(trips: tuple[GovernorTripRecord, ...], budget_bytes: int) -> str:
    budget_mb = budget_bytes / (1024 * 1024)
    parts = []
    for trip in trips:
        if trip.trip_kind == "route_ineligible":
            parts.append(f"route={trip.route} not eligible for this job ({trip.error})")
        elif trip.trip_kind == "crashed":
            parts.append(f"route={trip.route} crashed ({trip.error})")
        else:
            peak = (
                f"{trip.observed_peak_mb:.1f}MB" if trip.observed_peak_mb is not None else "unknown"
            )
            parts.append(
                f"route={trip.route} peak={peak} exceeded the {budget_mb:.1f}MB budget "
                f"({trip.trip_kind})"
            )
    return (
        "runtime governor exhausted the reroute ladder without a completed run: "
        + "; ".join(parts)
        + ". No further bounded route is available for this job -- reduce its size, "
        "widen out-of-core/sequential eligibility, or raise the memory budget. This is "
        "a diagnosed routing exhaustion, not an opaque process kill."
    )


def run_job_with_governor(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None = None,
    *,
    budget_bytes: int,
    ladder: tuple[GovernorRoute, ...] = _DEFAULT_LADDER,
    use_runtime_governor: bool = False,
    hard_threshold_fraction: float = _DEFAULT_HARD_THRESHOLD_FRACTION,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    on_trip: Callable[[GovernorTripRecord], None] | None = None,
    **isolated_kwargs: Any,
) -> GovernorResult:
    """Run one job under the runtime governor: monitor its child process's
    VmRSS, `SIGKILL` it on a hard-threshold cross, and reroute deterministically
    to the next bounded route on the ladder -- never surfacing a bare `rc137`.

    Args:
        config, sources: forwarded to `run_pipeline_isolated` unchanged for
            every rung attempted; the ladder re-runs the SAME job (the engine
            is seeded, so a restart with the same seed is deterministic and
            loses only wall-clock time -- no mid-job checkpointing exists or
            is needed).
        budget_bytes: this job's slot memory budget (e.g. `out_of_core.
            _budget.resolve_budget(...).budget_bytes`, or the platform's
            `WorkerBudget` charge) -- the hard kill threshold is
            `hard_threshold_fraction` of THIS number, re-applied unchanged at
            every rung (each route is assumed to share the same slot budget;
            a caller with per-route budgets should call this once per rung
            itself instead). TB-1 fix #3: this is ALSO forwarded as
            `run_pipeline`'s `out_of_core_budget_bytes` at every rung
            (`_isolated_kwargs_with_budget`), unless `isolated_kwargs`
            already sets it explicitly -- so the out-of-core route's DuckDB
            `memory_limit` is sized from this job's actual slot share, not
            `resolve_budget(None)`'s 25%-of-host-RAM fallback.
        ladder: the routes to try in order. Defaults to the SC2 priority
            order (`full_frame`, `out_of_core`, `sequential`). A caller that
            already knows a job is not full_frame-eligible (e.g. it forced
            `out_of_core` at submit time) can pass a shorter ladder starting
            there.
        use_runtime_governor: default `False` -- the flag gate. OFF, this
            function makes exactly ONE `run_pipeline_isolated` call on
            `ladder[0]` with NO monitor attached and NO reroute on failure:
            byte-for-byte what calling `run_pipeline_isolated` directly does
            today. ON, the RSS monitor is attached to every rung and a
            governor trip (or a self-detected OOM) advances to the next rung
            instead of surfacing the raw `oom_killed` result to the caller.
        hard_threshold_fraction: fraction of `budget_bytes` that trips the
            kill. Spec §3.7 default ~0.93.
        poll_interval_s: how often the monitor samples the child's VmRSS.
            Spec §3.7 default "every few seconds" / task brief "~1-2s"; 1.5s
            here. Tests pass a much smaller value to keep a real-subprocess
            test fast without changing production behavior.
        on_trip: optional callback invoked with each `GovernorTripRecord` the
            moment it happens (before the next rung is attempted), so a
            caller (the eventual B5 telemetry loop) can start recording
            misses without waiting for the whole ladder to resolve.
        **isolated_kwargs: forwarded to `run_pipeline_isolated` at every
            rung (e.g. `staging_dir`, `output_dir`, `timeout_s`,
            `engine_version`). `on_spawn` and `execution_mode` are owned by
            this function and MUST NOT be passed here (they would collide
            with the monitor wiring / the ladder's own rung selection).

    Returns:
        A `GovernorResult`. `outcome="completed"` carries the successful
        `IsolatedRunResult` and which rung produced it; `outcome="exhausted"`
        carries `diagnostic` (naming every route tried and why) -- never a
        bare subprocess exit code. `result` is `None` for a ladder-exhausted
        failure (`trips` holds the per-rung detail); the flag-off exhausted
        path (LOW-1) instead carries the one attempted rung's own
        `IsolatedRunResult` there, since it has no `trips`.

    Raises:
        ValueError: `on_spawn`, `execution_mode`, or `isolate=False` (MED-2 --
            the governor cannot supervise an in-process run) was passed in
            `isolated_kwargs`; `ladder` is empty; `budget_bytes` is not a
            positive int; `hard_threshold_fraction` is not in `(0, 1]`; or
            `poll_interval_s` is not > 0.
    """
    require_bool("use_runtime_governor", use_runtime_governor)
    _validate_call(ladder, budget_bytes, hard_threshold_fraction, poll_interval_s, isolated_kwargs)
    # TB-1 fix #3 (docs/plans/2026-07-12-track-b-completion-program.md's "two
    # defects" section, #54): forward this job's slot budget to run_pipeline's
    # out_of_core_budget_bytes so the out-of-core route's DuckDB memory_limit
    # is sized from it, not `resolve_budget(None)`'s 25%-of-host-RAM fallback
    # (`out_of_core._budget._HOST_RAM_FRACTION`) -- wrong on any non-32GB host
    # and under concurrent slots.
    isolated_kwargs = _isolated_kwargs_with_budget(budget_bytes, isolated_kwargs)

    if not use_runtime_governor:
        return _run_flag_off(config, sources, ladder[0], isolated_kwargs)

    hard_threshold_bytes = int(budget_bytes * hard_threshold_fraction)
    trips: list[GovernorTripRecord] = []

    for index, route in enumerate(ladder):
        result, monitor = _run_one_rung(
            config,
            sources,
            route,
            hard_threshold_bytes,
            poll_interval_s,
            isolated_kwargs,
        )

        if result.outcome == "completed":
            return GovernorResult(
                outcome="completed", final_route=route, result=result, trips=tuple(trips)
            )

        next_route = _next_rung(ladder, index)

        if result.outcome == "crashed" and _is_route_ineligible_error(route, result.error):
            trip = GovernorTripRecord(
                route=route,
                budget_bytes=budget_bytes,
                observed_peak_mb=None,
                trip_kind="route_ineligible",
                reroute_to=next_route,
                error=result.error,
            )
        elif result.outcome == "crashed":
            # A genuine, non-memory, non-eligibility failure: rerouting would
            # just run the same bug on a different route (or mask it behind
            # a route change). The governor's job is OOM avoidance, not
            # general fault-tolerance -- surface it immediately rather than
            # burn the rest of the ladder on a job that was never going to
            # complete.
            trip = GovernorTripRecord(
                route=route,
                budget_bytes=budget_bytes,
                observed_peak_mb=None,
                trip_kind="crashed",
                reroute_to=None,
                error=result.error,
            )
            trips.append(trip)
            if on_trip is not None:
                on_trip(trip)
            return GovernorResult(
                outcome="exhausted",
                final_route=None,
                result=None,
                trips=tuple(trips),
                diagnostic=_exhausted_diagnostic(tuple(trips), budget_bytes),
            )
        else:
            if result.outcome != "oom_killed":
                # IsolatedRunOutcome is a 3-value Literal ("completed",
                # "oom_killed", "crashed"); both other values are handled
                # above, so this branch is unreachable in practice -- a
                # raised error (not a bare `assert`, which `python -O`
                # strips) if a future outcome value is ever added without
                # updating this function.
                raise AssertionError(
                    f"run_job_with_governor: unexpected IsolatedRunResult.outcome={result.outcome!r}"
                )
            governor_tripped = monitor is not None and monitor.tripped
            observed_peak_mb = _observed_peak_mb(monitor, result)
            trip = GovernorTripRecord(
                route=route,
                budget_bytes=budget_bytes,
                observed_peak_mb=observed_peak_mb,
                trip_kind="governor_kill" if governor_tripped else "self_oom",
                reroute_to=next_route,
                error=result.error,
            )

        trips.append(trip)
        if on_trip is not None:
            on_trip(trip)

        if next_route is None:
            return GovernorResult(
                outcome="exhausted",
                final_route=None,
                result=None,
                trips=tuple(trips),
                diagnostic=_exhausted_diagnostic(tuple(trips), budget_bytes),
            )
        # else: loop continues to the next rung -- this IS the reroute.

    # Unreachable (the loop above always returns on its last iteration), kept
    # only so mypy sees an exhaustive return on every path.
    return GovernorResult(
        outcome="exhausted",
        final_route=None,
        result=None,
        trips=tuple(trips),
        diagnostic=_exhausted_diagnostic(tuple(trips), budget_bytes),
    )


def _isolated_kwargs_with_budget(
    budget_bytes: int, isolated_kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Default `out_of_core_budget_bytes` to this job's slot budget (TB-1 fix #3).

    Without this, `run_pipeline`'s `out_of_core_budget_bytes` stays `None`
    at every rung, so the out-of-core route's `resolve_budget(None)` sizes
    DuckDB's `memory_limit` from 25% of detected HOST/cgroup RAM
    (`out_of_core._budget._HOST_RAM_FRACTION`) instead of this job's actual
    slot share -- wrong on any non-32GB host and under concurrent slots
    (docs/plans/2026-07-12-track-b-completion-program.md's "two defects"
    section, #54). Applied to EVERY rung (unconditionally, before the
    ladder loop) since every route shares the same slot budget by this
    function's own existing contract (see `budget_bytes`'s docstring
    above): a caller with per-route budgets already calls this once per
    rung itself instead of using the ladder.

    Never overrides an EXPLICIT `out_of_core_budget_bytes` the caller
    already set in `isolated_kwargs` (e.g. one that deliberately differs
    from the governor's own RSS-kill threshold) -- this only supplies the
    default, matching every other knob in this module's "additive, opt-in"
    discipline.
    """
    if "out_of_core_budget_bytes" in isolated_kwargs:
        return isolated_kwargs
    return {**isolated_kwargs, "out_of_core_budget_bytes": budget_bytes}


def _validate_call(
    ladder: tuple[GovernorRoute, ...],
    budget_bytes: int,
    hard_threshold_fraction: float,
    poll_interval_s: float,
    isolated_kwargs: dict[str, Any],
) -> None:
    if not ladder:
        raise ValueError("run_job_with_governor: ladder must be non-empty.")
    if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or budget_bytes <= 0:
        raise ValueError(
            f"run_job_with_governor: budget_bytes must be a positive int, got {budget_bytes!r}."
        )
    # LOW-2: 0 trips the kill immediately (VmRSS is never negative); above 1
    # can never be crossed by a live process -- neither is a usable threshold.
    if (
        isinstance(hard_threshold_fraction, bool)
        or not isinstance(hard_threshold_fraction, (int, float))
        or not (0 < hard_threshold_fraction <= 1)
    ):
        raise ValueError(
            "run_job_with_governor: hard_threshold_fraction must be in (0, 1], "
            f"got {hard_threshold_fraction!r}."
        )
    # LOW-2: non-positive either busy-loops the monitor thread (0) or is
    # nonsensical (negative) -- neither is a usable sample cadence.
    if (
        isinstance(poll_interval_s, bool)
        or not isinstance(poll_interval_s, (int, float))
        or poll_interval_s <= 0
    ):
        raise ValueError(
            f"run_job_with_governor: poll_interval_s must be > 0, got {poll_interval_s!r}."
        )
    collisions = {"on_spawn", "execution_mode"} & isolated_kwargs.keys()
    if collisions:
        raise ValueError(
            f"run_job_with_governor owns {sorted(collisions)} -- do not pass "
            f"{'them' if len(collisions) > 1 else 'it'} via isolated_kwargs."
        )
    # MED-2: isolate=False runs the job IN this process -- on_spawn is never
    # called, so no monitor ever attaches, and a real OOM kills the driver
    # itself (the exact rc137 this module exists to prevent). Fail closed.
    if isolated_kwargs.get("isolate") is False:
        raise ValueError(
            "run_job_with_governor: isolate=False cannot be combined with the "
            "runtime governor -- no child pid is ever spawned, so on_spawn is "
            "never called and no monitor attaches. Leave isolate at its "
            "default (True), or call run_pipeline_isolated directly if you "
            "deliberately want the unsupervised in-process fallback."
        )


def _run_flag_off(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    route: GovernorRoute,
    isolated_kwargs: dict[str, Any],
) -> GovernorResult:
    result = run_pipeline_isolated(config, sources, execution_mode=route, **isolated_kwargs)
    if result.outcome == "completed":
        return GovernorResult(outcome="completed", final_route=route, result=result, trips=())
    return GovernorResult(
        outcome="exhausted",
        final_route=None,
        # LOW-1: surface the underlying result rather than dropping it -- no
        # trip history exists here to hold this diagnostic, so the one
        # rung's own result is the only place a caller can recover it from.
        result=result,
        trips=(),
        diagnostic=(
            f"route={route} did not complete (outcome={result.outcome}); "
            "use_runtime_governor=False, so no monitor was attached and no reroute "
            f"was attempted. error={result.error}"
        ),
    )


def _run_one_rung(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    route: GovernorRoute,
    hard_threshold_bytes: int,
    poll_interval_s: float,
    isolated_kwargs: dict[str, Any],
) -> tuple[IsolatedRunResult, _RssMonitor | None]:
    monitor_holder: list[_RssMonitor] = []

    def _on_spawn(pid: int) -> None:
        monitor = _RssMonitor(pid, hard_threshold_bytes, poll_interval_s)
        monitor_holder.append(monitor)
        monitor.start()

    try:
        result = run_pipeline_isolated(
            config, sources, execution_mode=route, on_spawn=_on_spawn, **isolated_kwargs
        )
    finally:
        # Unconditional: a monitor still polling after its child has already
        # been reaped is a leaked thread, whether the run completed, crashed,
        # or was killed by the monitor itself (in which case `stop` is a
        # cheap no-op join on an already-finished thread).
        if monitor_holder:
            monitor_holder[0].stop()

    return result, (monitor_holder[0] if monitor_holder else None)


def _observed_peak_mb(monitor: _RssMonitor | None, result: IsolatedRunResult) -> float | None:
    if monitor is not None and monitor.peak_observed_bytes > 0:
        return monitor.peak_observed_bytes / (1024 * 1024)
    return result.peak_rss_mb

"""Host- and cgroup-aware memory budget and spill-disk guard for the
out-of-core route.

One knob bounds the route's memory: `resolve_budget` turns an explicit byte
budget (or, absent one, a conservative fraction of detected host RAM) into the
DuckDB `memory_limit` string `connect_duckdb` already accepts and a
`batch_rows` for the streaming passes. The sizing follows established
practice: DuckDB itself bounds its buffer manager with `memory_limit` and
spills operator state to `temp_directory` when the limit is hit (DuckDB
"Memory Management" / "Tuning Workloads" docs; its own default is 80% of RAM),
and the conservative-fraction default mirrors the heap-fraction conventions
long used by managed runtimes and databases (the JVM's default max heap of 1/4
of physical RAM via MaxRAMPercentage=25; PostgreSQL's canonical ~25%-of-RAM
shared_buffers guidance). A fraction, not the whole host, because the route's
Python/Arrow batch buffers and the OS page cache live outside DuckDB's
accounting.

`batch_rows` scales with the budget but is floored (a degenerate batch size
would grind the DuckDB round trips to a halt) and capped at the route's
pinned default (`_join._JOIN_BATCH_ROWS`), so a large host never silently
changes behavior versus the constant the parity suite pins, and no batch is
ever sized by table cardinality.

Spill is bounded too: `check_temp_disk_budget` measures the on-disk footprint
under the route's `temp_dir` (relation/join staging plus DuckDB spill) and
fails closed with a coded error before the disk fills, the same guard-rail
role as DuckDB's `max_temp_directory_size` setting -- `_pipeline_route_exec.
run_out_of_core_route` threads a `shutil.disk_usage`-derived budget into
`run_fk_out_of_core` so this cap is enforced on the pipeline path (OOC-D).
`check_disk_spill_preflight` is the forward-looking counterpart: given a
*predicted* footprint, it checks free disk up front rather than after the
fact. The predicted-footprint estimator + the routing call site (OOC-D, per
the OOM-avoidance routing redesign §3.6) now live in the sibling
`_spill_estimate.py` module (`predict_ooc_disk_bytes` /
`enforce_ooc_disk_preflight`), a conservative two-term (spill + output) upper
bound, split out to hold this module's own LOC cap.

The effective ceiling `resolve_budget` fractions prefers a cgroup memory
limit over raw host RAM (`detect_effective_memory_bytes`): a process almost
never gets the whole host to itself in production (containers, k8s pods,
systemd slices all cap memory below the physical total), and the cgroup
limit -- not host RAM -- is the number the kernel OOM-kills against.
`detect_cgroup_memory_limit_bytes` reads cgroup v2's `memory.max` (walking
the leaf..root hierarchy and taking the min, since a parent's limit bounds
every descendant) or, absent v2, cgroup v1's `memory.limit_in_bytes`, and
returns `None` -- triggering the host-RAM fallback -- only when neither
yields a real number. `resolve_budget` also accepts `reserved_bytes`, the
sum already charged to co-running slots under the same cgroup, so its
return is this job's *slot* share of the ceiling, not the whole cgroup.

`resolve_budget`'s conservative 0.25 fraction is deliberately UNCHANGED and
shared with a second consumer beyond the out-of-core route itself:
`_pipeline_routing_signals.py`'s `resolve_full_frame_fits_estimate` /
`resolve_probe_recovery` price the FULL-FRAME admission decision against this
same `budget.budget_bytes` (`decide_execution_route`'s byte-estimate routing).
That decision must stay conservative -- an under-predicted full-frame estimate
OOMs hard, and the runtime governor that would reroute a job off a bad
admission is not wired into `run_pipeline` (only platform-invoked runs get
one) -- so widening `resolve_budget`'s own fraction would silently loosen
full-frame admission too, not just the out-of-core route it was meant to fix.
See `resolve_ooc_memory_limit` below for the OOC-only remedy, kept decoupled
from this function on purpose.

OUT-OF-CORE MEMORY SIZING (`resolve_ooc_memory_limit`, separate from
`resolve_budget` above): a real 100M-row cloud benchmark measured DuckDB
starved to ~3.8 GB of a 16 GB process cap under `resolve_budget`'s 0.25
fraction, while ~11 GB sat idle and the 300 GB spill disk was never touched --
the job OOMed *inside* DuckDB with plenty of both RAM and disk to spare. That
starvation is safe to fix ONLY for DuckDB's own `memory_limit`: unlike a
full-frame admission miss (a hard OOM with no recovery), an out-of-core
`memory_limit` that turns out too tight just SPILLS the overflow to
`temp_directory` (DuckDB's own larger-than-memory behavior, bounded
separately here by `check_temp_disk_budget`) -- generous sizing has a soft
failure mode where full-frame admission has a hard one, so only the OOC path
gets the generous model.

`resolve_ooc_memory_limit`'s model is subtractive rather than a fixed
fraction: `budget = ceiling - reserve`, where
`reserve = max(_OOC_RESERVE_FRACTION * ceiling, _OOC_RESERVE_FLOOR_BYTES)`
covers the Python interpreter, Arrow's per-batch buffers, and OS overhead
that all live OUTSIDE DuckDB's own accounting (`connect_duckdb`'s
`memory_limit` bounds only DuckDB's buffer manager, per DuckDB's "Memory
Management" / "Tuning Workloads" guidance on larger-than-memory execution).
DuckDB gets the REST of the ceiling -- typically 75-90%+ on hosts bigger than
the reserve floor -- rather than a quarter of it, and still spills to
`temp_directory` whenever a query's working set exceeds even that generous
share.

CONCURRENCY (a second, independent divisor `resolve_ooc_memory_limit` alone
applies): `connect_duckdb` opens a FRESH `:memory:` DuckDB instance per call,
and `memory_limit` bounds only THAT ONE instance -- DuckDB has no
cross-instance accounting. `_runner.py` can hold several instances open at
once: `_stream_table` opens one `ChildFkBatchJoiner` connection PER INCOMING
FK EDGE up front (`_batch_join.py`). On the sink path, joiners close after
the rewrite stream drains (via `on_stream_consumed` in `_emit.py`), before
`_relation.py::build_parent_key_relation` opens for OUTGOING edges; on the
resident path (no sink), joiners stay open during the build (both live in the
same `_stream_table` try block). So the sink path peaks at max(incoming_edges,
1), the resident path at incoming_edges + 1. `_max_concurrent_ooc_instances`
computes the resident peak (incoming_edges + 1) as the divisor for both paths:
it is exact for residents and conservative (over-provisions) for sinks. Only
one table streams at a time (`run_fk_out_of_core`'s per-table loop is
sequential, never concurrent), so the run's ceiling is the max over every
table. But incoming-edge fan-in is a property of the PLAN's relationship
graph, which this module never sees -- both budget functions take only byte
counts, by design, so neither depends on `decoy_engine.relationships` or
`decoy_engine.plan`. No exact bound is derivable here without that dependency,
so `max_concurrent_instances` is an explicit, documented, CONSERVATIVE default
(`_DEFAULT_MAX_CONCURRENT_DUCKDB_INSTANCES`) rather than a computed one: real
FK schemas rarely fan many incoming edges into one table (the 100M-row cloud
benchmark's parent->child->grandchild chain peaks at 2 -- one joiner plus one
relation build), so the default trades a little headroom for safety on any
schema up to that fan-in without a caller having to know the graph. A caller
that DOES have graph visibility (and a wider-fan-in schema) should pass
`max_concurrent_instances` explicitly, sized from its own edge count. The
resolved `memory_limit` is `budget_bytes // max_concurrent_instances` (floored
at `_MIN_BUDGET_BYTES` per instance), so the SUM of every live instance's cap
stays within `budget_bytes`, which itself already excludes the reserve above.
`batch_rows` scales with the (undivided) budget, not `memory_limit`: it
bounds Python/Arrow-side batch buffers, already carved out by the reserve,
not DuckDB's own accounting, so it is not further divided by concurrency.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._join import _JOIN_BATCH_ROWS

# Conservative auto-detect fraction of the effective memory ceiling (JVM
# default heap fraction).
_HOST_RAM_FRACTION = 0.25

# Never budget below this even on a tiny or misdetected host: DuckDB plus the
# route's per-edge Arrow buffers need real working room to make progress.
_MIN_BUDGET_BYTES = 64 * 1024 * 1024

# One streamed batch should stay a small slice of the budget: several bounded
# copies of a batch are live at once per edge (raw batch, masked batch, join
# staging, sink write), so the divisor keeps their sum well inside the budget
# at the ~1 KiB/row working size of the measured wide-string chains.
_BATCH_BYTES_PER_ROW = 32 * 1024
_MIN_BATCH_ROWS = 1_024
_MAX_BATCH_ROWS = _JOIN_BATCH_ROWS

_PROC_MEMINFO = Path("/proc/meminfo")

# cgroup filesystem locations. Module-level so tests can monkeypatch them to
# a fake tree instead of depending on the real host's cgroup setup.
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
_CGROUP_V2_MOUNT = Path("/sys/fs/cgroup")
_CGROUP_V1_MEMORY_MOUNT = Path("/sys/fs/cgroup/memory")

# cgroup v1 has no literal "unlimited" string like v2's "max"; it instead
# sets memory.limit_in_bytes to LLONG_MAX rounded down to the page size
# (cgroup-v1/memory.txt). That rounding is page-size-specific -- 4 KiB on
# x86-64, up to 64 KiB on ppc64le / some arm64 -- so a single 4 KiB constant
# would keep the 64 KiB-page sentinel as a real ~8 EiB limit and let a job run
# uncapped. Treat any value within one max-plausible page (64 KiB) of LLONG_MAX
# as "no limit set at this level"; no real limit is ever set that high.
_CGROUP_V1_UNLIMITED_THRESHOLD = (1 << 63) - 1 - 65_536

# We deliberately do NOT add memory.swap.max (v2) / memory.memsw.limit_in_bytes
# (v1) to widen the ceiling. Swap raises the kernel's kill point, but only by
# letting pages go to disk mid-run -- already a degraded state a budget
# should route away from, not toward. Ignoring it is the safe direction: the
# limit this module reports can only be tighter than the true kill line,
# never looser.

# --- resolve_ooc_memory_limit only (module docstring's "OUT-OF-CORE MEMORY
# SIZING" section): kept separate from _HOST_RAM_FRACTION above because that
# fraction is shared with the router's full-frame admission pricing, which
# must stay conservative. These two constants size the SUBTRACTIVE reserve
# (ceiling - reserve, not ceiling * fraction) that leaves DuckDB most of the
# ceiling instead of a quarter of it.
_OOC_RESERVE_FRACTION = 0.2
_OOC_RESERVE_FLOOR_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# Conservative stand-in for "how many DuckDB instances can be live at once
# during one out-of-core run" (see the module docstring's CONCURRENCY
# section) -- this module has no visibility into the plan's relationship
# graph, so the true incoming-edge fan-in per table cannot be computed here.
# A caller that DOES know the graph should pass max_concurrent_instances
# explicitly instead of relying on this default.
_DEFAULT_MAX_CONCURRENT_DUCKDB_INSTANCES = 4


@dataclass(frozen=True)
class OutOfCoreBudget:
    """A resolved memory budget for one out-of-core run."""

    budget_bytes: int
    memory_limit: str
    batch_rows: int


def detect_host_memory_bytes() -> int:
    """Total physical host RAM in bytes, without a psutil dependency.

    POSIX sysconf first (page size times physical pages), /proc/meminfo as the
    fallback; fails closed with a coded error where neither is available, so a
    caller never proceeds on a silently-invented budget.
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        if page_size > 0 and phys_pages > 0:
            return page_size * phys_pages
    except (AttributeError, OSError, ValueError):
        pass
    try:
        for line in _PROC_MEMINFO.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    raise ExecutionError(
        code="out_of_core_memory_detection_failed",
        message=(
            "host RAM could not be detected (no sysconf, no /proc/meminfo); "
            "pass an explicit budget_bytes instead."
        ),
    )


def _read_cgroup_bytes_value(path: Path) -> int | None:
    """Parse one cgroup byte-count file. `None` on missing/unlimited/unparseable.

    cgroup v2 spells "unlimited" as the literal string "max"; the file is
    otherwise a single integer, never a human-readable size like "4G"
    (cgroup-v2.txt). v1's `memory.limit_in_bytes` is always numeric, so this
    reader is shared by both -- the v1 "unlimited" sentinel is filtered by
    the caller, not here, since only v1 has one.
    """
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _cgroup_ancestor_dirs(leaf: Path, root: Path) -> list[Path]:
    """`leaf` plus every directory from `leaf` up to (and including) `root`.

    A cgroup's effective limit is the min of its own file and every
    ancestor's, because a parent's memory.max/limit_in_bytes bounds all of
    its descendants regardless of what a child sets -- nesting is shallow in
    practice (a handful of levels for systemd/container runtimes), so
    reading each level is cheap.
    """
    dirs = [leaf]
    current = leaf
    while current != root and root in current.parents:
        current = current.parent
        dirs.append(current)
    return dirs


def _cgroup_v2_effective_max_bytes(leaf: Path, root: Path) -> int | None:
    """Min `memory.max` across `leaf..root`; `None` if every level is
    unlimited, unreadable, or missing."""
    values = [
        value
        for directory in _cgroup_ancestor_dirs(leaf, root)
        if (value := _read_cgroup_bytes_value(directory / "memory.max")) is not None
    ]
    return min(values) if values else None


def _cgroup_v1_effective_limit_bytes(leaf: Path, root: Path) -> int | None:
    """Min `memory.limit_in_bytes` across `leaf..root`, excluding the v1
    unlimited sentinel at each level; `None` if none is a real limit."""
    values = [
        value
        for directory in _cgroup_ancestor_dirs(leaf, root)
        if (value := _read_cgroup_bytes_value(directory / "memory.limit_in_bytes")) is not None
        and value < _CGROUP_V1_UNLIMITED_THRESHOLD
    ]
    return min(values) if values else None


def _cgroup_leaf_dir(mount: Path, proc_self_cgroup: Path, *, controller: str | None) -> Path:
    """This process's leaf cgroup directory under `mount`.

    Parsed from `/proc/self/cgroup`, whose lines are `hierarchy-ID:controller-
    list:path` (cgroups(7)). v2's unified hierarchy has ID "0" and an empty
    controller list; v1 hierarchies have a real ID and a comma-separated
    controller list, so `controller=None` selects the v2 line and
    `controller="memory"` selects the v1 memory controller's line. Falls
    back to `mount` itself (the root cgroup) when the file is unreadable or
    no matching line is found, e.g. under a plain (non-cgroup) sandbox.
    """
    try:
        text = proc_self_cgroup.read_text()
    except OSError:
        return mount
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy_id, controllers, cgroup_path = parts
        is_v2_line = hierarchy_id == "0" and controllers == ""
        matches = is_v2_line if controller is None else controller in controllers.split(",")
        if matches:
            rel = cgroup_path.strip().lstrip("/")
            return (mount / rel) if rel else mount
    return mount


def detect_cgroup_memory_limit_bytes() -> int | None:
    """The effective cgroup memory ceiling for this process, or `None` when
    no real cgroup limit is present (no cgroup, or every level unlimited).

    Tries v2 (`memory.max`) first, then v1 (`memory.limit_in_bytes`) --
    v2 is the modern unified hierarchy and, when mounted, is authoritative;
    v1 is only consulted when v2 yields nothing. Returning `None` rather
    than blending the two lets `detect_effective_memory_bytes` make an
    explicit choice (cgroup vs. host RAM) instead of this function silently
    picking one.
    """
    v2_leaf = _cgroup_leaf_dir(_CGROUP_V2_MOUNT, _PROC_SELF_CGROUP, controller=None)
    v2_limit = _cgroup_v2_effective_max_bytes(v2_leaf, _CGROUP_V2_MOUNT)
    if v2_limit is not None:
        return v2_limit
    v1_leaf = _cgroup_leaf_dir(_CGROUP_V1_MEMORY_MOUNT, _PROC_SELF_CGROUP, controller="memory")
    return _cgroup_v1_effective_limit_bytes(v1_leaf, _CGROUP_V1_MEMORY_MOUNT)


def detect_effective_memory_bytes() -> int:
    """The memory ceiling this process actually runs under: a cgroup limit
    when one is present, else physical host RAM.

    A cgroup limit is the real OOM-kill point in production (containers, k8s
    pods, systemd slices); host RAM answers a different question ("how much
    physical memory exists on the box") that overshoots badly inside any
    cgroup tighter than the host. Falling back to host RAM only when no
    cgroup limit is present keeps bare-metal/dev/CI behavior identical to
    before this function existed.
    """
    cgroup_limit = detect_cgroup_memory_limit_bytes()
    if cgroup_limit is not None:
        return cgroup_limit
    return detect_host_memory_bytes()


def resolve_budget(
    budget_bytes: int | None = None,
    *,
    reserved_bytes: int = 0,
) -> OutOfCoreBudget:
    """Resolve one memory budget into DuckDB and batch-sizing knobs.

    With no explicit budget, a conservative fraction of the effective memory
    ceiling (cgroup limit, else host RAM -- see `detect_effective_memory_bytes`)
    is used. `reserved_bytes` -- the platform's charge for co-running slots
    under the same cgroup -- is subtracted so the return is this job's SLOT
    budget, not the whole cgroup/host figure; it defaults to 0, so existing
    callers are unaffected. The result is floored so a tiny or misdetected
    budget, or one nearly exhausted by `reserved_bytes`, never yields a
    zero/absurd limit, and `batch_rows` is clamped to the route's pinned
    default at the top end.
    """
    if budget_bytes is not None and budget_bytes <= 0:
        raise ExecutionError(
            code="out_of_core_budget_invalid",
            message=f"budget_bytes must be positive, got {budget_bytes}.",
        )
    if reserved_bytes < 0:
        raise ExecutionError(
            code="out_of_core_reserved_bytes_invalid",
            message=f"reserved_bytes must be >= 0, got {reserved_bytes}.",
        )
    if budget_bytes is None:
        budget_bytes = int(detect_effective_memory_bytes() * _HOST_RAM_FRACTION)
    budget_bytes = max(budget_bytes - reserved_bytes, _MIN_BUDGET_BYTES)
    batch_rows = min(max(budget_bytes // _BATCH_BYTES_PER_ROW, _MIN_BATCH_ROWS), _MAX_BATCH_ROWS)
    # MiB count with a decimal "MB" suffix: DuckDB reads "MB" as base-10, so the
    # effective limit lands slightly BELOW budget_bytes. That is the safe
    # direction for a cap (never over the budget), and it keeps the string a
    # round MiB figure.
    return OutOfCoreBudget(
        budget_bytes=budget_bytes,
        memory_limit=f"{budget_bytes // (1024 * 1024)}MB",
        batch_rows=batch_rows,
    )


def resolve_ooc_memory_limit(
    budget_bytes: int | None = None,
    *,
    reserved_bytes: int = 0,
    max_concurrent_instances: int | None = None,
) -> OutOfCoreBudget:
    """Resolve the out-of-core route's OWN generous DuckDB memory sizing.

    Deliberately NOT `resolve_budget`: that function's conservative 0.25
    fraction is shared with the pipeline router's full-frame admission
    pricing (`_pipeline_routing_signals.py`), which must stay conservative
    because an under-predicted full-frame estimate OOMs hard with no
    governor reroute in `run_pipeline`. This function is for the ONE place
    a generous budget is safe: DuckDB's own `memory_limit`, where an
    under-sized cap just spills the overflow to `temp_directory` instead of
    OOMing (bounded separately by `check_temp_disk_budget`). See the module
    docstring's "OUT-OF-CORE MEMORY SIZING" and "CONCURRENCY" sections for
    the full reasoning this implements.

    With no explicit `budget_bytes`, the resolved budget is `ceiling -
    reserve` (subtractive, not a fraction of the ceiling), where `reserve =
    max(_OOC_RESERVE_FRACTION * ceiling, _OOC_RESERVE_FLOOR_BYTES)`. An
    explicit `budget_bytes` (e.g. a caller-computed process-cap share) skips
    the ceiling detection entirely, matching `resolve_budget`'s own
    explicit-budget contract. `reserved_bytes` (co-running slot charges) and
    the `_MIN_BUDGET_BYTES` floor behave identically to `resolve_budget`.

    `memory_limit` is then this resolved budget divided by
    `max_concurrent_instances` (default `_DEFAULT_MAX_CONCURRENT_DUCKDB_
    INSTANCES` when not given). The division is a strict floor: the SUM of
    every live instance's cap is guaranteed <= `budget_bytes`, so DuckDB
    never over-subscribes the budget across the connections `_runner.py`
    holds open at once. This is deliberately NOT floored per instance at
    `_MIN_BUDGET_BYTES` -- that upward floor would push the sum OVER
    `budget_bytes` on a small budget split across high fan-in, the exact
    over-subscription the invariant forbids -- so a tight budget yields a
    genuinely smaller per-instance cap rather than a silently over-committed
    one (the overflow spills, per this function's whole rationale). The lone
    exception is a sub-1-MiB split (only reachable at >64-way FK fan-in on a
    near-floor ~64 MiB budget), where DuckDB rejects a "0MB" limit outright,
    so the string floors at "1MB" and the sum can nominally exceed the
    budget; in that regime the cap is moot because spilling, not the cap,
    carries the run.

    `batch_rows` scales off the UNDIVIDED `budget_bytes`, NOT the per-instance
    `memory_limit`: it bounds the Python/Arrow batch buffers of the single
    table `_runner.py` streams at a time (its per-table loop is sequential),
    which are not DuckDB-accounted and not multiplied by the concurrent DuckDB
    instance count, so dividing it by concurrency would needlessly shrink it.
    `OutOfCoreBudget.budget_bytes` on the return value is the undivided total
    (for diagnostics/consistency with `resolve_budget`'s shape); the
    concurrency division is folded into `memory_limit` alone.
    """
    if budget_bytes is not None and budget_bytes <= 0:
        raise ExecutionError(
            code="out_of_core_budget_invalid",
            message=f"budget_bytes must be positive, got {budget_bytes}.",
        )
    if reserved_bytes < 0:
        raise ExecutionError(
            code="out_of_core_reserved_bytes_invalid",
            message=f"reserved_bytes must be >= 0, got {reserved_bytes}.",
        )
    if max_concurrent_instances is not None and max_concurrent_instances < 1:
        raise ExecutionError(
            code="out_of_core_concurrency_invalid",
            message=(f"max_concurrent_instances must be >= 1, got {max_concurrent_instances}."),
        )
    if max_concurrent_instances is None:
        max_concurrent_instances = _DEFAULT_MAX_CONCURRENT_DUCKDB_INSTANCES
    if budget_bytes is None:
        ceiling = detect_effective_memory_bytes()
        reserve = max(int(ceiling * _OOC_RESERVE_FRACTION), _OOC_RESERVE_FLOOR_BYTES)
        budget_bytes = ceiling - reserve
    budget_bytes = max(budget_bytes - reserved_bytes, _MIN_BUDGET_BYTES)
    # batch_rows off the UNDIVIDED budget: it sizes Python/Arrow batch buffers
    # for the ONE table streamed at a time (the runner's per-table loop is
    # sequential), which are not DuckDB-accounted and not scaled by the
    # concurrent-instance count, so the concurrency divisor below must not
    # touch it.
    batch_rows = min(max(budget_bytes // _BATCH_BYTES_PER_ROW, _MIN_BATCH_ROWS), _MAX_BATCH_ROWS)
    # Strict floor division: per_instance * max_concurrent <= budget_bytes, so
    # the SUM of every live DuckDB instance's cap stays within the budget. NOT
    # floored up at _MIN_BUDGET_BYTES -- that would over-subscribe the budget
    # (Dennis-1); a tight budget takes a smaller cap and spills instead. The
    # only guard is against a sub-1-MiB split producing a "0MB" string DuckDB
    # rejects outright (reachable only at >64-way fan-in on a ~64 MiB budget).
    per_instance_mib = max(1, budget_bytes // max_concurrent_instances // (1024 * 1024))
    # Same MiB-with-decimal-suffix rounding-down rationale as resolve_budget:
    # DuckDB reads "MB" as base-10, so the effective limit lands slightly below
    # the MiB byte count -- the safe direction, never over the per-instance cap.
    return OutOfCoreBudget(
        budget_bytes=budget_bytes,
        memory_limit=f"{per_instance_mib}MB",
        batch_rows=batch_rows,
    )


def temp_disk_bytes(temp_dir: Path) -> int:
    """Current on-disk footprint under `temp_dir`, in bytes.

    Files evicted between listing and stat are skipped: an eviction shrinks
    the footprint, so skipping it never hides an over-budget state.
    """
    total = 0
    if temp_dir.exists():
        for dirpath, _dirnames, filenames in os.walk(temp_dir):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    return total


def check_temp_disk_budget(temp_dir: Path, *, max_bytes: int) -> int:
    """Fail closed once the spill footprint under `temp_dir` exceeds a budget.

    Returns the current footprint when under budget so a caller can log or
    poll it. Point-in-time by design: a runner calls it at its natural batch
    or table boundaries (no watcher thread), so a spike strictly between two
    checks is caught at the next boundary rather than the exact byte.
    """
    used = temp_disk_bytes(temp_dir)
    if used > max_bytes:
        raise ExecutionError(
            code="out_of_core_temp_disk_exceeded",
            message=(
                f"out-of-core temp disk footprint {used} bytes exceeds the "
                f"{max_bytes}-byte budget under {temp_dir}."
            ),
        )
    return used


@dataclass(frozen=True)
class DiskSpillPreflight:
    """Result of checking whether a scratch path's free disk can absorb a
    predicted spill."""

    ok: bool
    free_bytes: int
    predicted_bytes: int
    headroom_bytes: int


def _nearest_existing_ancestor(path: Path) -> Path:
    """The closest directory in `path`'s ancestry that actually exists.

    Disk usage is a filesystem-level property of whatever volume `path`
    lands on; the route's scratch directory is created lazily by the runner,
    so a preflight check must not require it (or fabricate it) up front.
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or "/")


def check_disk_spill_preflight(path: Path, *, predicted_spill_bytes: int) -> DiskSpillPreflight:
    """Pass/fail check: does `path`'s filesystem have enough free space for
    a predicted spill of `predicted_spill_bytes`?

    Standalone and estimator-agnostic by design -- this function only knows
    how to compare a byte count to free disk; `_spill_estimate.py`'s
    `predict_ooc_disk_bytes` supplies the estimate and `enforce_ooc_disk_
    preflight` is routing's one call site (OOC-D, the OOM-avoidance routing
    redesign §3.6's "reject only when spill disk is insufficient"), wired
    into `_pipeline_routing_signals.resolve_execution_route`. `shutil.
    disk_usage` is used over a manual statvfs call: it already normalizes
    the platform differences and reports free bytes available to the
    process, the number a spill actually competes for.
    """
    if predicted_spill_bytes < 0:
        raise ExecutionError(
            code="out_of_core_predicted_spill_bytes_invalid",
            message=f"predicted_spill_bytes must be >= 0, got {predicted_spill_bytes}.",
        )
    usage = shutil.disk_usage(_nearest_existing_ancestor(path))
    headroom = usage.free - predicted_spill_bytes
    return DiskSpillPreflight(
        ok=headroom >= 0,
        free_bytes=usage.free,
        predicted_bytes=predicted_spill_bytes,
        headroom_bytes=headroom,
    )


__all__ = [
    "DiskSpillPreflight",
    "OutOfCoreBudget",
    "check_disk_spill_preflight",
    "check_temp_disk_budget",
    "detect_cgroup_memory_limit_bytes",
    "detect_effective_memory_bytes",
    "detect_host_memory_bytes",
    "resolve_budget",
    "resolve_ooc_memory_limit",
    "temp_disk_bytes",
]

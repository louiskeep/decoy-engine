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
role as DuckDB's `max_temp_directory_size` setting. `check_disk_spill_preflight`
is the forward-looking counterpart: given a *predicted* spill size, it checks
free disk up front rather than after the fact. It is additive and not yet
wired into routing -- future work replaces the row-count hard-reject with
"reject only when spill disk is insufficient", per the OOM-avoidance routing
redesign (docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §3.6).

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

    Additive and standalone -- not wired into routing yet. This is the future
    basis for "reject only when spill disk is insufficient" (the
    OOM-avoidance routing redesign §3.6): once a predicted-spill estimator
    exists, a caller checks this before committing to the out-of-core route
    instead of discovering a full disk mid-spill. `shutil.disk_usage` is
    used over a manual statvfs call: it already normalizes the platform
    differences and reports free bytes available to the process, the number
    a spill actually competes for.
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
    "temp_disk_bytes",
]

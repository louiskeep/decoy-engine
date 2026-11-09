"""Usable CPU count for this process, and the native thread clamp it feeds (RUN-STREAM E1b).

`detect_usable_cpus` is the platform's `api/cgroup_cpu.py` (`effective_cores`) moved into the engine
so the CLI, the out-of-core route and the platform share one probe. It follows the established
implementations: uber-go/automaxprocs (`internal/cgroups`) finds the process's cgroup from
`/proc/self/cgroup`, finds the controller's mount in `/proc/self/mountinfo`, translates the
membership path through the mount's root field, and reads the CFS quota (`cpu.max` on cgroup v2,
`cpu.cfs_quota_us` over `cpu.cfs_period_us` on v1); the JVM's container support (HotSpot
`CgroupSubsystemFactory`) parses the same two files the same way. Both read only the process's own
cgroup. The kernel enforces the tightest quota on the whole path to the root, and Rust's
`std::thread::available_parallelism` reads only the leaf, so this adds the ancestor walk; the walk
stops at the cgroup namespace root because limits above it are not visible to the process.

A limit file that is present but unreadable or malformed is a coded error: a limit that cannot be
read is not the same as no limit. A level without the file (the root has none) is skipped, and a
host with no /proc or cgroup tree at all (macOS) has no limit. Affinity comes from
`os.sched_getaffinity` where the platform has it and from `os.cpu_count()` elsewhere. All paths
resolve under `fs_root` so tests can drive the whole probe from a fake tree.
"""

from __future__ import annotations

import functools
import math
import os
from pathlib import Path
from typing import Any

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.pipeline._cgroup import (
    CgroupMappingError,
    Mount,
    parse_membership,
    parse_mountinfo,
    relative_to_root,
    select_mount,
)

__all__ = ["detect_usable_cpus", "native_thread_clamp"]


def _unreadable(detail: str) -> ExecutionError:
    return ExecutionError(
        code=reasons.runtime.CPU_DETECTION_FAILED, message=f"cpu_limit_unreadable ({detail})"
    )


def _leaf_dirs(mount: Mount, membership: str, fs_root: Path) -> list[Path]:
    """Directories from the leaf up to and including the mountpoint."""
    rel = relative_to_root(membership, mount.root)
    if rel is None:
        raise CgroupMappingError(
            f"membership={membership!r} is outside mount root {str(mount.root)!r}"
        )
    mountpoint = Path(str(fs_root) + str(mount.mountpoint))
    levels = [mountpoint]
    current = mountpoint
    for part in rel.parts:
        current = current / part
        levels.append(current)
    return list(reversed(levels))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        failure = type(exc).__name__
    # Outside the handler so the error carries no cause or context from the failed read.
    raise _unreadable(f"path={path}, error={failure}")


def _positive_int(token: str) -> int | None:
    return int(token) if token.isascii() and token.isdigit() and int(token) > 0 else None


def _v2_ratio(path: Path) -> float | None:
    text = _read(path)
    if text is None:
        return None
    tokens = text.split()
    if len(tokens) != 2:
        raise _unreadable(f"path={path}")
    quota, period_text = tokens
    period_n = _positive_int(period_text)
    if period_n is None:
        raise _unreadable(f"path={path}")
    if quota == "max":
        return None
    quota_n = _positive_int(quota)
    if quota_n is None:
        raise _unreadable(f"path={path}")
    return quota_n / period_n


def _v1_ratio(directory: Path) -> float | None:
    quota_path = directory / "cpu.cfs_quota_us"
    text = _read(quota_path)
    if text is None:
        return None
    raw = text.strip()
    if raw == "-1":
        return None
    quota = _positive_int(raw)
    if quota is None:
        raise _unreadable(f"path={quota_path}")
    period_path = directory / "cpu.cfs_period_us"
    period_text = _read(period_path)
    period = _positive_int(period_text.strip()) if period_text is not None else None
    if period is None:
        raise _unreadable(f"path={period_path}")
    return quota / period


def cpu_quota_cores(fs_root: Path = Path("/")) -> float | None:
    """The tightest CPU quota (in cores) on the process's cgroup path, or None
    when no level limits it."""
    try:
        return _cpu_quota_cores(fs_root)
    except CgroupMappingError as mapping:
        detail = str(mapping)
    # Outside the handler so the error carries no cause or context.
    raise _unreadable(detail)


def _cpu_quota_cores(fs_root: Path) -> float | None:
    proc = Path(str(fs_root)) / "proc" / "self"
    cgroup_text = _read(proc / "cgroup")
    mountinfo_text = _read(proc / "mountinfo")
    if cgroup_text is None or mountinfo_text is None:
        return None
    v2_path, v1_path = parse_membership(cgroup_text)
    mounts = parse_mountinfo(mountinfo_text)
    ratios: list[float] = []
    if v2_path is not None:
        mount = select_mount(mounts, v2_path, v2=True)
        if mount is not None:
            for level in _leaf_dirs(mount, v2_path, fs_root):
                ratio = _v2_ratio(level / "cpu.max")
                if ratio is not None:
                    ratios.append(ratio)
    if v1_path is not None:
        mount = select_mount(mounts, v1_path, v2=False)
        if mount is not None:
            for level in _leaf_dirs(mount, v1_path, fs_root):
                ratio = _v1_ratio(level)
                if ratio is not None:
                    ratios.append(ratio)
    return min(ratios) if ratios else None


def detect_usable_cpus(*, fs_root: Path | None = None, affinity: int | None = None) -> int:
    """``max(1, min(affinity, floor(tightest quota)))``; affinity alone when no level limits.

    With no arguments the result is read once per process. ``fs_root`` and ``affinity`` are for
    callers that inject a fake tree or a known affinity (the platform wrapper, tests).
    """
    if fs_root is None and affinity is None:
        return _cached_usable_cpus()
    return _compute(fs_root or Path("/"), affinity)


def _affinity_cpus() -> int:
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is None:
        return os.cpu_count() or 1
    return len(getaffinity(0))


def _compute(fs_root: Path, affinity: int | None) -> int:
    cpus = affinity if affinity is not None else _affinity_cpus()
    quota = cpu_quota_cores(fs_root)
    if quota is None:
        return max(1, cpus)
    return max(1, min(cpus, math.floor(quota)))


@functools.cache
def _cached_usable_cpus() -> int:
    return _compute(Path("/"), None)


def native_thread_clamp(requested: int) -> dict[str, Any]:
    """The thread count the native kernels apply to a `native_threads` request.

    The companion's own resolver answers (`resolve_native_threads`: the same clamp and host read
    every kernel call makes), so the record cannot drift from what the kernel does. ``applied`` is
    None when the companion, or its resolver symbol, is absent or the resolver refuses the request
    or a bad knee override.
    """
    return {"requested": requested, "applied": _kernel_resolved_threads(requested)}


def _kernel_resolved_threads(requested: int) -> int | None:
    try:
        from decoy_engine_native import _kernel

        return int(_kernel.resolve_native_threads(requested))
    except Exception:  # absent companion, older companion, or a value-free coded refusal
        return None

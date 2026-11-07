"""Choose the pytest-xdist worker count for scripts/dev/gates.sh.

Usage: _gate_workers.py   (reads GATES_WORKERS; prints "<count> <reason>")

GATES_WORKERS is a number (used as given, minimum 1) or `auto` (the default). A count above 1 in
a venv without pytest-xdist becomes 1, and the reason says so. `auto` caps by
memory because each xdist worker holds a full copy of the test process (about 1.3 GB peak
measured on this suite, 5.3 GB for four workers):
  - a small host (usable memory under 14 GB, from /proc/meminfo MemTotal and the cgroup limit,
    whichever is lower) gets at most 2 workers, and 1 when MemAvailable is under 4 GB;
  - any other host gets min(nproc, 4).
Stdlib only, so it runs in any venv."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

KB_PER_GB = 1024 * 1024
SMALL_HOST_GB = 14
SMALL_HOST_MAX_WORKERS = 2
LOW_AVAILABLE_GB = 4
LARGE_HOST_MAX_WORKERS = 4


def read_host(meminfo: Path, cgroup_max: Path) -> tuple[int, int, int | None]:
    """(MemTotal kB, MemAvailable kB, cgroup memory limit in bytes or None when unlimited/absent)."""
    fields: dict[str, int] = {}
    for line in meminfo.read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            fields[key] = int(rest.split()[0])
    limit: int | None = None
    try:
        raw = cgroup_max.read_text().strip()
        limit = int(raw) if raw.isdigit() else None
    except OSError:
        pass
    return fields.get("MemTotal", 0), fields.get("MemAvailable", 0), limit


def choose(
    requested: str, nproc: int, mem_total_kb: int, mem_avail_kb: int, cgroup_max: int | None
) -> tuple[int, str]:
    requested = (requested or "auto").strip().lower()
    if requested != "auto":
        if not requested.lstrip("-").isdigit():
            raise ValueError(f"GATES_WORKERS must be a number or 'auto', got {requested!r}")
        return max(1, int(requested)), f"GATES_WORKERS={requested}"
    total_gb = mem_total_kb / KB_PER_GB
    if cgroup_max is not None:
        total_gb = min(total_gb, cgroup_max / (KB_PER_GB * 1024))
    avail_gb = mem_avail_kb / KB_PER_GB
    if total_gb < SMALL_HOST_GB:
        if avail_gb < LOW_AVAILABLE_GB:
            return 1, f"auto: small host ({total_gb:.1f} GB), only {avail_gb:.1f} GB available"
        n = max(1, min(nproc, SMALL_HOST_MAX_WORKERS))
        return n, f"auto: small host ({total_gb:.1f} GB), capped at {SMALL_HOST_MAX_WORKERS}"
    n = max(1, min(nproc, LARGE_HOST_MAX_WORKERS))
    return n, f"auto: {total_gb:.1f} GB, {nproc} cpu, capped at {LARGE_HOST_MAX_WORKERS}"


USAGE_EXIT = 64  # EX_USAGE: python itself exits 2 for a missing script, which is a crash here


def main() -> int:
    try:
        nproc = len(os.sched_getaffinity(0))
    except AttributeError:  # macOS
        nproc = os.cpu_count() or 1
    total, avail, limit = (0, 0, None)
    try:
        total, avail, limit = read_host(Path("/proc/meminfo"), Path("/sys/fs/cgroup/memory.max"))
    except OSError:
        pass
    if total == 0:  # no /proc/meminfo (macOS): size is unknown, so be conservative
        total, avail = SMALL_HOST_GB * KB_PER_GB - 1, LOW_AVAILABLE_GB * KB_PER_GB
    try:
        count, why = choose(os.environ.get("GATES_WORKERS", "auto"), nproc, total, avail, limit)
    except ValueError as exc:
        print(f"gates: {exc}", file=sys.stderr)
        return USAGE_EXIT
    if count > 1 and importlib.util.find_spec("xdist") is None:
        count, why = 1, f"{why}; pytest-xdist is not installed in {sys.prefix}, running serial"
    print(count, why)
    return 0


if __name__ == "__main__":
    sys.exit(main())

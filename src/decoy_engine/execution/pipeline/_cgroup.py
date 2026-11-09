"""Shared cgroup membership and mount translation (RUN-STREAM E1b).

`/proc/self/cgroup` names the process's cgroup; `/proc/self/mountinfo` says where each hierarchy is
mounted and which subtree of it the mount exposes (its root field). Both the CPU probe
(`pipeline/_host.py`) and the memory budget (`out_of_core/_budget.py`) need the membership line, and
the CPU probe also translates it through the mount root (the automaxprocs and HotSpot method). The
controller-specific quota files and the error policy stay with the callers: this module raises
`CgroupMappingError` with a value-free detail and each caller maps it to its own error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError

__all__ = [
    "CgroupMappingError",
    "Mount",
    "parse_membership",
    "parse_mountinfo",
    "relative_to_root",
    "select_mount",
]


class CgroupMappingError(ExecutionError):
    """A membership path or mount mapping that cannot be resolved.

    The detail is value-free. The code is the CPU probe's; the memory budget catches this error and
    degrades to "no cgroup limit", so it never reaches a caller as a memory failure.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=reasons.runtime.CPU_DETECTION_FAILED, message=f"cpu_limit_unreadable ({detail})"
        )


@dataclass(frozen=True)
class Mount:
    root: PurePosixPath
    mountpoint: PurePosixPath
    fstype: str
    super_options: frozenset[str]


def _unescape(field: str) -> str:
    """mountinfo octal-escapes space, tab, newline and backslash as ``\\NNN``."""
    out: list[str] = []
    i = 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def parse_mountinfo(text: str) -> list[Mount]:
    mounts: list[Mount] = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, _, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            continue
        mounts.append(
            Mount(
                root=PurePosixPath(_unescape(left_fields[3])),
                mountpoint=PurePosixPath(_unescape(left_fields[4])),
                fstype=right_fields[0],
                super_options=frozenset(right_fields[2].split(",")),
            )
        )
    return mounts


def parse_membership(cgroup_text: str, v1_controller: str = "cpu") -> tuple[str | None, str | None]:
    """(v2 path, path in the v1 hierarchy carrying `v1_controller`) from /proc/self/cgroup."""
    v2: str | None = None
    v1: str | None = None
    for line in cgroup_text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _hierarchy, controllers, path = parts
        if controllers == "":
            v2 = path
        elif v1_controller in controllers.split(","):
            v1 = path
    return v2, v1


def relative_to_root(membership: str, root: PurePosixPath) -> PurePosixPath | None:
    """``membership`` relative to the mount root, or None when it is outside."""
    path = PurePosixPath(membership)
    if ".." in path.parts or not path.is_absolute():
        raise CgroupMappingError(f"membership={membership!r}")
    if str(path) == "/":
        return PurePosixPath(".")
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def select_mount(
    mounts: list[Mount], membership: str, *, v2: bool, v1_controller: str = "cpu"
) -> Mount | None:
    matching: list[Mount] = []
    for mount in mounts:
        if v2:
            if mount.fstype != "cgroup2":
                continue
        elif mount.fstype != "cgroup" or v1_controller not in mount.super_options:
            continue
        if relative_to_root(membership, mount.root) is not None:
            matching.append(mount)
    if len(matching) > 1:
        raise CgroupMappingError(f"ambiguous cgroup mounts for membership={membership!r}")
    if not matching:
        # A mount exists but membership is outside every root: not a clean "no mount".
        same_kind = [
            m
            for m in mounts
            if (
                m.fstype == "cgroup2"
                if v2
                else (m.fstype == "cgroup" and v1_controller in m.super_options)
            )
        ]
        if same_kind:
            raise CgroupMappingError(
                f"membership={membership!r} is outside every cgroup mount root"
            )
        return None
    return matching[0]

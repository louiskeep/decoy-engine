"""Pipeline-spine route clusters. RUN-STREAM's strict `stream` requirement lives in `_stream`.

The host CPU probe and the shared cgroup mapping are exported here so other packages do not import
the private modules.
"""

from decoy_engine.execution.pipeline._cgroup import (
    CgroupMappingError,
    Mount,
    parse_membership,
    parse_mountinfo,
    relative_to_root,
    select_mount,
)
from decoy_engine.execution.pipeline._host import detect_usable_cpus, native_thread_clamp

__all__ = [
    "CgroupMappingError",
    "Mount",
    "detect_usable_cpus",
    "native_thread_clamp",
    "parse_membership",
    "parse_mountinfo",
    "relative_to_root",
    "select_mount",
]

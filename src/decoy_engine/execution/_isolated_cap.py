"""Restricted-channel transport of the attempt-arena capability to an isolated
child (F25 T11, plan 1.5 / A04).

The isolated-run payload can carry source/target DSNs, so the attempt-arena
capability a child runs under must never travel through argv, the environment,
or a discoverable path. The parent writes a small capability descriptor to a
pipe and passes the read end to the child as an inherited file descriptor; the
child reads it exactly once and closes it. Only the integer fd *number* is named
in the environment (the systemd ``LISTEN_FDS`` convention): the capability
content itself is read from the inherited fd, never from the environment.

The descriptor is the attempt's identity and quota assignment, not a fresh
quota: a child receives the parent's attempt capability so parent and child
share one project quota domain (plan 1.5 item 2). The child writes only beneath
the parent-registered work root, so it never allocates a second independent
quota. The encrypted recovery envelope and the liveness-lock fd that will also
ride this channel are Slice 4/5; this module is the transport they reuse.
"""

from __future__ import annotations

import json
import os
from typing import Any

# The env var names ONLY the inherited fd number, never the capability. The
# capability bytes are read from that fd and the fd is closed after one read.
CAP_FD_ENV = "DECOY_SCRATCH_CAP_FD"


def capability_descriptor(arena: Any) -> dict[str, Any] | None:
    """The control-plane descriptor for ``arena``'s attempt capability, or None
    when there is no qualified arena (the pre-activation path transports nothing).

    Carries identity, fence generation, approved root and quota assignment only,
    never keys, DSNs or source values (plan 1.5 ledger record rule)."""

    cap = getattr(arena, "capability", None)
    if cap is None:
        return None
    lease = cap.lease
    return {
        "arena_id": cap.arena_id,
        "job_id": lease.job_id,
        "attempt_id": lease.attempt_id,
        "lease_id": lease.lease_id,
        "generation": lease.generation,
        "mode": lease.mode.value,
        "approved_root": str(cap.approved_root),
        "quota_bytes": cap.quota_bytes,
        "quota_inodes": cap.quota_inodes,
    }


def open_capability_channel(descriptor: dict[str, Any]) -> tuple[int, int]:
    """Create the pipe carrying ``descriptor`` to the child.

    Returns ``(read_fd, write_fd)``. The caller passes ``read_fd`` to the child
    via ``pass_fds`` and sets ``CAP_FD_ENV`` to its number, writes the descriptor
    to ``write_fd``, then closes ``write_fd`` so the child reads EOF after the one
    message. ``read_fd`` is marked inheritable; ``write_fd`` stays private to the
    parent."""

    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    payload = json.dumps(descriptor).encode("utf-8")
    os.write(write_fd, payload)
    return read_fd, write_fd


def child_env_with_cap_fd(env: dict[str, str], read_fd: int) -> dict[str, str]:
    """Return ``env`` with ``CAP_FD_ENV`` set to the inherited fd number."""

    return {**env, CAP_FD_ENV: str(read_fd)}


def read_inherited_capability(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Child side: read the capability descriptor from the inherited fd once.

    Returns the descriptor dict, or None when the parent transported none (the
    pre-activation path). The fd is drained and closed after the single read, so
    the capability does not linger in an open descriptor (plan 1.5 "cleared after
    a single read")."""

    source = os.environ if env is None else env
    raw = source.get(CAP_FD_ENV)
    if raw is None:
        return None
    fd = int(raw)
    with os.fdopen(fd, "rb", closefd=True) as handle:
        data = handle.read()
    if not data:
        return None
    descriptor: dict[str, Any] = json.loads(data)
    return descriptor


__all__ = [
    "CAP_FD_ENV",
    "capability_descriptor",
    "child_env_with_cap_fd",
    "open_capability_channel",
    "read_inherited_capability",
]

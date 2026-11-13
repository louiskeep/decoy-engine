"""Shared helpers for the F22 sink tests (plan 5.6, 7.2; slices c1a, c1b).

A sink is built on either POSIX policy: the ambient one from a path, the strict one from an
`OutputDir` a `RootedFileAccess` issued. Stages are found by name beside the target.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from tests.fileaccess.publication._dirmode_support import _output_dir

POLICIES = ["ambient", "strict"]
STAGE_PREFIX = "_decoy_stage_"


def table(seed: str) -> pa.Table:
    """A small table whose content depends on `seed` (a different seed is a different result)."""
    return pa.table({"id": [1, 2, 3], "tag": [f"{seed}-a", f"{seed}-b", f"{seed}-c"]})


@contextmanager
def sink_factory(policy: str, base: Path, rel: str = "out") -> Iterator[Any]:
    """Yields `make()`, which builds a fresh sink for `base / rel` under `policy`."""
    roots: list[int] = []

    def make() -> ParquetTransactionalSink:
        if policy == "ambient":
            return ParquetTransactionalSink(base / rel)
        out, root_fd = _output_dir(policy, base, rel)
        roots.append(root_fd)
        return ParquetTransactionalSink(out)

    try:
        yield make
    finally:
        for fd in roots:
            os.close(fd)


def stages(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.iterdir() if p.name.startswith(STAGE_PREFIX))


def tree(leaf: Path) -> list[tuple[str, int, bytes | None]]:
    """Every entry under `leaf`: relative path, inode and bytes for files."""
    entries = []
    for path in sorted([leaf, *leaf.rglob("*")]):
        data = path.read_bytes() if path.is_file() else None
        entries.append((str(path.relative_to(leaf)), os.lstat(path).st_ino, data))
    return entries

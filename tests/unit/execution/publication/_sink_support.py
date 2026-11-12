"""Shared helpers for the F22 sink tests (plan 5.6, 7.2; slices c1a, c1b).

A sink is built on either POSIX policy: the ambient one from a path, the strict one from an
`OutputDir` a `RootedFileAccess` issued. Stages are found by name beside the target. The
`spawn` child here commits through a sink and SIGKILLs itself after the bind rename (restart
tests, with a marker file as the non-vacuity line).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from tests.fileaccess.publication._dirmode_support import _output_dir

POLICIES = ["ambient", "strict"]
STAGE_PREFIX = "._decoy_publication_pending_"
DESCRIPTOR = "_decoy_publication.json"


def table(seed: str) -> pa.Table:
    """A small table whose content depends on `seed` (a different seed is a different result)."""
    return pa.table({"id": [1, 2, 3], "tag": [f"{seed}-a", f"{seed}-b", f"{seed}-c"]})


@contextmanager
def sink_factory(policy: str, base: Path, rel: str = "out") -> Iterator[Any]:
    """Yields `make(request=None)`, which builds a fresh sink for `base / rel` under `policy`."""
    roots: list[int] = []

    def make(request: Any = None) -> ParquetTransactionalSink:
        kw = {} if request is None else {"request": request}
        if policy == "ambient":
            return ParquetTransactionalSink(base / rel, **kw)
        out, root_fd = _output_dir(policy, base, rel)
        roots.append(root_fd)
        return ParquetTransactionalSink(out, **kw)

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


def sink_crash_child(spec_path: str) -> None:
    """Commit `table(spec["seed"])` through a sink with `spec["op"]` into `spec["rel"]`,
    SIGKILLed just before the first fsync after the bind rename."""
    from decoy_engine.publication import PublicationRequest
    from tests.unit.publication._publish_support import KILL_MARKER, _kill_at_chain_fsync

    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    _kill_at_chain_fsync(1, base.parent / KILL_MARKER)
    with sink_factory(spec["policy"], base, spec["rel"]) as make:
        sink = make(PublicationRequest(spec["op"]))
        sink.write("customers", table(spec["seed"]))
        receipt = sink.commit()
    Path(spec["result"]).write_text(json.dumps({"outcome": receipt.outcome}), encoding="utf-8")

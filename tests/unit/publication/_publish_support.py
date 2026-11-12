"""Shared helpers for the F22 `publish_unit` tests (plan 5.3, 7.1, 7.2; slice a3).

Tables are tiny Parquet files written through `publish_tables` or staged by hand; a unit is
compared by its tree, bytes, inode numbers and descriptor digest. The `spawn` children here run
one publish each: a winner parked in the `_after_judge` hook (two-writer tests, ordered by
events, never by timing) or a writer that SIGKILLs itself at a named fsync after the bind rename
(crash tests, with a marker file as the non-vacuity line).
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
from pathlib import Path
from typing import Any

from tests.fileaccess.publication._dirmode_support import _output_dir

DESCRIPTOR = "_decoy_publication.json"
STAGE_GLOB = "._decoy_publication_pending_*"
OSMARK = "OSMARK"
KILL_MARKER = "killed-after-rename"
POLICIES = ["strict", "ambient"]


def tables(seed: str, *, name: str = "customers.parquet") -> dict[str, Any]:
    """One small table whose content depends on `seed` (a different seed is a different result)."""
    import pyarrow as pa

    return {name: pa.table({"id": [1, 2, 3], "tag": [f"{seed}-a", f"{seed}-b", f"{seed}-c"]})}


def make_out(policy: str, base: Path, rel: str) -> tuple[Any, int]:
    return _output_dir(policy, base, rel)


def close_root(root_fd: int) -> None:
    if root_fd >= 0:
        os.close(root_fd)


def snapshot(leaf: Path) -> dict[str, Any]:
    """Every entry of a bound unit: relative path, inode, bytes for files, and the digest."""
    entries = []
    for path in sorted([leaf, *leaf.rglob("*")]):
        st = os.lstat(path)
        data = path.read_bytes() if path.is_file() else None
        entries.append((str(path.relative_to(leaf)), st.st_ino, data))
    digest = hashlib.sha256((leaf / DESCRIPTOR).read_bytes()).hexdigest()
    return {"entries": entries, "digest": digest}


def stages_left(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.glob(STAGE_GLOB))


def descriptor(leaf: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((leaf / DESCRIPTOR).read_bytes())
    return loaded


# -- spawn children -------------------------------------------------------------------------


def _anchor_child(base: Path) -> None:
    """Fix the ambient mount boundary at `base` inside a child (as `anchor_at` does in-process)."""
    from decoy_engine.fileaccess import _local
    from tests.fileaccess.publication._chain_support import restat

    real = _local._probe_dir  # type: ignore[attr-defined]
    top = os.path.realpath(base)

    def probe(path: Any) -> os.stat_result:
        st = real(path)
        where = os.path.realpath(path)
        inside = where == top or where.startswith(top + os.sep)
        return st if inside else restat(st, dev=st.st_dev + 1)

    _local._probe_dir = probe  # type: ignore[attr-defined]


def _kill_at_chain_fsync(nth: int, marker: Path) -> None:
    """SIGKILL just before the `nth` fsync (1-based) that follows the stage rename."""
    real_fsync, real_replace = os.fsync, os.replace
    state = {"renamed": False, "count": 0}

    def replace(src: Any, dst: Any, **kw: Any) -> None:
        real_replace(src, dst, **kw)
        if "._decoy_publication_pending_" in os.fsdecode(src):
            state["renamed"] = True

    def fsync(fd: int) -> None:
        if state["renamed"]:
            state["count"] += 1
            if state["count"] == nth:
                marker.write_text("x", encoding="utf-8")
                os.kill(os.getpid(), signal.SIGKILL)
        real_fsync(fd)

    os.replace = replace  # type: ignore[assignment]
    os.fsync = fsync  # type: ignore[assignment]


def crash_child(spec_path: str) -> None:
    """Publish `spec["seed"]` into `spec["rel"]`, SIGKILLed before chain fsync `spec["kill_at"]`."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    from decoy_engine.publication import PublicationRequest, publish_tables

    if spec["policy"] == "ambient":
        _anchor_child(base)
    _kill_at_chain_fsync(spec["kill_at"], base.parent / KILL_MARKER)
    out, _root_fd = make_out(spec["policy"], base, spec["rel"])
    receipt = publish_tables(out, tables(spec["seed"]), PublicationRequest(spec["op"]))
    Path(spec["result"]).write_text(json.dumps({"outcome": receipt.outcome}), encoding="utf-8")


def publish_child(spec_path: str) -> None:
    """A plain publish in a fresh process; writes the receipt's outcome and durability."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    from decoy_engine.publication import PublicationRequest, publish_tables

    out, _root_fd = make_out(spec["policy"], base, spec["rel"])
    receipt = publish_tables(out, tables(spec["seed"]), PublicationRequest(spec["op"]))
    Path(spec["result"]).write_text(
        json.dumps({"outcome": receipt.outcome, "durable": receipt.durable}), encoding="utf-8"
    )


def run_spec(target: Any, spec: dict[str, Any], tmp: Path) -> tuple[int, dict[str, Any]]:
    """Run `target(spec_path)` in a `spawn` child; return its exit code and its result."""
    spec_path = tmp / f"spec-{len(list(tmp.glob('spec-*.json')))}.json"
    result_path = tmp / (spec_path.stem + ".result")
    spec_path.write_text(json.dumps({**spec, "result": str(result_path)}), encoding="utf-8")
    proc = multiprocessing.get_context("spawn").Process(target=target, args=(str(spec_path),))
    proc.start()
    proc.join(120)
    if proc.is_alive():  # pragma: no cover - a hung child is a failure, not a pass
        proc.kill()
        proc.join()
        raise AssertionError("child did not finish")
    result = json.loads(result_path.read_text("utf-8")) if result_path.exists() else {}
    return proc.exitcode if proc.exitcode is not None else -1, result


def winner_child(spec_path: str, w_in_hook: Any, l_probed: Any, l_waiting: Any) -> None:
    """Writer W: parks in `_after_judge` (lock held, bind verdict) until L has probed the lock
    and is itself blocked on it, then binds."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    from decoy_engine.publication import PublicationRequest, _publish, publish_tables

    fired = []

    def hook() -> None:
        fired.append(True)
        w_in_hook.set()
        if not (l_probed.wait(60) and l_waiting.wait(60)):  # pragma: no cover - a hung loser
            raise AssertionError("loser never probed the lock")

    _publish._after_judge = hook  # type: ignore[attr-defined]
    out, _root_fd = make_out(spec["policy"], Path(spec["base"]), spec["rel"])
    receipt = publish_tables(out, tables(spec["seed"]), PublicationRequest(spec["op"]))
    Path(spec["result"]).write_text(
        json.dumps({"outcome": receipt.outcome, "fired": bool(fired)}), encoding="utf-8"
    )


def start_winner(spec: dict[str, Any], tmp: Path) -> tuple[Any, Path, Any, Any, Any]:
    ctx = multiprocessing.get_context("spawn")
    w_in_hook, l_probed, l_waiting = ctx.Event(), ctx.Event(), ctx.Event()
    spec_path = tmp / "winner.json"
    result_path = tmp / "winner.result"
    spec_path.write_text(json.dumps({**spec, "result": str(result_path)}), encoding="utf-8")
    proc = ctx.Process(target=winner_child, args=(str(spec_path), w_in_hook, l_probed, l_waiting))
    proc.start()
    return proc, result_path, w_in_hook, l_probed, l_waiting

"""Shared helpers: an independent descriptor oracle and a real-process publisher."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import publication as pub
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

WORKER = Path(__file__).with_name("_worker.py")


def table_a(extra: int = 0) -> pa.Table:
    return pa.table({"id": [1, 2, 3 + extra], "name": ["a", "b", "c"]})


def table_b() -> pa.Table:
    return pa.table({"k": [10, 20], "v": [1.5, 2.5]})


def empty_table() -> pa.Table:
    return pa.table({"z": pa.array([], type=pa.int64())})


def publish_two(
    target: Path,
    *,
    request: pub.PublicationRequest | None = None,
    a: pa.Table | None = None,
) -> tuple[ParquetTransactionalSink, pub.CommitReceipt]:
    sink = ParquetTransactionalSink(target, request=request)
    sink.write("alpha", a if a is not None else table_a())
    sink.write("beta", table_b())
    return sink, sink.commit()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def oracle(target: Path) -> dict[str, Any]:
    """Re-derive everything from raw files with no production equality helper."""
    desc_bytes = (target / "_decoy_publication.json").read_bytes()
    desc = json.loads(desc_bytes)
    seen = {}
    for art in desc["artifacts"]:
        f = target / art["path"]
        assert f.stat().st_size == art["size"], art["path"]
        assert sha(f) == art["sha256"], art["path"]
        if art["role"] == "table":
            assert pq.read_metadata(f).num_rows == art["rows"], art["path"]
        seen[art["path"]] = art
    ev = target / desc["evidence"]["path"]
    assert sha(ev) == desc["evidence"]["sha256"]
    on_disk = {p.name for p in target.iterdir()}
    assert on_disk == set(seen) | {"_decoy_publication.json", "_decoy_evidence_core.json"}
    return {
        "descriptor": desc,
        "descriptor_digest": hashlib.sha256(desc_bytes).hexdigest(),
        "result_id": desc["result_id"],
    }


def tree_digest(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(p.relative_to(root)): (sha(p), p.stat().st_ino)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def staging_dirs(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.glob("_decoy_stage_*"))


def run_worker(*args: str, check: bool = True) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, str(WORKER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

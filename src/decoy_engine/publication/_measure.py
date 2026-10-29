"""Measuring and verifying publication artifacts from real files.

Hashing reads closed files in bounded chunks. A Parquet footer alone does not prove
every planned row arrived, so footer row counts are reconciled with the producer's
counts when the producer supplies them.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pyarrow.parquet as pq

from decoy_engine.errors import DecoyError
from decoy_engine.publication import _codec
from decoy_engine.publication._types import ArtifactRef

_CHUNK = 1 << 20


class MeasureError(DecoyError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)
        self.reason_code = reason_code


def hash_file(path: Path) -> tuple[int, str]:
    import hashlib

    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def schema_digest(path: Path) -> tuple[int, str]:
    meta = pq.ParquetFile(path).metadata
    schema = pq.ParquetFile(path).schema_arrow
    fields = [[f.name, str(f.type), f.nullable] for f in schema]
    return meta.num_rows, _codec.sha256_hex(_codec.canonical_bytes(fields))


def _regular_file(path: Path) -> None:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise MeasureError("unsafe_artifact", f"{path.name} is not a regular file")


def measure_directory(
    stage: Path, *, producer_rows: Mapping[str, int] | None = None
) -> tuple[ArtifactRef, ...]:
    """Inventory every file in a flat staging directory, excluding the descriptor files."""
    entries: list[ArtifactRef] = []
    for child in sorted(stage.iterdir(), key=lambda p: p.name):
        if child.name in _codec.RESERVED_NAMES:
            continue
        try:
            _codec.safe_relative_path(child.name)
            _regular_file(child)
        except _codec.CodecError as exc:
            raise MeasureError("unsafe_artifact", str(exc)) from exc
        size, digest = hash_file(child)
        if child.suffix == ".parquet":
            try:
                rows, sdigest = schema_digest(child)
            except Exception as exc:
                raise MeasureError("unreadable_table", child.name) from exc
            expected = None if producer_rows is None else producer_rows.get(child.stem)
            if expected is not None and expected != rows:
                raise MeasureError("row_count_mismatch", f"{child.name}: {expected} != {rows}")
            entries.append(ArtifactRef("table", child.name, size, digest, rows, sdigest))
        else:
            role = "quarantine" if child.suffix == ".jsonl" else "sidecar"
            entries.append(ArtifactRef(role, child.name, size, digest))
    return _codec.sorted_unique(entries)


def verify_artifacts(root: Path, entries: tuple[ArtifactRef, ...]) -> None:
    """Re-hash every bound artifact; any missing or changed file is corruption."""
    for e in entries:
        path = root / e.path
        try:
            _regular_file(path)
            size, digest = hash_file(path)
        except (OSError, MeasureError) as exc:
            raise MeasureError("artifact_missing", e.path) from exc
        if size != e.size or digest != e.sha256:
            raise MeasureError("artifact_changed", e.path)

"""Reusable conformance suite for publication adapters (local, DB, object store).

An adapter supplies a factory that builds a sink for a destination/request and a reader
that returns `{table: row_count}` for whatever the destination currently resolves to.
`run_conformance` exercises the F22 contract with real publish/retry/conflict/failure
sequences and raises `ConformanceError` on the first violation. It cannot qualify a DB
or object-store adapter by itself: those must additionally pass vendor commit-disconnect
and conditional-write race tests against the real service (mocks prove request shape only).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.errors import DecoyError
from decoy_engine.publication import _protocol
from decoy_engine.publication._types import (
    CommitError,
    CommitReceipt,
    PublicationRefusedError,
    PublicationRequest,
)


class ConformanceError(DecoyError, AssertionError):
    pass


SinkFactory = Callable[[PublicationRequest], Any]
Reader = Callable[[Path], dict[str, int]]


@dataclass(frozen=True)
class Adapter:
    name: str
    make_sink: SinkFactory
    read_tables: Reader
    unit: str


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConformanceError(message)


def _publish(adapter: Adapter, request: PublicationRequest, ids: list[int]) -> CommitReceipt:
    sink = adapter.make_sink(request)
    sink.write("t", pa.table({"id": ids}))
    try:
        return sink.commit()  # type: ignore[no-any-return, unused-ignore]
    except CommitError:
        sink.abort()
        raise


def _batches_then_fail() -> Iterator[pa.RecordBatch]:
    yield pa.record_batch({"id": [1]})
    raise RuntimeError("producer failed")


def run_conformance(adapter: Adapter, workdir: Path) -> None:
    """Raise `ConformanceError` unless `adapter` honors the publication contract."""
    probe = adapter.make_sink(PublicationRequest(destination=workdir / "probe", unit=adapter.unit))
    decl = _protocol.declaration_for(probe)
    _require(not _protocol.violations(decl), f"{adapter.name}: declaration is not admissible")

    dest = workdir / uuid.uuid4().hex
    req = PublicationRequest(destination=dest, operation_id="op-1", unit=adapter.unit)
    first = _publish(adapter, req, [1, 2, 3])
    _require(first.outcome == "published", "first publication must report 'published'")
    _require(adapter.read_tables(dest) == {"t": 3}, "published data must be complete")

    again = _publish(adapter, req, [1, 2, 3])
    _require(again.outcome == "already_committed", "a retry must report 'already_committed'")
    _require(again.descriptor_digest == first.descriptor_digest, "a retry must return the original")
    _require(adapter.read_tables(dest) == {"t": 3}, "a retry must not duplicate data")

    try:
        _publish(
            adapter,
            PublicationRequest(destination=dest, operation_id="op-1", unit=adapter.unit),
            [9],
        )
    except PublicationRefusedError as exc:
        _require(
            exc.receipt is not None and exc.receipt.outcome == "conflict",
            "reused operation id with new content is a conflict",
        )
    else:
        raise ConformanceError("a reused operation id with different content was accepted")
    _require(
        adapter.read_tables(dest) == {"t": 3}, "a conflict must not mutate the committed result"
    )

    sink = adapter.make_sink(
        PublicationRequest(
            destination=workdir / uuid.uuid4().hex, operation_id="op-2", unit=adapter.unit
        )
    )
    try:
        sink.write_batches("t", _batches_then_fail(), schema=pa.schema([("id", pa.int64())]))
    except RuntimeError:
        pass
    else:
        raise ConformanceError("producer failure must propagate")
    try:
        sink.commit()
    except PublicationRefusedError:
        pass
    else:
        raise ConformanceError("a caught write failure must permanently invalidate the attempt")
    sink.abort()

    done = adapter.make_sink(
        PublicationRequest(
            destination=workdir / uuid.uuid4().hex, operation_id="op-3", unit=adapter.unit
        )
    )
    done.write("t", pa.table({"id": [1]}))
    done.commit()
    done.abort()  # abort after commit must never remove published data
    _require(
        adapter.read_tables(done.request.destination) == {"t": 1}, "abort after commit removed data"
    )

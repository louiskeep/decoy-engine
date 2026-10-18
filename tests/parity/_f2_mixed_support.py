"""Shared builders for the F2 mixed generate+mask route tests.

One config shape (a generate parent `people`, a mask child `accounts` with an FK
column and two masked payload columns), one run helper that drives the oracle
(`execution_mode="full_frame"`) and the production route (`"auto"`) on the same
inputs, and a strict table comparison that also checks the Arrow IPC bytes.
"""

from __future__ import annotations

import copy
import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionResult
from decoy_engine.keyprovider import SecretKeyProvider

ENGINE_VERSION = "f2-mixed"
COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
NEEDS_COMPANION = pytest.mark.skipif(
    not COMPANION_PRESENT, reason="decoy-engine-native companion not installed"
)

CHILD = "accounts"
PARENT = "people"
FK = "person_id"

# Small enough to chunk a few dozen rows several times over.
CHUNK_ROWS = 7
LANE_KW: dict[str, Any] = {"auto_chunk_threshold_rows": 1, "chunk_size_rows": CHUNK_ROWS}


def key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


def payload_columns() -> list[dict[str, Any]]:
    return [
        {"name": "email", "strategy": "hash", "namespace": "ns_email"},
        {"name": "note", "strategy": "redact"},
    ]


def child_table(keys: pa.Array, *, rows: int | None = None) -> pa.Table:
    n = len(keys) if rows is None else rows
    return pa.table(
        {
            FK: keys,
            "email": pa.array([f"u{i}@example.com" for i in range(n)], pa.string()),
            "note": pa.array([f"note-{i}" for i in range(n)], pa.string()),
        }
    )


def string_parent_columns() -> list[dict[str, Any]]:
    return [{"name": "id", "type": "sequence", "start": 0, "step": 1}]


def mixed_config(
    tmp_path: Path,
    child: pa.Table,
    *,
    parent_columns: list[dict[str, Any]] | None = None,
    parent_rows: int = 5,
    policy: str = "preserve",
    child_columns: list[dict[str, Any]] | None = None,
    coupled: bool = True,
    extra_generate: bool = False,
    lookup_rows: int = 3,
    seed: int = 20261017,
) -> dict[str, Any]:
    """Generate parent `people` + mask child `accounts` (parquet source). `coupled`
    drops the relationships block, giving an INDEPENDENT mixed job."""
    path = tmp_path / f"{CHILD}.parquet"
    pq.write_table(child, path)
    columns = [{"name": FK, "strategy": "passthrough"}, *(child_columns or payload_columns())]
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {CHILD: {"type": "file", "format": "parquet", "path": str(path)}},
        "targets": {
            PARENT: {"type": "file", "format": "csv", "path": f"{PARENT}.out.csv"},
            CHILD: {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")},
        },
        "tables": [
            {
                "name": PARENT,
                "row_count": parent_rows,
                "generate_columns": parent_columns or string_parent_columns(),
            },
            {"name": CHILD, "columns": columns},
        ],
        "relationships": (
            [
                {
                    "parent": {"table": PARENT, "columns": ["id"]},
                    "children": [{"table": CHILD, "columns": [FK]}],
                    "orphan_policy": policy,
                    "namespace": "ns_people",
                }
            ]
            if coupled
            else []
        ),
    }
    if extra_generate:
        raw["tables"].append(
            {
                "name": "lookup",
                "row_count": lookup_rows,
                "generate_columns": [{"name": "code", "type": "sequence", "start": 0, "step": 1}],
            }
        )
        raw["targets"]["lookup"] = {"type": "file", "format": "csv", "path": "lookup.out.csv"}
    return PipelineConfig.model_validate(raw).model_dump()


def run(
    config: dict[str, Any],
    sources: dict[str, Any],
    *,
    mode: str = "auto",
    **kwargs: Any,
) -> ExecutionResult:
    merged = {**LANE_KW, **kwargs}
    return run_pipeline(
        copy.deepcopy(config),
        dict(sources),
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        execution_mode=mode,  # type: ignore[arg-type]
        **merged,
    )


def oracle(config: dict[str, Any], sources: dict[str, Any], **kwargs: Any) -> ExecutionResult:
    return run(config, sources, mode="full_frame", **kwargs)


def ipc_bytes(table: pa.Table) -> bytes:
    """Arrow IPC stream bytes with schema metadata stripped (the pandas metadata the
    oracle's round trip attaches is not output data)."""
    table = table.replace_schema_metadata(None).combine_chunks()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def assert_outputs_identical(got: dict[str, pa.Table], want: dict[str, pa.Table]) -> None:
    assert sorted(got) == sorted(want)
    for name in want:
        assert got[name].schema.equals(want[name].schema), name
        assert ipc_bytes(got[name]) == ipc_bytes(want[name]), name


def warning_signature(result: ExecutionResult) -> list[tuple[str, str, str]]:
    # Order-sensitive on purpose: the mixed route must emit the oracle's warnings in its order.
    return [(w.code, w.column, repr(sorted(w.detail.items()))) for w in result.warnings]


def mask_evidence(result: ExecutionResult, table: str = CHILD) -> dict[str, Any]:
    return result.quality_metrics["mixed_route"]["mask_tables"][table]["chunked_route"]


def executed_backends(result: ExecutionResult, table: str = CHILD) -> dict[str, str]:
    return {c["column"]: c["executed_backend"] for c in mask_evidence(result, table)["columns"]}


def chunk_count(result: ExecutionResult, table: str = CHILD) -> int:
    return max(int(c["calls"]) for c in mask_evidence(result, table)["columns"])


class RecordingSink:
    """A `TransactionalSink` that records every call and can fail on demand.

    `fail_on` names the operation to fail: "write", "write_batches", "commit",
    "write:<table>" / "write_batches:<table>", or "write_batches_late" (fail after one
    masked batch has been staged)."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.staged: dict[str, pa.Table] = {}
        self.published: dict[str, pa.Table] | None = None
        self.aborted = 0
        self.committed = 0
        self.fail_on = fail_on

    def _maybe_fail(self, op: str, table: str = "") -> None:
        if self.fail_on in (op, f"{op}:{table}"):
            raise RuntimeError(f"injected sink failure on {op} {table}".strip())

    def write(self, table: str, data: pa.Table) -> None:
        self.calls.append(("write", table))
        self._maybe_fail("write", table)
        self.staged[table] = data

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.calls.append(("write_batches", table))
        self._maybe_fail("write_batches", table)
        if self.fail_on == "write_batches_late":
            # Stage one masked batch, then fail: the generate table and a masked batch
            # are both already staged when the session breaks.
            first = next(iter(batches))
            self.staged[table] = pa.Table.from_batches([first], schema=schema)
            raise RuntimeError("injected sink failure after a staged masked batch")
        self.staged[table] = pa.Table.from_batches(list(batches), schema=schema)

    def commit(self) -> None:
        self.calls.append(("commit", ""))
        self._maybe_fail("commit")
        self.committed += 1
        self.published = dict(self.staged)

    def abort(self) -> None:
        self.calls.append(("abort", ""))
        self.aborted += 1
        self.staged = {}


def lazy_source(tmp_path: Path, table: pa.Table, *, row_group_size: int) -> Any:
    """A parquet-backed `LazySource` whose row groups (and so its batches) are uneven
    relative to the lane's chunk size."""
    from decoy_engine.profile._readers import LazySource

    path = tmp_path / "lazy_accounts.parquet"
    pq.write_table(table, path, row_group_size=row_group_size)
    return LazySource(path)


def tables_in_order(result: ExecutionResult) -> Iterator[str]:
    yield from result.outputs

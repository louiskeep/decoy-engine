"""Phase D / faker SEQ-native: shared fixtures and the oracle / native / Python trio runner.

Shapes reuse the D2c payload builder (`_d2c_cat_data.payload_shape`): the faker columns are
payload and the FK key is hashed. A masking cell runs three ways on identical inputs, the
full-frame oracle (the shipped Python faker handler), the SEQ route with the index kernel
available, and the SEQ route with the index kernel hidden (the Python fallback), and every
pair must agree on all nine strict dimensions. The native witness is the evidence entry under
`sequential_faker`, not the observation of a kernel call: the Python handler reaches the same
index kernel too.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._sequential_native_ops import SEQ_FAKER_DISPOSITION_KEY
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import NAMESPACES, Shape, strs
from tests.parity._d2c_cat_data import hide_cat, payload_shape

NS = "ns_f"
FIRST = "person_first_name"
LAST = "person_last_name"
POOL = 64
SRC = ["alice", "bob", None, "carol", "", "alice", "dave"]
ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
PASSTHROUGH = column_seed("passthrough", namespace=None)


def faker_seed(
    namespace: str | None = NS,
    *,
    provider: str = FIRST,
    deterministic: bool = True,
    mode: str = "reuse",
    pool_size: int | None = POOL,
) -> ColumnSeed:
    """A faker column seed. `deterministic` and the pool size are set explicitly, because the
    hash-shaped `column_seed` derives determinism from the namespace."""
    return replace(
        column_seed("faker", namespace=namespace),
        provider=provider,
        deterministic=deterministic,
        cardinality_mode=mode,  # type: ignore[arg-type]
        pool_size=pool_size,
    )


def positional_seed(namespace: str | None = NS, **kw: Any) -> ColumnSeed:
    return faker_seed(namespace, deterministic=False, **kw)


def gated(
    columns: dict[str, tuple[pa.Array, ColumnSeed]], flag: list[int], predicate: str
) -> Shape:
    """`columns` masked only where `flag` satisfies `predicate` (every faker column is gated)."""
    return payload_shape(
        columns,
        extra_cols={"flag": pa.array(flag, pa.int64())},
        extra_seeds={"flag": PASSTHROUGH},
        when=dict.fromkeys(columns, predicate),
    )


def with_job_seed(shape: Shape, job_seed: bytes) -> Shape:
    envelope = replace(shape.plan.seed_envelope, job_seed=job_seed)
    plan = type(shape.plan)(seed_envelope=envelope)
    return Shape(plan, shape.graph, shape.sources)


def run_seq_with(shape: Shape, registry: Any, key_provider: Any = None) -> Any:
    srcs = shape.sources
    return PandasExecutionAdapter().run_sequential(
        shape.plan,
        lambda t: srcs[t],
        registry=registry,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
        key_provider=key_provider,
    )


def run_full_with(shape: Shape, registry: Any, key_provider: Any = None) -> Any:
    return PandasExecutionAdapter().run(
        shape.plan,
        shape.sources,
        registry=registry,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
        key_provider=key_provider,
    )


@dataclass(frozen=True)
class Trio:
    oracle: Any
    native: Any
    python: Any


def run_trio(
    shape: Shape,
    monkeypatch: pytest.MonkeyPatch,
    registry: Any = None,
    key_provider: Any = None,
) -> Trio:
    """The oracle, the SEQ run as the environment allows, and the SEQ run with the index
    kernel hidden. Without a companion the second equals the third, which is still a valid
    parity cell and the witness tests assert on `native` only where it is loaded."""
    registry = registry or get_default_registry()
    oracle = run_full_with(shape, registry, key_provider)
    native = run_seq_with(shape, registry, key_provider)
    with monkeypatch.context() as hidden:
        hide_cat(hidden)
        python = run_seq_with(shape, registry, key_provider)
    return Trio(oracle, native, python)


def assert_trio_strict(trio: Trio, tables: tuple[str, ...] = ("P", "C")) -> None:
    for table in tables:
        for label, other in (("native", trio.native), ("python", trio.python)):
            report = sc.compare_strict(trio.oracle.outputs[table], other.outputs[table])
            assert report == ALL_TRUE, (table, label, report)
    # Warnings and row errors travel on their own channels, never inside the table.
    for other in (trio.native, trio.python):
        assert other.warnings == trio.oracle.warnings
        assert other.row_errors == trio.oracle.row_errors


def faker_entries(result: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = result.quality_metrics.get(SEQ_FAKER_DISPOSITION_KEY, [])
    return entries


def faker_entry(result: Any, column: str) -> dict[str, Any]:
    [entry] = [e for e in faker_entries(result) if e["column"] == column and e["path"] == "scalar"]
    return entry


def assert_native_witness(result: Any, column: str, *, calls: int = 1) -> None:
    entry = faker_entry(result, column)
    assert entry["executed_backend"] == "rust_companion", entry
    assert "reason" not in entry, entry
    assert entry["native_calls"] == calls, entry


def assert_python_ran(result: Any, column: str, reason: str) -> None:
    entry = faker_entry(result, column)
    assert entry["executed_backend"] == "python_scalar", entry
    assert entry["reason"] == reason, entry
    assert entry["native_calls"] == 0, entry


def cells(n: int, *, null_every: int = 7) -> pa.Array:
    return strs([None if i % null_every == 3 else f"name{i % 23}" for i in range(n)])

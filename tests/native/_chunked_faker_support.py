"""Shared builders for the C5b-ii tests (non-deterministic REUSE Faker on the chunked route).

The column under test is `f`, a non-deterministic `person_first_name` Faker with an explicit
`pool_size`, beside an integer passthrough `p`. The frozen KATs were captured once from the
scalar `derive_index(job_seed, selection_namespace, encode_int(g), pool.size)` over the pool
built with the CONFIGURED namespace, and are never recomputed by the code under test.
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import kernel
from decoy_engine.determinism import derive_index
from decoy_engine.generation.pool import PoolBuilder, ValuePool
from decoy_engine.generation.pool._identity import resolve_faker_pool_identity
from decoy_engine.plan import compile_plan
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import FORCE, Run, assert_same_as_oracle, run_one, run_pair
from tests.native._chunked_entry_support import (
    TABLE,
    make_config,
    passthrough,
    split,
)

__all__ = [
    "FORCE",
    "KAT_CONFIGURED",
    "KAT_DEFAULT",
    "KAT_EDGE_CONFIGURED",
    "KAT_EDGE_DEFAULT",
    "POOL_SIZE",
    "Run",
    "assert_same_as_oracle",
    "default_namespace",
    "expected_values",
    "job_seed_of",
    "make_config",
    "nd_faker",
    "passthrough",
    "pool_of",
    "run_one",
    "run_pair",
    "source",
    "split",
]

POOL_SIZE = 400

# Row g = 0..11, default namespace "faker-nd/1:t/1:f", pool built with namespace None.
KAT_DEFAULT = [
    "Jeffrey",
    "Michael",
    "Robert",
    "Kylie",
    "Rhonda",
    "Karen",
    "John",
    "Taylor",
    "Lawrence",
    "Benjamin",
    "Emily",
    "Angelica",
]
# Row g = 0..11, configured namespace "ns_f".
KAT_CONFIGURED = [
    "Darren",
    "Robert",
    "Omar",
    "James",
    "Cheryl",
    "Joshua",
    "Darren",
    "Suzanne",
    "Cathy",
    "Brian",
    "Wesley",
    "Isaiah",
]
# g in {2**63 - 1, 2**63, 2**64 - 1}; g = 0 is KAT_*[0].
KAT_EDGE_DEFAULT = {2**63 - 1: "Laura", 2**63: "John", 2**64 - 1: "Laura"}
KAT_EDGE_CONFIGURED = {2**63 - 1: "Christian", 2**63: "Miguel", 2**64 - 1: "Rebecca"}


def default_namespace(table: str, column: str) -> str:
    """The documented default selection namespace, written out (not via the helper under test)."""
    return f"faker-nd/{len(table)}:{table}/{len(column)}:{column}"


def nd_faker(
    name: str = "f",
    *,
    namespace: str | None = None,
    provider: str = "person_first_name",
    pool_size: int | None = POOL_SIZE,
    **extra: Any,
) -> dict[str, Any]:
    col: dict[str, Any] = {
        "name": name,
        "strategy": "faker",
        "provider": provider,
        "deterministic": False,
    }
    if pool_size is not None:
        col["pool_size"] = pool_size
    if namespace is not None:
        col["namespace"] = namespace
    col.update(extra)
    return col


def source(values: list[Any], *, typ: pa.DataType | None = None) -> pa.Table:
    """A table with the Faker source `f` and an integer passthrough `p`."""
    return pa.table(
        {
            "f": pa.array(values, typ or pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


def _with_source_file(config: dict[str, Any]) -> dict[str, Any]:
    cfg = copy.deepcopy(config)
    path = os.path.join(tempfile.mkdtemp(), "s.parquet")
    pq.write_table(pa.table({"f": ["a"] * 5, "p": [1] * 5}), path)
    cfg["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    return cfg


def job_seed_of(config: dict[str, Any]) -> bytes:
    cfg = _with_source_file(config)
    plan = compile_plan(cfg, profile_source(cfg, seed=42), decoy_engine_version="c5b-ii-test")
    return plan.seed_envelope.job_seed


def pool_of(
    *, namespace: str | None, job_seed: bytes, provider: str = "person_first_name"
) -> ValuePool:
    """The pool both routes build: CONFIGURED namespace, `job_seed`, never the selection one."""
    builder = PoolBuilder(get_default_registry())
    size, locale, build_config, _ = resolve_faker_pool_identity(
        builder=builder,
        provider=provider,
        plan_pool_size=POOL_SIZE,
        namespace=namespace,
        job_seed=job_seed,
        cfg={},
    )
    return builder.build(
        provider=provider,
        size=size,
        job_seed=job_seed,
        locale=locale,
        config=build_config,
        namespace=namespace,
    )


def expected_values(
    ordinals: Any,
    *,
    config: dict[str, Any],
    namespace: str | None,
    table: str = TABLE,
    column: str = "f",
    key: bytes | None = None,
    job_seed: bytes | None = None,
) -> list[str]:
    """The scalar `pool.values[derive_index(key, selection_ns, encode_int(g), size)]` per ordinal."""
    job_seed = job_seed_of(config) if job_seed is None else job_seed
    pool = pool_of(namespace=namespace, job_seed=job_seed)
    selection = namespace or default_namespace(table, column)
    use = job_seed if key is None else key
    return [
        pool.values[derive_index(use, selection, kernel.encode_int(g), pool_size=pool.size)]
        for g in ordinals
    ]


def write_source(table: pa.Table, path: Path) -> str:
    pq.write_table(table, str(path))
    return str(path)


def seed_of(namespace: str | None, *, deterministic: bool = False) -> Any:
    """The compiled `ColumnSeed` of a `person_first_name` Faker column, built directly."""
    from decoy_engine.plan._types import ColumnSeed

    return ColumnSeed(
        namespace=namespace,
        strategy="faker",
        provider="person_first_name",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=(("pool_size", POOL_SIZE),),
        coherent_with=(),
        when=None,
        scale=None,
        pool_size=None,
    )


def oracle_pool_for(seed: Any, job_seed: bytes) -> ValuePool:
    """The pool the oracle handler itself builds and caches for `seed`."""
    import pandas as pd

    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.execution._strategies._faker import FakerStrategyHandler
    from decoy_engine.generation.pool import PoolCache
    from decoy_engine.relationships._graph import RelationshipGraph
    from decoy_engine.relationships._namespace import NamespaceRegistry

    ctx = StrategyContext(
        registry=get_default_registry(),
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=job_seed,
        mask_key=job_seed,
        current_table=TABLE,
        row_offset=0,
    )
    FakerStrategyHandler().run(pd.DataFrame({"f": ["x", "y"]}), "f", seed, ctx)
    (pool,) = ctx.pool_cache._entries.values()
    return pool

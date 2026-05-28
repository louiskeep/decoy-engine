"""engine-v2 S9 slice 2g: FPE strategy (re-keyed Feistel + chunked parallelism).

The non-negotiable gate is byte-identical chunk_count=1 vs chunk_count=4 output.
Tested directly on the handler so the chunk count can be varied.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0xC0FFEE).to_bytes(8, "big")


def _ctx() -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        job_seed=_SEED,
    )


def _fpe_col(*, namespace: str | None = "fpe_ns") -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("charset", "digits"),),
        coherent_with=(),
    )


class TestFpe:
    def test_format_preserving_and_null(self) -> None:
        df = pd.DataFrame({"acct": ["12345", "67890", None]})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "acct", _fpe_col(), _ctx())
        vals = out["acct"].tolist()
        assert vals[2] is None
        for v in vals[:2]:
            assert len(v) == 5 and v.isdigit()

    def test_deterministic_same_value_same_output(self) -> None:
        df = pd.DataFrame({"acct": ["12345", "99999", "12345"]})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "acct", _fpe_col(), _ctx())
        vals = out["acct"].tolist()
        assert vals[0] == vals[2]  # same source -> same ciphertext

    def test_chunked_serial_parity(self) -> None:
        # The non-negotiable gate: chunk_count=1 and chunk_count=4 byte-identical.
        rows = [f"{i:05d}" for i in range(50)]
        serial, _ = FpeStrategyHandler(chunk_count=1).run(
            pd.DataFrame({"acct": list(rows)}), "acct", _fpe_col(), _ctx()
        )
        parallel, _ = FpeStrategyHandler(chunk_count=4).run(
            pd.DataFrame({"acct": list(rows)}), "acct", _fpe_col(), _ctx()
        )
        assert serial["acct"].tolist() == parallel["acct"].tolist()

    def test_requires_namespace(self) -> None:
        df = pd.DataFrame({"acct": ["12345"]})
        with pytest.raises(ExecutionError) as exc:
            FpeStrategyHandler().run(df, "acct", _fpe_col(namespace=None), _ctx())
        assert exc.value.code == "fpe_requires_namespace"

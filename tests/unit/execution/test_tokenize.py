"""tokenize strategy (MG-5 / vault V0): reversible tokenization via a vault.

Drives the real planning + execution path
(``compile_plan`` -> ``PandasExecutionAdapter.run(token_vault_writer=...)``) with
a fake ``TokenVaultWriter``, so the token shape, vault idempotency, null
handling, keying, determinism and the fail-closed-without-a-vault contract are
all exercised end-to-end at the adapter boundary (the platform injects a real
writer later).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.plan import compile_plan
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import build_namespace_registry

_VERSION = "0.1.0"


class FakeVault:
    """In-memory TokenVaultWriter: records puts, serves lookups."""

    def __init__(self, preloaded: dict[str, str] | None = None) -> None:
        self.map: dict[str, str] = dict(preloaded or {})
        self.puts: list[tuple[str, str]] = []
        self.lookups: list[str] = []

    def lookup_existing(self, plaintext: str) -> str | None:
        self.lookups.append(plaintext)
        return self.map.get(plaintext)

    def put(self, plaintext: str, token: str) -> None:
        self.puts.append((plaintext, token))
        self.map[plaintext] = token


class FakeBatchVault:
    """BatchTokenVaultWriter: serves one batched lookup + one bulk put, and
    counts each batch call so a test can prove the per-cell round-trip is gone."""

    def __init__(self, preloaded: dict[str, str] | None = None) -> None:
        self.map: dict[str, str] = dict(preloaded or {})
        self.lookup_batches: list[list[str]] = []
        self.put_batches: list[dict[str, str]] = []

    def lookup_existing_many(self, plaintexts):
        self.lookup_batches.append(list(plaintexts))
        return {p: self.map[p] for p in plaintexts if p in self.map}

    def put_many(self, pairs) -> None:
        self.put_batches.append(dict(pairs))
        self.map.update(pairs)


def _col(df: pd.DataFrame, name: str) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=len(df),
        null_count=int(df[name].isna().sum()),
        distinct_count=int(df[name].nunique()),
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


def _run(df: pd.DataFrame, namespace: str | None = "pan_ns", *, vault: Any) -> dict[str, list]:
    profile = Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=len(df), columns=(_col(df, "pan"),)),),
        relationships=(),
        profiled_at=datetime(2026, 10, 23),
        decoy_engine_version=_VERSION,
    )
    pan_col: dict[str, Any] = {"name": "pan", "strategy": "tokenize"}
    if namespace is not None:
        pan_col["namespace"] = namespace
    config = {
        "global_settings": {"seed": 7},
        "tables": [{"name": "t", "columns": [pan_col]}],
    }
    plan = compile_plan(config, profile, decoy_engine_version=_VERSION)
    ns = build_namespace_registry(config, profile)
    res = PandasExecutionAdapter().run(
        plan,
        {"t": pa.Table.from_pandas(df, preserve_index=False)},
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=ns,
        token_vault_writer=vault,
    )
    return res.outputs["t"].to_pydict()


def test_tokens_have_expected_shape_and_are_vaulted() -> None:
    df = pd.DataFrame({"pan": ["4111111111111111", "5500000000000004"]})
    vault = FakeVault()
    out = _run(df, vault=vault)
    for tok in out["pan"]:
        assert tok.startswith("tok_") and len(tok) == len("tok_") + 32
    # every (plaintext, token) pair was recorded in the vault
    assert dict(vault.puts) == {
        "4111111111111111": out["pan"][0],
        "5500000000000004": out["pan"][1],
    }


def test_null_is_preserved_and_not_vaulted() -> None:
    df = pd.DataFrame({"pan": ["abc", None, "def"]})
    vault = FakeVault()
    out = _run(df, vault=vault)
    assert out["pan"][1] is None
    assert [p for p, _ in vault.puts] == ["abc", "def"]  # null never reached the vault


def test_repeated_plaintext_is_idempotent_one_vault_write() -> None:
    df = pd.DataFrame({"pan": ["x", "x", "x"]})
    vault = FakeVault()
    out = _run(df, vault=vault)
    assert out["pan"][0] == out["pan"][1] == out["pan"][2]  # stable token
    assert len(vault.puts) == 1  # lookup_existing short-circuits the 2nd/3rd


def test_vault_is_authoritative_existing_token_reused() -> None:
    # A token already stored for "x" must be reused verbatim, not recomputed.
    vault = FakeVault(preloaded={"x": "tok_PREEXISTING"})
    out = _run(pd.DataFrame({"pan": ["x", "y"]}), vault=vault)
    assert out["pan"][0] == "tok_PREEXISTING"
    assert out["pan"][1].startswith("tok_") and out["pan"][1] != "tok_PREEXISTING"
    assert vault.puts == [("y", out["pan"][1])]  # only the new value was written


def test_namespace_changes_the_token() -> None:
    df = pd.DataFrame({"pan": ["x"]})
    a = _run(df, namespace="ns_a", vault=FakeVault())["pan"][0]
    b = _run(df, namespace="ns_b", vault=FakeVault())["pan"][0]
    assert a != b  # keyed on namespace -> different vault/namespace tokenizes apart


def test_deterministic_across_runs_same_seed() -> None:
    df = pd.DataFrame({"pan": ["x", "y", "z"]})
    a = _run(df, vault=FakeVault())
    b = _run(df, vault=FakeVault())
    assert a == b


def test_fails_closed_without_a_vault() -> None:
    df = pd.DataFrame({"pan": ["x"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=None)
    assert (
        "tokenize_requires_vault" in str(exc.value)
        or getattr(exc.value, "code", "") == "tokenize_requires_vault"
    )


def test_fails_closed_without_a_namespace() -> None:
    df = pd.DataFrame({"pan": ["x"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, namespace=None, vault=FakeVault())
    assert (
        "tokenize_requires_namespace" in str(exc.value)
        or getattr(exc.value, "code", "") == "tokenize_requires_namespace"
    )


# --- MG-5 V1 perf: batched vault I/O (BatchTokenVaultWriter) ----------------


def test_batch_path_one_lookup_one_put_over_distinct() -> None:
    # Repeats + distinct values resolve in exactly ONE lookup and ONE bulk put,
    # the put carrying only the distinct misses (no per-cell round-trip).
    df = pd.DataFrame({"pan": ["a", "b", "a", "c", "b"]})
    vault = FakeBatchVault()
    out = _run(df, vault=vault)
    assert len(vault.lookup_batches) == 1
    assert vault.lookup_batches[0] == ["a", "b", "c"]  # distinct, order-stable
    assert len(vault.put_batches) == 1
    assert set(vault.put_batches[0]) == {"a", "b", "c"}  # only distinct misses
    assert out["pan"][0] == out["pan"][2]  # repeats share one token
    assert out["pan"][1] == out["pan"][4]
    for tok in out["pan"]:
        assert tok.startswith("tok_") and len(tok) == len("tok_") + 32


def test_batch_path_authoritative_reuse_excludes_hits_from_put() -> None:
    vault = FakeBatchVault(preloaded={"x": "tok_PREEXISTING"})
    out = _run(pd.DataFrame({"pan": ["x", "y"]}), vault=vault)
    assert out["pan"][0] == "tok_PREEXISTING"  # stored token wins verbatim
    assert out["pan"][1].startswith("tok_") and out["pan"][1] != "tok_PREEXISTING"
    assert len(vault.lookup_batches) == 1
    assert set(vault.put_batches[0]) == {"y"}  # only the miss is written


def test_batch_path_nulls_preserved_and_never_vaulted() -> None:
    vault = FakeBatchVault()
    out = _run(pd.DataFrame({"pan": ["abc", None, "def"]}), vault=vault)
    assert out["pan"][1] is None
    assert vault.lookup_batches[0] == ["abc", "def"]  # null never reached the vault
    assert set(vault.put_batches[0]) == {"abc", "def"}


def test_batch_path_all_null_does_no_vault_io() -> None:
    vault = FakeBatchVault()
    out = _run(pd.DataFrame({"pan": [None, None]}), vault=vault)
    assert out["pan"] == [None, None]
    assert vault.lookup_batches == [] and vault.put_batches == []


def test_batch_and_per_cell_produce_identical_tokens() -> None:
    # The token for a value must not depend on which seam the writer exposes:
    # the batch path mints the same deterministic surrogate as the per-cell path.
    df = pd.DataFrame({"pan": ["x", "y", "z"]})
    per_cell = _run(df, vault=FakeVault())
    batched = _run(df, vault=FakeBatchVault())
    assert per_cell == batched


# --- V0b: run_pipeline exposure + full-frame routing ------------------------


def test_run_pipeline_threads_vault_and_forces_full_frame(tmp_path: Any) -> None:
    """The platform injects the token vault via run_pipeline. A tokenize job must
    reach the full-frame adapter (the bounded routes don't thread the vault), so
    run_pipeline forces full-frame + no auto-chunk when a token vault is passed."""
    import pyarrow.parquet as pq

    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline
    from decoy_engine.keyprovider import SecretKeyProvider

    tbl = pa.table({"pan": ["4111", "5500", "4111"]})
    src = tmp_path / "in.parquet"
    pq.write_table(tbl, src)
    raw = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(src)}},
        "targets": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "o.parquet")}
        },
        "tables": [
            {"name": "t", "columns": [{"name": "pan", "strategy": "tokenize", "namespace": "ns"}]}
        ],
    }
    cfg = PipelineConfig.model_validate(raw).model_dump()
    vault = FakeVault()
    res = run_pipeline(
        cfg,
        {"t": tbl},
        engine_version="v0b",
        key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
        token_vault_writer=vault,
    )
    out = res.outputs["t"].column("pan").to_pylist()
    assert all(t.startswith("tok_") for t in out)
    assert out[0] == out[2]  # idempotent surrogate for the repeated value
    assert len(vault.puts) == 2  # two distinct plaintexts written
    assert res.quality_metrics["execution"]["execution_mode"] == "full_frame"


def test_run_pipeline_without_vault_fails_closed(tmp_path: Any) -> None:
    import pyarrow.parquet as pq

    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline

    tbl = pa.table({"pan": ["x"]})
    src = tmp_path / "in.parquet"
    pq.write_table(tbl, src)
    raw = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(src)}},
        "targets": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "o.parquet")}
        },
        "tables": [
            {"name": "t", "columns": [{"name": "pan", "strategy": "tokenize", "namespace": "ns"}]}
        ],
    }
    cfg = PipelineConfig.model_validate(raw).model_dump()
    with pytest.raises(ExecutionError) as exc:
        run_pipeline(cfg, {"t": tbl}, engine_version="v0b")  # no token vault
    assert (
        "tokenize_requires_vault" in str(exc.value)
        or getattr(exc.value, "code", "") == "tokenize_requires_vault"
    )

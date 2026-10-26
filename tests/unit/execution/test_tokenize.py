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


# --- HIGH-1: V0 canonicalization guard (string/object dtype only) ----------


def test_fails_closed_on_an_int_dtype_column() -> None:
    # A plain int64 column (no nulls, so pandas has not even widened it) is still
    # not string/object dtype; V0 tokenize must reject it rather than canonicalize
    # a raw str(value) non-deterministically relative to mask.hash's contract.
    df = pd.DataFrame({"pan": [1, 2, 3]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=FakeVault())
    assert (
        "tokenize_requires_string_dtype" in str(exc.value)
        or getattr(exc.value, "code", "") == "tokenize_requires_string_dtype"
    )


def test_fails_closed_on_a_null_widened_int_column() -> None:
    # The exact hazard HIGH-1 names: an int column with a null widens to float64
    # under pandas. Pre-fix this would have silently tokenized "123.0"; V0 fails
    # closed on the dtype instead of ever reaching a raw str(value) on it.
    df = pd.DataFrame({"pan": pd.array([123, None, 456], dtype="Int64")})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=FakeVault())
    assert (
        "tokenize_requires_string_dtype" in str(exc.value)
        or getattr(exc.value, "code", "") == "tokenize_requires_string_dtype"
    )


def test_fails_closed_on_a_datetime_column() -> None:
    df = pd.DataFrame({"pan": pd.to_datetime(["2026-01-01", "2026-01-02"])})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=FakeVault())
    assert (
        "tokenize_requires_string_dtype" in str(exc.value)
        or getattr(exc.value, "code", "") == "tokenize_requires_string_dtype"
    )


def test_unicode_equivalent_strings_tokenize_identically() -> None:
    # "e" + combining acute (NFD) vs the precomposed "e-acute" (NFC) are the same
    # logical string; mask.hash's canonicalizer NFC-normalizes before deriving, and
    # tokenize must match so the SAME plaintext is never vaulted under two different
    # byte forms (breaking idempotency and FK joinability for a reversible primitive).
    nfd = "café"  # "café" with a combining acute accent (NFD-ish)
    nfc = "café"  # "café" precomposed (NFC)
    assert nfd != nfc  # distinct byte forms of the same logical string
    vault = FakeVault()
    out = _run(pd.DataFrame({"pan": [nfd, nfc]}), vault=vault)
    assert out["pan"][0] == out["pan"][1]
    assert len(vault.puts) == 1  # one canonical plaintext, one vault write


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


# --- MEDIUM-1: writer-raises fail-closed (R2 persistence-seam contract) ----


class _RaisingVault(FakeVault):
    """A FakeVault whose `put` or `lookup_existing` raises, to prove a writer
    failure aborts the whole run rather than leaking a partial result."""

    def __init__(self, *, raise_on: str) -> None:
        super().__init__()
        self._raise_on = raise_on

    def lookup_existing(self, plaintext: str) -> str | None:
        if self._raise_on == "lookup_existing":
            raise RuntimeError("vault lookup_existing boom")
        return super().lookup_existing(plaintext)

    def put(self, plaintext: str, token: str) -> None:
        if self._raise_on == "put":
            raise RuntimeError("vault put boom")
        super().put(plaintext, token)


def _run_through_transactional_sink(df: pd.DataFrame, vault: Any, target_dir: Any) -> None:
    """Mirror `run_sequential`'s own commit discipline: write + commit the sink
    only AFTER masking succeeds; abort (and re-raise) if it does not. A sink
    that aborts without ever writing or committing is the proof that a writer
    failure mid-mask never reaches the point of publishing a partial output."""
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

    sink = ParquetTransactionalSink(target_dir)
    profile = Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=len(df), columns=(_col(df, "pan"),)),),
        relationships=(),
        profiled_at=datetime(2026, 10, 23),
        decoy_engine_version=_VERSION,
    )
    config = {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "t", "columns": [{"name": "pan", "strategy": "tokenize", "namespace": "ns"}]}
        ],
    }
    plan = compile_plan(config, profile, decoy_engine_version=_VERSION)
    ns = build_namespace_registry(config, profile)
    try:
        res = PandasExecutionAdapter().run(
            plan,
            {"t": pa.Table.from_pandas(df, preserve_index=False)},
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=ns,
            token_vault_writer=vault,
        )
    except Exception:
        sink.abort()
        raise
    for name, table in res.outputs.items():
        sink.write(name, table)
    sink.commit()


@pytest.mark.parametrize("raise_on", ["put", "lookup_existing"])
def test_vault_writer_raising_aborts_the_run_with_no_partial_output(
    raise_on: str, tmp_path: Any
) -> None:
    df = pd.DataFrame({"pan": ["4111111111111111", "5500000000000004"]})
    vault = _RaisingVault(raise_on=raise_on)
    target_dir = tmp_path / "out"
    with pytest.raises(RuntimeError, match="vault .* boom"):
        _run_through_transactional_sink(df, vault, target_dir)
    # No output table was produced (the adapter.run() call never returned), and the
    # transactional sink never committed -- its target directory was never created,
    # so there is no partial output to find.
    assert not target_dir.exists()


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

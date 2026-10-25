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
    """In-memory TokenVaultWriter: records puts, serves lookups, scoped by namespace.

    Identity is the ``(namespace, plaintext)`` pair, so the same plaintext under
    two namespaces is two distinct entries with two distinct tokens."""

    def __init__(self, preloaded: dict[tuple[str, str], str] | None = None) -> None:
        self.map: dict[tuple[str, str], str] = dict(preloaded or {})
        self.puts: list[tuple[str, str, str]] = []
        self.lookups: list[tuple[str, str]] = []

    def lookup_existing(self, namespace: str, plaintext: str) -> str | None:
        self.lookups.append((namespace, plaintext))
        return self.map.get((namespace, plaintext))

    def put(self, namespace: str, plaintext: str, token: str) -> str:
        self.puts.append((namespace, plaintext, token))
        return self.map.setdefault((namespace, plaintext), token)


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
    # every (namespace, plaintext) -> token pair was recorded in the vault
    assert {(ns, p): t for ns, p, t in vault.puts} == {
        ("pan_ns", "4111111111111111"): out["pan"][0],
        ("pan_ns", "5500000000000004"): out["pan"][1],
    }


def test_null_is_preserved_and_not_vaulted() -> None:
    df = pd.DataFrame({"pan": ["abc", None, "def"]})
    vault = FakeVault()
    out = _run(df, vault=vault)
    assert out["pan"][1] is None
    assert [p for _ns, p, _t in vault.puts] == ["abc", "def"]  # null never reached the vault


def test_repeated_plaintext_is_idempotent_one_vault_write() -> None:
    df = pd.DataFrame({"pan": ["x", "x", "x"]})
    vault = FakeVault()
    out = _run(df, vault=vault)
    assert out["pan"][0] == out["pan"][1] == out["pan"][2]  # stable token
    assert len(vault.puts) == 1  # lookup_existing short-circuits the 2nd/3rd


def test_vault_is_authoritative_existing_token_reused() -> None:
    # A token already stored for "x" must be reused verbatim, not recomputed.
    vault = FakeVault(preloaded={("pan_ns", "x"): "tok_PREEXISTING"})
    out = _run(pd.DataFrame({"pan": ["x", "y"]}), vault=vault)
    assert out["pan"][0] == "tok_PREEXISTING"
    assert out["pan"][1].startswith("tok_") and out["pan"][1] != "tok_PREEXISTING"
    assert vault.puts == [("pan_ns", "y", out["pan"][1])]  # only the new value was written


def test_namespace_changes_the_token() -> None:
    df = pd.DataFrame({"pan": ["x"]})
    a = _run(df, namespace="ns_a", vault=FakeVault())["pan"][0]
    b = _run(df, namespace="ns_b", vault=FakeVault())["pan"][0]
    assert a != b  # keyed on namespace -> different vault/namespace tokenizes apart


def test_one_shared_vault_does_not_collide_across_namespaces() -> None:
    # The same plaintext under two namespaces, through ONE shared vault, must get
    # two distinct tokens: a plaintext-only vault key would make the second lookup
    # reuse the first namespace's token and link the namespaces (Codex HIGH-2).
    vault = FakeVault()
    x = "4111111111111111"
    a = _run(pd.DataFrame({"pan": [x]}), namespace="ns_a", vault=vault)["pan"][0]
    b = _run(pd.DataFrame({"pan": [x]}), namespace="ns_b", vault=vault)["pan"][0]
    assert a != b  # no cross-namespace collision through the shared vault
    # both entries are recorded, each scoped by its own namespace
    assert {(ns, p): t for ns, p, t in vault.puts} == {("ns_a", x): a, ("ns_b", x): b}


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

    def lookup_existing(self, namespace: str, plaintext: str) -> str | None:
        if self._raise_on == "lookup_existing":
            # echo the plaintext, as a real database error would, to prove the
            # engine does not forward it into run_pipeline's exception.
            raise RuntimeError(f"vault lookup_existing boom for {plaintext}")
        return super().lookup_existing(namespace, plaintext)

    def put(self, namespace: str, plaintext: str, token: str) -> str:
        if self._raise_on == "put":
            raise RuntimeError(f"vault put boom for {plaintext} -> {token}")
        return super().put(namespace, plaintext, token)


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
    with pytest.raises(ExecutionError) as exc:
        _run_through_transactional_sink(df, vault, target_dir)
    # Fail-closed with a coded, run-aborting error (not the raw vault exception).
    assert getattr(exc.value, "code", "") == "tokenize_vault_error"
    # No output table was produced (the adapter.run() call never returned), and the
    # transactional sink never committed -- its target directory was never created,
    # so there is no partial output to find.
    assert not target_dir.exists()
    # The raw vault error echoed the plaintext (and token); the engine must not
    # forward it. The coded error carries neither the source value nor the token,
    # and the original exception chain is suppressed (Codex HIGH-3).
    msg = str(exc.value)
    assert "4111111111111111" not in msg and "5500000000000004" not in msg
    assert "boom" not in msg


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


def test_tokenize_at_ga_requires_a_secret_key(tmp_path: Any) -> None:
    # tokenize persists a reversible mapping, so its token must come from the
    # secret mask key, never the public job seed. At GA a tokenize job with no
    # secret must be rejected (keyed_strategy_requires_secret), not derive
    # dictionary-attackable tokens from the seed (Codex BLOCKER-1).
    from unittest.mock import patch

    import pyarrow.parquet as pq

    import decoy_engine
    from decoy_engine.config import PipelineConfig
    from decoy_engine.keyprovider import KeyedStrategyRequiresSecret

    tbl = pa.table({"pan": ["4111", "5500"]})
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
    # GA (not pre-GA), a vault present but NO secret key provider.
    with patch("decoy_engine.keyprovider.is_pre_ga", return_value=False):
        with pytest.raises(KeyedStrategyRequiresSecret) as exc:
            decoy_engine.run_pipeline(
                cfg, {"t": tbl}, engine_version="ga", token_vault_writer=FakeVault()
            )
    assert getattr(exc.value, "code", "") == "keyed_strategy_requires_secret"


# --- B-6 durability: put() returns the AUTHORITATIVE token --------------------


class _RacedVault(FakeVault):
    """Lookup misses, but a concurrent writer committed first: ``put`` returns the
    winner's token, which differs from the proposal."""

    WINNER = "tok_" + "ab" * 16

    def put(self, namespace: str, plaintext: str, token: str) -> str:
        self.puts.append((namespace, plaintext, token))
        self.map[(namespace, plaintext)] = self.WINNER
        return self.WINNER


def test_emits_authoritative_token_not_local_proposal() -> None:
    df = pd.DataFrame({"pan": ["4111111111111111", "5500000000000004"]})
    vault = _RacedVault()
    out = _run(df, vault=vault)
    assert out["pan"] == [_RacedVault.WINNER, _RacedVault.WINNER]
    # the proposal really differed, so emitting it would have been the orphan bug
    assert all(proposed != _RacedVault.WINNER for _ns, _p, proposed in vault.puts)


class _BadReturnVault(FakeVault):
    def __init__(self, bad: Any) -> None:
        super().__init__()
        self._bad = bad

    def put(self, namespace: str, plaintext: str, token: str) -> Any:
        self.puts.append((namespace, plaintext, token))
        return self._bad


@pytest.mark.parametrize("bad", [None, "", 123, b"tok_x"])
def test_non_str_or_empty_put_return_fails_closed(bad: Any) -> None:
    df = pd.DataFrame({"pan": ["secret-value"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=_BadReturnVault(bad))
    assert getattr(exc.value, "code", "") == "tokenize_vault_error"
    assert "secret-value" not in str(exc.value)


# --- B-6 batch seam: BatchTokenVaultWriter (lookup_existing_many / put_many) ---


class FakeBatchVault:
    """BatchTokenVaultWriter: one namespace-scoped batched lookup + one bulk put per
    column, recording each batch call so a test can prove the per-cell round-trip
    is gone. Deliberately exposes NO per-row methods, so any per-row call fails."""

    def __init__(self, preloaded: dict[tuple[str, str], str] | None = None) -> None:
        self.map: dict[tuple[str, str], str] = dict(preloaded or {})
        self.lookup_batches: list[tuple[str, list[str]]] = []
        self.put_batches: list[tuple[str, dict[str, str]]] = []

    def lookup_existing_many(self, namespace: str, plaintexts: Any) -> dict[str, str]:
        self.lookup_batches.append((namespace, list(plaintexts)))
        return {p: self.map[(namespace, p)] for p in plaintexts if (namespace, p) in self.map}

    def put_many(self, namespace: str, pairs: Any) -> dict[str, str]:
        self.put_batches.append((namespace, dict(pairs)))
        out: dict[str, str] = {}
        for p, t in pairs.items():
            out[p] = self.map.setdefault((namespace, p), t)
        return out


def test_batch_path_one_lookup_one_put_over_distinct() -> None:
    # Repeats + distinct values resolve in exactly ONE lookup and ONE bulk put,
    # the put carrying only the distinct misses (no per-cell round-trip).
    df = pd.DataFrame({"pan": ["a", "b", "a", "c", "b"]})
    vault = FakeBatchVault()
    out = _run(df, vault=vault)
    assert vault.lookup_batches == [("pan_ns", ["a", "b", "c"])]  # distinct, order-stable
    assert len(vault.put_batches) == 1
    assert vault.put_batches[0][0] == "pan_ns"
    assert set(vault.put_batches[0][1]) == {"a", "b", "c"}  # only distinct misses
    assert out["pan"][0] == out["pan"][2]  # repeats share one token
    assert out["pan"][1] == out["pan"][4]
    for tok in out["pan"]:
        assert tok.startswith("tok_") and len(tok) == len("tok_") + 32


def test_batch_path_authoritative_reuse_excludes_hits_from_put() -> None:
    vault = FakeBatchVault(preloaded={("pan_ns", "x"): "tok_PREEXISTING"})
    out = _run(pd.DataFrame({"pan": ["x", "y"]}), vault=vault)
    assert out["pan"][0] == "tok_PREEXISTING"  # stored token wins verbatim
    assert out["pan"][1].startswith("tok_") and out["pan"][1] != "tok_PREEXISTING"
    assert len(vault.lookup_batches) == 1
    assert set(vault.put_batches[0][1]) == {"y"}  # only the miss is written


def test_batch_path_nulls_preserved_and_never_vaulted() -> None:
    vault = FakeBatchVault()
    out = _run(pd.DataFrame({"pan": ["abc", None, "def"]}), vault=vault)
    assert out["pan"][1] is None
    assert vault.lookup_batches == [("pan_ns", ["abc", "def"])]  # null never reached the vault
    assert set(vault.put_batches[0][1]) == {"abc", "def"}


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


def test_batch_path_scopes_both_calls_by_namespace() -> None:
    # One shared vault, two namespaces: both batch calls carry their namespace,
    # and the same plaintext yields two distinct entries with two distinct tokens.
    vault = FakeBatchVault()
    a = _run(pd.DataFrame({"pan": ["same"]}), namespace="ns_a", vault=vault)
    b = _run(pd.DataFrame({"pan": ["same"]}), namespace="ns_b", vault=vault)
    assert a["pan"][0] != b["pan"][0]
    assert [ns for ns, _ in vault.lookup_batches] == ["ns_a", "ns_b"]
    assert [ns for ns, _ in vault.put_batches] == ["ns_a", "ns_b"]
    assert set(vault.map) == {("ns_a", "same"), ("ns_b", "same")}


def test_batch_path_nfc_normalizes_before_the_lookup() -> None:
    composed, decomposed = "café", "café"
    vault = FakeBatchVault()
    out = _run(pd.DataFrame({"pan": [composed, decomposed]}), vault=vault)
    assert out["pan"][0] == out["pan"][1]
    assert vault.lookup_batches == [("pan_ns", [composed])]  # one distinct, NFC form
    assert list(vault.put_batches[0][1]) == [composed]


class _RacedBatchVault(FakeBatchVault):
    """``put_many`` reports a concurrent winner's token for every miss."""

    WINNER = "tok_" + "cd" * 16

    def put_many(self, namespace: str, pairs: Any) -> dict[str, str]:
        self.put_batches.append((namespace, dict(pairs)))
        return dict.fromkeys(pairs, self.WINNER)


def test_batch_path_emits_the_authoritative_tokens_put_many_returns() -> None:
    vault = _RacedBatchVault()
    out = _run(pd.DataFrame({"pan": ["4111111111111111", "5500000000000004"]}), vault=vault)
    assert out["pan"] == [_RacedBatchVault.WINNER, _RacedBatchVault.WINNER]
    assert all(t != _RacedBatchVault.WINNER for t in vault.put_batches[0][1].values())


class _BadPutManyVault(FakeBatchVault):
    def __init__(self, shape: str) -> None:
        super().__init__()
        self._shape = shape

    def put_many(self, namespace: str, pairs: Any) -> Any:
        self.put_batches.append((namespace, dict(pairs)))
        if self._shape == "missing_key":
            return {}
        if self._shape == "not_a_mapping":
            return list(pairs)
        if self._shape == "extra_key":
            return {**pairs, "never-asked": "tok_" + "ee" * 16}
        return dict.fromkeys(
            pairs, {"none": None, "empty": "", "int": 123, "bytes": b"tok_x"}[self._shape]
        )


@pytest.mark.parametrize(
    "shape", ["missing_key", "not_a_mapping", "extra_key", "none", "empty", "int", "bytes"]
)
def test_batch_put_many_bad_return_fails_closed_without_leaking(shape: str) -> None:
    df = pd.DataFrame({"pan": ["secret-value"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=_BadPutManyVault(shape))
    assert getattr(exc.value, "code", "") == "tokenize_vault_error"
    assert "secret-value" not in str(exc.value)


class _BadLookupManyVault(FakeBatchVault):
    def __init__(self, shape: str) -> None:
        super().__init__()
        self._shape = shape

    def lookup_existing_many(self, namespace: str, plaintexts: Any) -> Any:
        if self._shape == "not_a_mapping":
            return [(p, "tok_x") for p in plaintexts]
        if self._shape == "extra_key":
            return {"never-asked": "tok_" + "ee" * 16}
        return dict.fromkeys(plaintexts, {"none": None, "empty": "", "int": 7}[self._shape])


@pytest.mark.parametrize("shape", ["not_a_mapping", "extra_key", "none", "empty", "int"])
def test_batch_lookup_many_bad_return_fails_closed_without_leaking(shape: str) -> None:
    df = pd.DataFrame({"pan": ["secret-value"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=_BadLookupManyVault(shape))
    assert getattr(exc.value, "code", "") == "tokenize_vault_error"
    assert "secret-value" not in str(exc.value)


class _RaisingBatchVault(FakeBatchVault):
    def __init__(self, *, raise_on: str) -> None:
        super().__init__()
        self._raise_on = raise_on

    def lookup_existing_many(self, namespace: str, plaintexts: Any) -> dict[str, str]:
        if self._raise_on == "lookup_existing_many":
            raise RuntimeError(f"vault lookup boom for {list(plaintexts)}")
        return super().lookup_existing_many(namespace, plaintexts)

    def put_many(self, namespace: str, pairs: Any) -> dict[str, str]:
        if self._raise_on == "put_many":
            raise RuntimeError(f"vault put boom for {dict(pairs)}")
        return super().put_many(namespace, pairs)


@pytest.mark.parametrize("raise_on", ["lookup_existing_many", "put_many"])
def test_batch_writer_raising_is_a_coded_error_that_names_no_value(raise_on: str) -> None:
    df = pd.DataFrame({"pan": ["secret-value", "other-secret"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=_RaisingBatchVault(raise_on=raise_on))
    assert getattr(exc.value, "code", "") == "tokenize_vault_error"
    text = (
        repr(exc.value) + str(exc.value) + repr(exc.value.__cause__) + repr(exc.value.__context__)
    )
    assert "secret-value" not in text and "other-secret" not in text and "tok_" not in text


class _HalfBatchVault(FakeVault):
    """A per-row writer that grew only ``lookup_existing_many``: not a batch writer."""

    def __init__(self) -> None:
        super().__init__()
        self.batch_lookups = 0

    def lookup_existing_many(self, namespace: str, plaintexts: Any) -> dict[str, str]:
        self.batch_lookups += 1
        return {}


def test_half_implemented_batch_writer_takes_the_per_row_path_whole() -> None:
    # Either both batch methods are used or neither: a writer that exposes one
    # of them must not get a mixed batch-lookup / per-row-put shape.
    vault = _HalfBatchVault()
    out = _run(pd.DataFrame({"pan": ["x", "y"]}), vault=vault)
    assert vault.batch_lookups == 0
    assert [p for _ns, p, _t in vault.puts] == ["x", "y"]
    assert len(out["pan"]) == 2


@pytest.mark.parametrize("raise_on", ["lookup_existing", "put"])
def test_per_row_writer_raising_leaves_no_value_on_the_exception_chain(raise_on: str) -> None:
    # `raise ... from None` clears __cause__ but Python still attaches the
    # writer's exception as __context__; anything that walks the chain (a
    # structured error serializer, a crash reporter) would see the plaintext.
    df = pd.DataFrame({"pan": ["secret-value", "other-secret"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=_RaisingVault(raise_on=raise_on))
    assert getattr(exc.value, "code", "") == "tokenize_vault_error"
    chain: list[BaseException] = []
    seen: BaseException | None = exc.value
    while seen is not None and seen not in chain:
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__
    text = "".join(repr(e) + str(e) + repr(getattr(e, "args", ())) for e in chain)
    assert "secret-value" not in text and "other-secret" not in text and "tok_" not in text


class _BadLookupVault(FakeVault):
    def __init__(self, bad: Any) -> None:
        super().__init__()
        self._bad = bad

    def lookup_existing(self, namespace: str, plaintext: str) -> Any:
        return self._bad


@pytest.mark.parametrize("bad", ["", 123, b"tok_x", ["tok_x"]])
def test_per_row_lookup_returning_a_non_token_fails_closed(bad: Any) -> None:
    # Same untrusted-producer rule as put(): a lookup hit must be a non-empty str
    # or the run aborts with the coded, value-free error (never publishes it).
    df = pd.DataFrame({"pan": ["secret-value"]})
    with pytest.raises(ExecutionError) as exc:
        _run(df, vault=_BadLookupVault(bad))
    assert getattr(exc.value, "code", "") == "tokenize_vault_error"
    assert "secret-value" not in str(exc.value)

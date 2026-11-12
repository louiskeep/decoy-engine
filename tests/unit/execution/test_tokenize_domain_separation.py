"""tokenize derives under the vault's own scope and outside the `hash` PRF domain (B-6 D2, D3).

T1: two vaults with the same mask key, namespace and plaintext mint different tokens; one vault
mints the same token run after run; `hash` and `tokenize` outputs differ for the same input.
T2: a writer from before vault-scoped derivation (no `derivation_scope`) is refused with
`token_vault_derivation_unsupported` before any row reaches the vault, through the adapter and
through `run_pipeline` (before a source is profiled). T7: the refusals carry the registered text
and nothing from the job.
"""

from __future__ import annotations

import unicodedata
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine import reasons
from decoy_engine.determinism import derive
from decoy_engine.execution import ExecutionError
from tests.unit.execution.test_tokenize import FakeVault, _run

SCOPE_A = bytes(range(32))
SCOPE_B = bytes(range(1, 33))
MASK_KEY = bytes(range(100, 132))


class _ScopelessVault(FakeVault):
    """A writer built before vault-scoped derivation: it has no `derivation_scope`."""

    def __init__(self) -> None:
        super().__init__()
        del self.derivation_scope


def _tokens(df: pd.DataFrame, vault: Any, namespace: str = "pan_ns") -> list[str]:
    return list(_run(df, namespace=namespace, vault=vault)["pan"])


def test_two_vaults_same_key_namespace_plaintext_mint_different_tokens() -> None:
    df = pd.DataFrame({"pan": ["4111111111111111", "alice@example.com"]})
    a = _tokens(df, FakeVault(scope=SCOPE_A))
    b = _tokens(df, FakeVault(scope=SCOPE_B))
    assert all(t.startswith("tok_") for t in a + b)
    assert not set(a) & set(b)


def test_one_vault_mints_stable_tokens_across_runs() -> None:
    df = pd.DataFrame({"pan": ["4111111111111111", "alice@example.com"]})
    assert _tokens(df, FakeVault(scope=SCOPE_A)) == _tokens(df, FakeVault(scope=SCOPE_A))


def _hash_column(df: pd.DataFrame, namespace: str = "pan_ns") -> list[str]:
    from datetime import datetime

    from decoy_engine.execution import PandasExecutionAdapter
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import Profile, TableProfile
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._graph import RelationshipGraph
    from decoy_engine.relationships._namespace import build_namespace_registry
    from tests.unit.execution.test_tokenize import _VERSION, _col

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
            {"name": "t", "columns": [{"name": "pan", "strategy": "hash", "namespace": namespace}]}
        ],
    }
    plan = compile_plan(config, profile, decoy_engine_version=_VERSION)
    res = PandasExecutionAdapter().run(
        plan,
        {"t": pa.Table.from_pandas(df, preserve_index=False)},
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=build_namespace_registry(config, profile),
    )
    return list(res.outputs["t"].to_pydict()["pan"])


def test_hash_and_tokenize_differ_for_the_same_input_and_parameters() -> None:
    # Same run seed (so the same mask key), namespace and plaintext through both strategies:
    # a `hash` output truncated to 32 hex chars must not be the token payload.
    df = pd.DataFrame({"pan": ["4111111111111111", "alice@example.com", "café"]})
    hashed = _hash_column(df)
    tokens = _tokens(df, FakeVault(scope=SCOPE_A))
    for h, t in zip(hashed, tokens, strict=True):
        assert h[:32] != t[len("tok_") :]


def token_for(mask_key: bytes, namespace: str, scope: bytes, plaintext: str) -> str:
    """One proposed token, built the way the strategy builds it (context once, then the token)."""
    from decoy_engine.execution._strategies._tokenize import token_from, tokenize_context

    ctx = SimpleNamespace(mask_key=mask_key)
    return token_from(tokenize_context(ctx, namespace, scope), namespace, plaintext)


@settings(max_examples=200, deadline=None)
@given(st.text(min_size=0, max_size=40), st.binary(min_size=16, max_size=64))
def test_token_payload_never_equals_hash_truncate_32(text: str, scope: bytes) -> None:
    pt = unicodedata.normalize("NFC", text)
    hash_prefix = derive(MASK_KEY, "pan_ns", pt.encode("utf-8")).hex()[:32]
    token = token_for(MASK_KEY, "pan_ns", scope, pt)
    assert token.startswith("tok_") and len(token) == 36
    assert token[4:] != hash_prefix


def test_tokenize_subkey_vector() -> None:
    """Pinned: the label, the scope framing and the order of the two derivations."""
    assert token_for(MASK_KEY, "pan_ns", SCOPE_A, "4111111111111111") == TOKEN_VECTOR


# Recomputed outside the engine (stdlib HMAC/HKDF from the formula in `_tokenize`'s docstring,
# SEED_PROTOCOL_VERSION 7, salt b"decoy-engine/determinism/v1") and equal to the engine's output.
TOKEN_VECTOR = "tok_3bf686adbfcebf14cb06a52c0b3a9e38"


# -- T2 and T7: a writer from before vault-scoped derivation is refused -----------------------


def _registered(code: str) -> str:
    return reasons.get("runtime", code).summary


def test_scopeless_writer_is_refused_before_any_row_reaches_the_vault() -> None:
    vault = _ScopelessVault()
    with pytest.raises(ExecutionError) as exc:
        _run(pd.DataFrame({"pan": ["4111111111111111"]}), vault=vault)
    assert exc.value.code == "token_vault_derivation_unsupported"
    assert vault.lookups == [] and vault.puts == []


@pytest.mark.parametrize(
    "scope",
    [b"", b"short", b"x" * 15, b"x" * 65, "a" * 32, bytearray(32), None],
    ids=["empty", "five", "fifteen", "sixty-five", "str", "bytearray", "none"],
)
def test_a_malformed_scope_is_refused_before_any_row_reaches_the_vault(scope: object) -> None:
    vault = FakeVault()
    vault.derivation_scope = scope  # type: ignore[assignment]
    with pytest.raises(ExecutionError) as exc:
        _run(pd.DataFrame({"pan": ["4111111111111111"]}), vault=vault)
    expected = (
        "token_vault_derivation_unsupported" if scope is None else "tokenize_vault_scope_required"
    )
    assert exc.value.code == expected
    assert vault.lookups == [] and vault.puts == []


@pytest.mark.parametrize(
    ("vault", "code"),
    [
        (_ScopelessVault, "token_vault_derivation_unsupported"),
        (lambda: FakeVault(scope=b"tiny"), "tokenize_vault_scope_required"),
    ],
    ids=["scopeless", "malformed"],
)
def test_refusal_text_is_the_registered_message_and_names_nothing(vault: Any, code: str) -> None:
    secret_value = "4111111111111111"
    with pytest.raises(ExecutionError) as exc:
        _run(pd.DataFrame({"pan": [secret_value]}), namespace="pan_ns_canary", vault=vault())
    text = str(exc.value)
    assert exc.value.code == code
    assert text == f"[{code}] strategy='tokenize': {_registered(code)}"
    for leaked in (secret_value, "pan_ns_canary", "tiny", "pan"):
        assert leaked not in text.split(": ", 1)[1]
    assert exc.value.__cause__ is None and exc.value.__context__ is None


class _LoudSource:
    """A source that fails the test when the engine reads it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the source was touched ({name}) before the vault was refused")


def test_run_pipeline_refuses_a_scopeless_writer_before_reading_a_source(tmp_path: Any) -> None:
    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline
    from decoy_engine.keyprovider import SecretKeyProvider

    raw = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(tmp_path / "i.pq")}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": str(tmp_path / "o.pq")}},
        "tables": [
            {"name": "t", "columns": [{"name": "pan", "strategy": "tokenize", "namespace": "ns"}]}
        ],
    }
    cfg = PipelineConfig.model_validate(raw).model_dump()
    vault = _ScopelessVault()
    with pytest.raises(ExecutionError) as exc:
        run_pipeline(
            cfg,
            {"t": _LoudSource()},  # type: ignore[dict-item]
            engine_version="b6",
            key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
            token_vault_writer=vault,
        )
    assert exc.value.code == "token_vault_derivation_unsupported"
    assert vault.lookups == [] and vault.puts == []
    assert list(tmp_path.iterdir()) == []

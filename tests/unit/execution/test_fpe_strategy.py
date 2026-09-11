"""engine-v2 S9 slice 2g: FPE strategy (NIST SP 800-38G FF1 + chunked parallelism).

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
        # 6+ digits: clears the FF1 minimum admissible domain for radix 10
        # (radix**length >= 1,000,000 needs length >= 6).
        df = pd.DataFrame({"acct": ["123456", "678905", None]})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "acct", _fpe_col(), _ctx())
        vals = out["acct"].tolist()
        # Null stays null; the exact marker (None vs nan) is a pandas
        # version detail, not part of the contract (audit BL-2 cleared
        # the semantic concern via the property suite).
        assert pd.isna(vals[2])
        for v in vals[:2]:
            assert len(v) == 6 and v.isdigit()

    def test_deterministic_same_value_same_output(self) -> None:
        df = pd.DataFrame({"acct": ["123456", "999999", "123456"]})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "acct", _fpe_col(), _ctx())
        vals = out["acct"].tolist()
        assert vals[0] == vals[2]  # same source -> same ciphertext

    def test_chunked_serial_parity(self) -> None:
        # The non-negotiable gate: chunk_count=1 and chunk_count=4 byte-identical.
        # 6-digit, zero-padded: clears the FF1 domain floor for radix 10.
        rows = [f"{i:06d}" for i in range(50)]
        serial, _ = FpeStrategyHandler(chunk_count=1).run(
            pd.DataFrame({"acct": list(rows)}), "acct", _fpe_col(), _ctx()
        )
        parallel, _ = FpeStrategyHandler(chunk_count=4).run(
            pd.DataFrame({"acct": list(rows)}), "acct", _fpe_col(), _ctx()
        )
        assert serial["acct"].tolist() == parallel["acct"].tolist()

    def test_requires_namespace(self) -> None:
        df = pd.DataFrame({"acct": ["123456"]})
        with pytest.raises(ExecutionError) as exc:
            FpeStrategyHandler().run(df, "acct", _fpe_col(namespace=None), _ctx())
        assert exc.value.code == "fpe_requires_namespace"


class TestWs1SingleKeyReversibility:
    """WS1 detokenization (2026-06-12): the key derives from (job_seed,
    namespace) ONLY, one key per context with a per-column tweak. Since
    Task 5.2 this is the NIST SP 800-38G FF1 key model exactly (the pre-FF1
    home-rolled Feistel used the identical key model already). The pre-WS1
    per-value keying `derive(seed, ns, canonicalize(value))` baked the
    PLAINTEXT into the key, making ciphertext-only decryption
    cryptographically impossible and the detokenization capability
    unbuildable. Covered by the SEED_PROTOCOL_VERSION 4 -> 5 bump."""

    def test_ciphertext_decrypts_without_plaintext(self) -> None:
        from decoy_engine.determinism import derive
        from decoy_engine.transforms.fpe import (
            _CHARSETS,
            FF1_KEY_LABEL,
            FF1_TWEAK_SCOPE_COLUMN,
            build_ff1_tweak,
            fpe_decrypt_value,
        )

        source = ["123456", "678905", "000015"]
        df = pd.DataFrame({"acct": list(source)})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "acct", _fpe_col(), _ctx())
        # An unmask caller holds ONLY (job_seed, namespace, column, charset).
        key = derive(_SEED, "fpe_ns", FF1_KEY_LABEL)
        tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, "acct")
        recovered = [
            fpe_decrypt_value(v, key, _CHARSETS["digits"], tweak) for v in out["acct"].tolist()
        ]
        assert recovered == source

    def test_joinability_preserved(self) -> None:
        # Single-key Feistel is still deterministic: same value, same
        # ciphertext within a namespace (the joinability contract).
        df_a = pd.DataFrame({"acct": ["123456", "678905"]})
        df_b = pd.DataFrame({"acct": ["123456", "111115"]})
        out_a, _ = FpeStrategyHandler(chunk_count=1).run(df_a, "acct", _fpe_col(), _ctx())
        out_b, _ = FpeStrategyHandler(chunk_count=1).run(df_b, "acct", _fpe_col(), _ctx())
        assert out_a["acct"].tolist()[0] == out_b["acct"].tolist()[0]

    def test_namespace_separates_keys(self) -> None:
        """A single value colliding across two namespace-derived keys is
        legal for a permutation, so the key-separation guard is aggregate
        across several independent values rather than a universal claim."""
        df = pd.DataFrame({"acct": ["123456", "678905", "111115"]})
        out_a, _ = FpeStrategyHandler(chunk_count=1).run(
            df.copy(), "acct", _fpe_col(namespace="ns_a"), _ctx()
        )
        out_b, _ = FpeStrategyHandler(chunk_count=1).run(
            df.copy(), "acct", _fpe_col(namespace="ns_b"), _ctx()
        )
        assert any(
            a != b for a, b in zip(out_a["acct"].tolist(), out_b["acct"].tolist(), strict=True)
        )


class TestFf1SingleCharAlwaysSubFloor:
    """Task 5.2: the pre-FF1 Feistel had a bespoke single-character rotation
    (QA-10 F2, 2026-06-01) that bijected one charset symbol onto another.
    FF1 has no equivalent: the algorithm itself requires a numeral string of
    length >= 2 (u = n // 2 must be at least 1), and even if it didn't, a
    single in-charset character is always below the FF1 minimum admissible
    domain (radix <= 64 everywhere in this engine's charsets, so radix**1 <=
    64 << FF1_MIN_DOMAIN). So a single-character fpe value now fails closed
    with `FpeUnencryptableError` (code `fpe.unencryptable_domain`) instead of
    silently permuting; there is no bijection to test here anymore."""

    def test_single_digit_value_fails_closed_on_domain_floor(self) -> None:
        from decoy_engine.errors import FpeUnencryptableError

        df = pd.DataFrame({"acct": ["5"]})
        with pytest.raises(ExecutionError) as exc:
            FpeStrategyHandler(chunk_count=1).run(df, "acct", _fpe_col(), _ctx())
        assert exc.value.code == "fpe_unencryptable_domain"
        assert isinstance(exc.value.__cause__, FpeUnencryptableError)

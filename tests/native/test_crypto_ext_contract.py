"""Contract tests for the keyed-crypto + FPE native extension (Task 0.4).

These pin the pure-Python reference kernels to the SHIPPED engine crypto so the
Phase 2 compiled kernel can be graded against a byte-exact oracle:

- hash parity: reference ``derive_batch`` == ``kernel/_scalar.hash_array``.
- FPE parity: reference ``encrypt_batch`` == the shipped ``FpeStrategyHandler``.
- FPE round trip: ``decrypt_batch(encrypt_batch(x)) == x``.
- missing-key fail-closed: a ``None`` mask_key raises before any output.
- null / mixed-object / bytes / numeric canonical encoding.
- KAT vectors reproduced by BOTH the reference AND the shipped primitives.
- the compiled-extension ABI fails before output when the extension is absent.
"""

from __future__ import annotations

import sys

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.determinism import derive
from decoy_engine.errors import MaskKeyRequiredError
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._fpe import FPE_KEY_LABEL, FpeStrategyHandler
from decoy_engine.execution.native._crypto_ext import (
    CRYPTO_EXT_ABI,
    FPE_KAT,
    HASH_KAT,
    CryptoExtensionUnavailableError,
    FpeConfig,
    load_compiled_crypto_kernel,
    reference_fpe,
    reference_keyed_derivation,
)
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.kernel._canonicalize import canonicalize_derive_source
from decoy_engine.kernel._scalar import hash_array
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.transforms.fpe import fpe_encrypt_value

MK32 = bytes(range(32))
NS = "people.ssn"

# Shared fixtures to drive the SHIPPED FpeStrategyHandler for parity checks.
_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = (0xC0FFEE).to_bytes(8, "big")


def _ctx() -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        job_seed=_JOB_SEED,
    )


def _fpe_col(config: dict[str, object], *, namespace: str = "fpe_ns") -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(config.items()),
        coherent_with=(),
    )


# ---------------------------------------------------------------------------
# Hash parity + encoding.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        pa.array(["alice", "bob", "carol"], type=pa.string()),
        pa.array(["alice", None, "carol"], type=pa.string()),
        pa.array([1, 2, 3, 4], type=pa.int64()),
        pa.array([], type=pa.string()),
        ["alice", 12345, None, "carol"],  # mixed-object list form
    ],
)
@pytest.mark.parametrize("truncate", [None, 8, 16])
def test_reference_hash_matches_scalar(values: object, truncate: int | None) -> None:
    got = reference_keyed_derivation().derive_batch(
        values, mask_key=MK32, namespace=NS, truncate=truncate
    )
    want = hash_array(values, seed=MK32, namespace=NS, truncate=truncate)
    assert got.to_pylist() == want.to_pylist()


@pytest.mark.parametrize(
    "values",
    [
        pa.array([], type=pa.string()),
        pa.array([None, None, None], type=pa.string()),
    ],
)
def test_reference_hash_batch_type_is_string_even_when_empty_or_all_null(
    values: pa.Array,
) -> None:
    # to_pylist() parity alone cannot catch this: pyarrow infers a null-typed array from
    # an empty or all-null list just as readily as from an explicit type=pa.string(), and
    # both compare equal as plain Python lists. Every KAT vector has a non-null value, so
    # the parity tests above never exercise this shape. Pin the Arrow type directly.
    got = reference_keyed_derivation().derive_batch(
        values, mask_key=MK32, namespace=NS, truncate=None
    )
    assert got.type == pa.string()


def test_hash_null_and_numeric_and_bytes_encoding() -> None:
    # None stays None; int and bytes each go through the shipped canonicalizer.
    values = ["text", 12345, b"\x00\x01raw", None]
    got = reference_keyed_derivation().derive_batch(
        values, mask_key=MK32, namespace=NS, truncate=None
    )
    expected = [
        derive(MK32, NS, canonicalize_derive_source("text")).hex(),
        derive(MK32, NS, canonicalize_derive_source(12345)).hex(),
        derive(MK32, NS, canonicalize_derive_source(b"\x00\x01raw")).hex(),
        None,
    ]
    assert got.to_pylist() == expected


def test_hash_mixed_object_list_only_form() -> None:
    # A mixed str+int column has no single Arrow type; the reference kernel
    # accepts the raw list form (the compiled kernel rejects it, see below).
    values = ["abc", 7, "def"]
    got = reference_keyed_derivation().derive_batch(
        values, mask_key=MK32, namespace=NS, truncate=None
    )
    assert got.to_pylist() == hash_array(values, seed=MK32, namespace=NS).to_pylist()


def test_hash_kat_reproduced_by_reference_and_shipped() -> None:
    assert HASH_KAT, "KAT vectors must be present"
    for vec in HASH_KAT:
        ref = reference_keyed_derivation().derive_batch(
            [vec.value], mask_key=vec.mask_key, namespace=vec.namespace, truncate=vec.truncate
        )
        assert ref.to_pylist() == [vec.expected]
        shipped = hash_array(
            [vec.value], seed=vec.mask_key, namespace=vec.namespace, truncate=vec.truncate
        )
        assert shipped.to_pylist() == [vec.expected]


def test_hash_missing_key_fails_closed() -> None:
    with pytest.raises(MaskKeyRequiredError):
        reference_keyed_derivation().derive_batch(["x"], mask_key=None, namespace=NS, truncate=None)


# ---------------------------------------------------------------------------
# FPE parity vs the shipped strategy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,column,rows",
    [
        ({"charset": "digits"}, "acct", ["12345", "67890", None, "12345"]),
        ({"charset": "digits"}, "ssn", ["123-45-6789", "000-11-2222"]),
        (
            {"charset": "ALPHANUM"},
            "code",
            ["AB12CD", "ZZ99ZZ", None],
        ),
        ({"charset": "digits", "validate_luhn": True}, "pan", ["4111111111111111"]),
    ],
)
def test_reference_fpe_matches_shipped_strategy(
    config: dict[str, object], column: str, rows: list[str | None]
) -> None:
    # SHIPPED strategy output (mask_key defaults to the 8-byte job_seed).
    df = pd.DataFrame({column: rows})
    out, _warnings = FpeStrategyHandler(chunk_count=1).run(df, column, _fpe_col(config), _ctx())
    shipped_vals = out[column].tolist()

    result = reference_fpe().encrypt_batch(
        pa.array(rows, type=pa.string()),
        mask_key=_JOB_SEED,
        namespace="fpe_ns",
        tweak_column=column,
        config=FpeConfig.from_mapping(config),
    )
    ref_vals = result.to_pylist()
    assert result.errors == ()
    # Nulls compare as None on both sides; non-null values byte-identical.
    for ref, shipped in zip(ref_vals, shipped_vals, strict=True):
        if ref is None:
            assert pd.isna(shipped)
        else:
            assert ref == shipped


@pytest.mark.parametrize(
    "config,column,rows",
    [
        ({"charset": "digits"}, "acct", ["12345", "67890", "00000"]),
        ({"charset": "digits", "preserve_separators": True}, "ssn", ["123-45-6789"]),
        ({"charset": "ALPHANUM"}, "code", ["AB12CD", "ZZ99ZZ"]),
        ({"charset": "digits", "validate_luhn": True}, "pan", ["4111111111111111"]),
    ],
)
def test_reference_fpe_roundtrip(config: dict[str, object], column: str, rows: list[str]) -> None:
    kern = reference_fpe()
    cfg = FpeConfig.from_mapping(config)
    enc = kern.encrypt_batch(
        pa.array(rows, type=pa.string()),
        mask_key=MK32,
        namespace=NS,
        tweak_column=column,
        config=cfg,
    )
    dec = kern.decrypt_batch(
        enc.values, mask_key=MK32, namespace=NS, tweak_column=column, config=cfg
    )
    assert dec.to_pylist() == rows


def test_fpe_kat_reproduced_by_reference_and_shipped() -> None:
    assert FPE_KAT, "FPE KAT vectors must be present"
    for vec in FPE_KAT:
        cfg = vec.config
        resolved_charset, preserve_sep, validate_luhn, checksum = cfg._resolve()
        key = derive(vec.mask_key, vec.namespace, FPE_KEY_LABEL)
        tweak = (cfg.join_group or vec.tweak_column).encode("utf-8", errors="replace")
        shipped = fpe_encrypt_value(
            vec.plaintext, key, resolved_charset, tweak, preserve_sep, validate_luhn, checksum
        )
        assert shipped == vec.ciphertext
        ref = reference_fpe().encrypt_batch(
            [vec.plaintext],
            mask_key=vec.mask_key,
            namespace=vec.namespace,
            tweak_column=vec.tweak_column,
            config=cfg,
        )
        assert ref.to_pylist() == [vec.ciphertext]


def test_fpe_missing_key_fails_closed() -> None:
    with pytest.raises(MaskKeyRequiredError):
        reference_fpe().encrypt_batch(
            pa.array(["123"], type=pa.string()),
            mask_key=None,
            namespace=NS,
            tweak_column="c",
            config=FpeConfig(),
        )
    with pytest.raises(MaskKeyRequiredError):
        reference_fpe().decrypt_batch(
            pa.array(["123"], type=pa.string()),
            mask_key=None,
            namespace=NS,
            tweak_column="c",
            config=FpeConfig(),
        )


def test_fpe_per_row_error_is_structured_and_redacted() -> None:
    # preserve_separators=False + out-of-charset char fails closed per row; the
    # structured error must NOT embed the offending cell value (PII discipline).
    secret = "12-34"  # the dash is out of the digits charset
    result = reference_fpe().encrypt_batch(
        pa.array(["1234", secret], type=pa.string()),
        mask_key=MK32,
        namespace=NS,
        tweak_column="acct",
        config=FpeConfig(charset="digits", preserve_separators=False),
    )
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.row_index == 1
    assert err.code == "fpe_unencryptable_value"
    assert secret not in err.message
    assert result.values.to_pylist()[1] is None


def test_fpe_join_group_shares_tweak_and_warns() -> None:
    # Two different columns under one join group encrypt identical values
    # identically, and the batch surfaces the join-group warning.
    cfg = FpeConfig(charset="digits", join_group="link")
    a = reference_fpe().encrypt_batch(
        pa.array(["12345"], type=pa.string()),
        mask_key=MK32,
        namespace=NS,
        tweak_column="col_a",
        config=cfg,
    )
    b = reference_fpe().encrypt_batch(
        pa.array(["12345"], type=pa.string()),
        mask_key=MK32,
        namespace=NS,
        tweak_column="col_b",
        config=cfg,
    )
    assert a.to_pylist() == b.to_pylist()
    assert any(w.code == "fpe_join_group_active" for w in a.warnings)


# ---------------------------------------------------------------------------
# Compiled-extension ABI: fail before output.
# ---------------------------------------------------------------------------


def test_compiled_kernel_absent_fails_before_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the "companion absent" path deterministically, regardless of whether
    # decoy-engine-native happens to be installed in this environment: setting
    # sys.modules[name] = None makes the import machinery raise ImportError for
    # that name (a documented Python import-system behavior), which is exactly the
    # failure the loader must map to CryptoExtensionUnavailableError. The real
    # present/absent environments are covered by tests/native/test_crypto_ext_loader.py.
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_abi_doc_block_present() -> None:
    haystack = CRYPTO_EXT_ABI.lower()
    for token in (
        "arrow c data interface",
        "mixed_object_not_native",
        "fail-before-output",
        "thread",
        "ownership",
    ):
        assert token in haystack

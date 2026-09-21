"""D0 gate + acceptance tests for the World B sampler's native index derivation.

The World B pool sampler used to derive one pool index per row with a Python
loop over `derive_index(seed, namespace, _canonicalize_source(value), pool_size)`
(`_sampler.py`). This module proves the batched `derive_index_batch` kernel is a
byte-identical drop-in for that loop across the sampler's whole value domain,
which is the load-bearing invariant of the swap.

`test_d0_*` are the D0 parity gate: `derive_index_batch` must equal the per-row
`derive_index(...)` element for element, over the admitted value corpus (str
incl. NFC/NFD, bools, signed/unsigned + numpy ints, tz-aware timestamps, None),
for both constructions the sampler uses: the reference kernel over raw Python
values, and the compiled kernel over `pa.Array.from_pandas`. It runs against the
reference always, and against the compiled kernel whenever the companion is
installed.

`test_legacy_oracle_*` are the acceptance differential tests: the *new* sampler
output vs a frozen copy of the *pre-change* per-row implementation, asserting
equal output for scalar and multi-column bundle sampling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.determinism import DeterminismError, derive_index
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import (
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.generation.composite._bundle_pool import BundlePool
from decoy_engine.generation.pool import CardinalityMode, GenerationError, PoolSampler, ValuePool
from decoy_engine.generation.pool._canonicalize import _canonicalize_source

# 32-byte mask key: the production deterministic-selection seam re-keys onto the
# keyed IKM (`_strategies/_faker.py`). The 8-byte job-seed form is exercised too
# (`derive_index` and both kernels accept 8- and 32-byte seeds).
_MASK_KEY = bytes(range(32))
_JOB_SEED = b"\x00\x00\x00\x00\x00\x00\x00\x2a"  # 42
_NS = "pool.city"


def _compiled_kernel_or_none() -> Any:
    try:
        return load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return None


_COMPILED = _compiled_kernel_or_none()
_KERNELS = [pytest.param(reference_index_derivation(), id="reference")]
if _COMPILED is not None:
    _KERNELS.append(pytest.param(_COMPILED, id="compiled"))


# ---------------------------------------------------------------------------
# Representative admitted-value corpus (the D0 corpus). Every entry is a class
# the sampler's per-row path canonicalizes into an index; the batch kernels must
# canonicalize each to the same bytes.
# ---------------------------------------------------------------------------

_ADMITTED_CORPUS: list[tuple[str, list[Any]]] = [
    ("ascii_strings", ["alice", "bob", "carol", "alice"]),
    # NFC vs NFD spelling of the same grapheme: canonicalization NFC-normalizes,
    # so both must map to the same index.
    ("unicode_nfc_nfd", ["café", "café", "naïve", "naïve"]),
    ("bools", [True, False, True, False]),
    ("python_ints", [1, 2, -5, 0, 2, 1]),
    ("numpy_int64", [np.int64(5), np.int64(-7), np.int64(9)]),
    ("numpy_uint64", [np.uint64(5), np.uint64(7), np.uint64(9)]),
    (
        "tz_aware_datetime",
        [
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2021, 6, 1, 12, 30, tzinfo=timezone.utc),
        ],
    ),
    (
        "pandas_tz_timestamps",
        [
            pd.Timestamp("2020-01-01", tz="UTC"),
            # Nanosecond precision: Arrow keeps timestamp[ns], canonicalization uses
            # the full isoformat, so the batch path must not truncate to microseconds.
            pd.Timestamp("2021-06-01T00:00:00.000000123", tz="UTC"),
        ],
    ),
    ("dup_values", ["x", "x", "x", "x"]),
]


def _per_row_indices(values: list[Any], *, seed: bytes, pool_size: int) -> list[int | None]:
    """The reference oracle: exactly the pre-change per-row derivation."""
    out: list[int | None] = []
    for value in values:
        out.append(derive_index(seed, _NS, _canonicalize_source(value), pool_size=pool_size))
    return out


@pytest.mark.parametrize("seed", [_MASK_KEY, _JOB_SEED], ids=["mask_key32", "job_seed8"])
@pytest.mark.parametrize("pool_size", [1, 7, 97, 40009])
@pytest.mark.parametrize("label,values", _ADMITTED_CORPUS, ids=[c[0] for c in _ADMITTED_CORPUS])
def test_d0_batch_matches_per_row(
    seed: bytes, pool_size: int, label: str, values: list[Any]
) -> None:
    """D0 GATE: the two constructions the sampler uses each match the per-row
    derive_index, element for element.

    - the reference kernel fed the raw Python values (the universal fallback);
    - the compiled kernel fed `pa.Array.from_pandas` (the fast path).

    `from_pandas` is deliberate, not `pa.array(list)`: the latter infers
    `timestamp[us]` and truncates sub-microsecond timestamps, which would
    diverge from the per-row path. `from_pandas` preserves the source dtype
    (`timestamp[ns]`), so both kernels canonicalize the same bytes.
    """
    series = pd.Series(values)
    expected = _per_row_indices(series.tolist(), seed=seed, pool_size=pool_size)

    ref_got = reference_index_derivation().derive_index_batch(
        series.tolist(), mask_key=seed, namespace=_NS, pool_size=pool_size
    )
    assert ref_got.type == pa.uint64()
    assert ref_got.to_pylist() == expected

    if _COMPILED is not None:
        comp_got = _COMPILED.derive_index_batch(
            pa.Array.from_pandas(series), mask_key=seed, namespace=_NS, pool_size=pool_size
        )
        assert comp_got.type == pa.uint64()
        assert comp_got.to_pylist() == expected


@pytest.mark.parametrize("kernel", _KERNELS)
def test_d0_null_positions_preserved(kernel: Any) -> None:
    """Nulls in -> nulls out, positionally, on the batch kernels."""
    arr = pa.array(["a", None, "b", None, "a"], type=pa.string())
    got = kernel.derive_index_batch(arr, mask_key=_MASK_KEY, namespace=_NS, pool_size=97)
    valid = got.is_valid().to_pylist()
    assert valid == [True, False, True, False, True]
    # Non-null indices still match the per-row derivation.
    non_null = [v for v in ["a", None, "b", None, "a"] if v is not None]
    expected = _per_row_indices(non_null, seed=_MASK_KEY, pool_size=97)
    assert [i for i in got.to_pylist() if i is not None] == expected


# ---------------------------------------------------------------------------
# Pool builders for the acceptance tests.
# ---------------------------------------------------------------------------


def _value_pool(values: list[Any]) -> ValuePool:
    return ValuePool(
        values=np.array(values, dtype=object),
        provider="test_provider",
        locale="default",
        config_hash="test-hash",
        seed=b"test-see",
        size=len(values),
        build_time_ms=0.0,
        backend_type="faker",
        backend_version="0",
        distinct_count=len(set(values)),
    )


def _bundle_pool(tuples: list[tuple[Any, ...]], cols: tuple[str, ...]) -> BundlePool:
    arr = np.empty(len(tuples), dtype=object)
    for i, t in enumerate(tuples):
        arr[i] = t
    return BundlePool(
        values=arr,
        provider="test_composite",
        locale="default",
        config_hash="test-hash",
        seed=b"test-see",
        size=len(tuples),
        build_time_ms=0.0,
        backend_type="faker",
        backend_version="0",
        distinct_count=len(tuples),
        output_columns=cols,
    )


# ---------------------------------------------------------------------------
# Frozen legacy oracle: verbatim copy of the pre-change per-row implementation.
# The acceptance tests assert the CURRENT sampler equals this, which is the
# byte-identity proof the plan's VERIFY item 2 requires.
# ---------------------------------------------------------------------------


def _legacy_deterministic(
    pool: ValuePool, n: int, source: pd.Series, seed: bytes, namespace: str
) -> pd.Series:
    src_values = source.tolist()
    is_null_arr = source.isna().to_numpy()
    pool_values = pool.values
    pool_size = pool.size
    output: list[Any] = [pd.NA] * n
    for i, value in enumerate(src_values):
        if is_null_arr[i]:
            continue
        canonical = _canonicalize_source(value)
        idx = derive_index(seed=seed, namespace=namespace, source=canonical, pool_size=pool_size)
        output[i] = pool_values[idx]
    return pd.Series(output)


def _legacy_sample_bundle(
    pool: BundlePool, n: int, source: pd.Series, seed: bytes, namespace: str
) -> dict[str, pd.Series]:
    cols = pool.output_columns
    per_col: dict[str, list[Any]] = {c: [] for c in cols}
    is_null = source.isna()
    for i in range(n):
        if is_null.iloc[i]:
            for c in cols:
                per_col[c].append(pd.NA)
            continue
        canonical = _canonicalize_source(source.iloc[i])
        idx = derive_index(seed=seed, namespace=namespace, source=canonical, pool_size=pool.size)
        bundle = pool.values[idx]
        for j, c in enumerate(cols):
            per_col[c].append(bundle[j])
    return {c: pd.Series(per_col[c]) for c in cols}


def _assert_series_identical(got: pd.Series, expected: pd.Series) -> None:
    assert len(got) == len(expected)
    assert list(got.index) == list(expected.index)
    # Positional null representation must match (pd.NA vs value).
    assert got.isna().tolist() == expected.isna().tolist()
    for g, e in zip(got.tolist(), expected.tolist(), strict=True):
        if pd.isna(g) or pd.isna(e):
            assert pd.isna(g) and pd.isna(e)
        else:
            assert g == e


# ---------------------------------------------------------------------------
# Acceptance: scalar sampling, new == legacy, for every admitted value class.
# ---------------------------------------------------------------------------

_SCALAR_SOURCES: list[tuple[str, pd.Series]] = [
    ("strings", pd.Series(["alice", "bob", "carol", "alice", "dave"], dtype=object)),
    ("unicode_nfc_nfd", pd.Series(["café", "café", "x"], dtype=object)),
    ("bools", pd.Series([True, False, True], dtype=object)),
    ("python_ints", pd.Series([10, 20, -5, 0], dtype=object)),
    ("numpy_int_column", pd.Series([1, 2, 3, 4], dtype="int64")),
    ("numpy_uint_column", pd.Series([1, 2, 3], dtype="uint64")),
    ("int8_column", pd.Series([1, 2, 3], dtype="int8")),
    # A sub-microsecond value drives the ns-vs-us construction path end-to-end
    # through PoolSampler (from_pandas preserves ns; pa.array(list) would
    # truncate) -- the one riskiest value class, so it is guarded here, not only
    # in the D0 test that bypasses PoolSampler (dennis MEDIUM).
    (
        "tz_aware_ts",
        pd.Series(
            [
                pd.Timestamp("2020-01-01", tz="UTC"),
                pd.Timestamp("2021-06-01T00:00:00.000000123", tz="UTC"),
            ]
        ),
    ),
    (
        "dates",
        pd.Series(
            [pd.Timestamp("2020-01-01").date(), pd.Timestamp("2021-01-01").date()], dtype=object
        ),
    ),
    ("decimals", pd.Series([Decimal("1.5"), Decimal("2.25"), Decimal("3.0")], dtype=object)),
    ("huge_ints", pd.Series([2**70, -(2**70), 2**80], dtype=object)),
    ("all_null", pd.Series([None, None, None], dtype=object)),
    ("mixed_null", pd.Series(["a", None, "b", None, "c"], dtype=object)),
    ("empty", pd.Series([], dtype=object)),
]

_POOL = _value_pool([f"pv{i}" for i in range(11)])


@pytest.mark.parametrize("seed", [_MASK_KEY, _JOB_SEED], ids=["mask_key32", "job_seed8"])
@pytest.mark.parametrize("label,source", _SCALAR_SOURCES, ids=[s[0] for s in _SCALAR_SOURCES])
def test_legacy_oracle_scalar(label: str, source: pd.Series, seed: bytes) -> None:
    n = len(source)
    expected = _legacy_deterministic(_POOL, n, source, seed, _NS)
    got = PoolSampler().sample(
        _POOL,
        n,
        mode=CardinalityMode.REUSE,
        seed=seed,
        source=source,
        namespace=_NS,
        deterministic=True,
    )
    _assert_series_identical(got, expected)


@pytest.mark.parametrize("pool_size", [1, 2, 5, 11, 97])
def test_legacy_oracle_scalar_varied_pool_size(pool_size: int) -> None:
    pool = _value_pool([f"pv{i}" for i in range(pool_size)])
    source = pd.Series(["alice", "bob", "carol", "alice", None, "eve"], dtype=object)
    n = len(source)
    expected = _legacy_deterministic(pool, n, source, _MASK_KEY, _NS)
    got = PoolSampler().sample(
        pool,
        n,
        mode=CardinalityMode.REUSE,
        seed=_MASK_KEY,
        source=source,
        namespace=_NS,
        deterministic=True,
    )
    _assert_series_identical(got, expected)


# ---------------------------------------------------------------------------
# Acceptance: composite bundle sampling, new == legacy, ONE shared index/row.
# ---------------------------------------------------------------------------

_BUNDLE_POOL = _bundle_pool(
    [(f"first{i}", f"last{i}", f"email{i}") for i in range(9)],
    cols=("first", "last", "email"),
)

_BUNDLE_SOURCES: list[tuple[str, pd.Series]] = [
    ("strings", pd.Series(["a", "b", "c", "a", "d"], dtype=object)),
    ("bools", pd.Series([True, False, True], dtype=object)),
    ("ints", pd.Series([1, 2, 3, 1], dtype="int64")),
    (
        "tz_ts",
        pd.Series(
            [
                pd.Timestamp("2020-01-01", tz="UTC"),
                pd.Timestamp("2021-01-01T00:00:00.000000123", tz="UTC"),
            ]
        ),
    ),
    ("decimals", pd.Series([Decimal("1.5"), Decimal("2.5")], dtype=object)),
    ("all_null", pd.Series([None, None], dtype=object)),
    ("mixed_null", pd.Series(["a", None, "b", None], dtype=object)),
    ("empty", pd.Series([], dtype=object)),
]


@pytest.mark.parametrize("seed", [_MASK_KEY, _JOB_SEED], ids=["mask_key32", "job_seed8"])
@pytest.mark.parametrize("label,source", _BUNDLE_SOURCES, ids=[s[0] for s in _BUNDLE_SOURCES])
def test_legacy_oracle_bundle(label: str, source: pd.Series, seed: bytes) -> None:
    n = len(source)
    expected = _legacy_sample_bundle(_BUNDLE_POOL, n, source, seed, _NS)
    got = PoolSampler().sample_bundle(
        _BUNDLE_POOL,
        n,
        mode=CardinalityMode.REUSE,
        seed=seed,
        source=source,
        namespace=_NS,
        deterministic=True,
    )
    assert set(got) == set(expected)
    for c in expected:
        _assert_series_identical(got[c], expected[c])


def test_legacy_oracle_bundle_shares_one_index_per_row() -> None:
    """Every emitted row's columns come from ONE shared pool tuple."""
    source = pd.Series(["a", "b", "c", "a"], dtype=object)
    n = len(source)
    got = PoolSampler().sample_bundle(
        _BUNDLE_POOL,
        n,
        mode=CardinalityMode.REUSE,
        seed=_MASK_KEY,
        source=source,
        namespace=_NS,
        deterministic=True,
    )
    # For each row, (first, last, email) must be the SAME pool tuple.
    pool_tuples = {t for t in _BUNDLE_POOL.values}
    for i in range(n):
        row = (got["first"].iloc[i], got["last"].iloc[i], got["email"].iloc[i])
        assert row in pool_tuples
    # Identical source values select the identical tuple (row 0 and row 3 are "a").
    assert got["first"].iloc[0] == got["first"].iloc[3]
    assert got["last"].iloc[0] == got["last"].iloc[3]
    assert got["email"].iloc[0] == got["email"].iloc[3]


# ---------------------------------------------------------------------------
# Acceptance: error parity (the exact current GenerationError / DeterminismError
# codes must be preserved).
# ---------------------------------------------------------------------------


def test_error_parity_float_source() -> None:
    source = pd.Series([1.5, 2.5], dtype="float64")
    with pytest.raises(GenerationError) as exc:
        PoolSampler().sample(
            _POOL,
            2,
            mode=CardinalityMode.REUSE,
            seed=_MASK_KEY,
            source=source,
            namespace=_NS,
            deterministic=True,
        )
    assert exc.value.code == "float_canonicalization_unsupported"


def test_error_parity_tz_naive_datetime() -> None:
    source = pd.Series([datetime(2020, 1, 1), datetime(2021, 1, 1)], dtype=object)
    with pytest.raises(GenerationError) as exc:
        PoolSampler().sample(
            _POOL,
            2,
            mode=CardinalityMode.REUSE,
            seed=_MASK_KEY,
            source=source,
            namespace=_NS,
            deterministic=True,
        )
    assert exc.value.code == "timezone_naive_datetime"


def test_error_parity_source_length_mismatch() -> None:
    source = pd.Series(["a", "b"], dtype=object)
    with pytest.raises(GenerationError) as exc:
        PoolSampler().sample(
            _POOL,
            5,
            mode=CardinalityMode.REUSE,
            seed=_MASK_KEY,
            source=source,
            namespace=_NS,
            deterministic=True,
        )
    assert exc.value.code == "source_length_mismatch"


def test_error_parity_bundle_source_length_mismatch() -> None:
    source = pd.Series(["a", "b"], dtype=object)
    with pytest.raises(GenerationError) as exc:
        PoolSampler().sample_bundle(
            _BUNDLE_POOL,
            5,
            mode=CardinalityMode.REUSE,
            seed=_MASK_KEY,
            source=source,
            namespace=_NS,
            deterministic=True,
        )
    assert exc.value.code == "source_length_mismatch"


def test_error_parity_pool_size_zero() -> None:
    """A degenerate empty pool still raises the derive-layer code, unchanged."""
    pool = _value_pool([])
    source = pd.Series(["a"], dtype=object)
    with pytest.raises(DeterminismError) as exc:
        PoolSampler().sample(
            pool,
            1,
            mode=CardinalityMode.REUSE,
            seed=_MASK_KEY,
            source=source,
            namespace=_NS,
            deterministic=True,
        )
    assert exc.value.code == "pool_size_invalid"


# ---------------------------------------------------------------------------
# Acceptance: compiled == reference == legacy (VERIFY item 3). Skips cleanly
# when the compiled companion is absent (CI covers the compiled substrate).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_COMPILED is None, reason="compiled index companion not installed")
@pytest.mark.parametrize("label,source", _SCALAR_SOURCES, ids=[s[0] for s in _SCALAR_SOURCES])
def test_compiled_equals_reference_equals_legacy_scalar(label: str, source: pd.Series) -> None:
    import decoy_engine.generation.pool._sampler as sampler_mod

    n = len(source)
    legacy = _legacy_deterministic(_POOL, n, source, _MASK_KEY, _NS)

    # Force the reference path (companion ABSENT).
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sampler_mod, "_compiled_index_kernel", lambda: None)
        got_ref = PoolSampler().sample(
            _POOL,
            n,
            mode=CardinalityMode.REUSE,
            seed=_MASK_KEY,
            source=source,
            namespace=_NS,
            deterministic=True,
        )
    # Force the compiled path (companion PRESENT).
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sampler_mod, "_compiled_index_kernel", lambda: _COMPILED)
        got_comp = PoolSampler().sample(
            _POOL,
            n,
            mode=CardinalityMode.REUSE,
            seed=_MASK_KEY,
            source=source,
            namespace=_NS,
            deterministic=True,
        )
    _assert_series_identical(got_ref, legacy)
    _assert_series_identical(got_comp, legacy)


# ---------------------------------------------------------------------------
# Acceptance: validation-failure injection. A malformed kernel result must fail
# HERE with a coded GenerationError, never gather from the pool with a bad index.
# ---------------------------------------------------------------------------


class _StubKernel:
    """A kernel returning a caller-supplied result, to inject malformed shapes."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def derive_index_batch(self, values: Any, **_: Any) -> Any:
        return self._result


@pytest.mark.parametrize(
    "result,expected_code",
    [
        (pa.array([0, 1, 2], type=pa.int32()), "index_batch_type_mismatch"),
        ([0, 1, 2], "index_batch_type_mismatch"),
        (pa.array([0, 1], type=pa.uint64()), "index_batch_length_mismatch"),
        (pa.array([0, None, 2], type=pa.uint64()), "index_batch_null_mask_mismatch"),
        (pa.array([0, 999, 2], type=pa.uint64()), "index_batch_out_of_bounds"),
    ],
    ids=["wrong_dtype", "not_arrow", "wrong_length", "null_injected", "out_of_bounds"],
)
def test_validation_injection_scalar(result: Any, expected_code: str) -> None:
    import decoy_engine.generation.pool._sampler as sampler_mod

    source = pd.Series(["a", "b", "c"], dtype=object)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sampler_mod, "_compiled_index_kernel", lambda: _StubKernel(result))
        with pytest.raises(GenerationError) as exc:
            PoolSampler().sample(
                _POOL,
                3,
                mode=CardinalityMode.REUSE,
                seed=_MASK_KEY,
                source=source,
                namespace=_NS,
                deterministic=True,
            )
    assert exc.value.code == expected_code


def test_validation_injection_bundle_out_of_bounds() -> None:
    import decoy_engine.generation.pool._sampler as sampler_mod

    source = pd.Series(["a", "b"], dtype=object)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            sampler_mod,
            "_compiled_index_kernel",
            lambda: _StubKernel(pa.array([0, 999], type=pa.uint64())),
        )
        with pytest.raises(GenerationError) as exc:
            PoolSampler().sample_bundle(
                _BUNDLE_POOL,
                2,
                mode=CardinalityMode.REUSE,
                seed=_MASK_KEY,
                source=source,
                namespace=_NS,
                deterministic=True,
            )
    assert exc.value.code == "index_batch_out_of_bounds"


# ---------------------------------------------------------------------------
# Acceptance: non-deterministic mode is untouched (D4).
# ---------------------------------------------------------------------------


def test_non_deterministic_mode_unchanged() -> None:
    """Non-deterministic REUSE does not touch derive_index and is reproducible."""
    pool = _value_pool([f"pv{i}" for i in range(20)])
    a = PoolSampler().sample(
        pool, 50, mode=CardinalityMode.REUSE, seed=_JOB_SEED, deterministic=False
    )
    b = PoolSampler().sample(
        pool, 50, mode=CardinalityMode.REUSE, seed=_JOB_SEED, deterministic=False
    )
    assert list(a) == list(b)

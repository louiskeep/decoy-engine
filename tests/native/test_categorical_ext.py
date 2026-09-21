"""Phase 5 Track B: native deterministic categorical kernel unit tests.

Byte-identity (value AND Arrow type) vs the pandas oracle handler is the merge
gate; these cover it at the kernel level (the coordinator/unified-slice
ExecutionResult boundary is covered in `tests/physical/test_shadow_
categorical.py`). The KAT re-derives its own `expected_output` from the oracle
so a drift in either the kernel or the oracle is caught, never silently
trusted; the `searchsorted == bisect_right` differential pins the one place the
native weighted path diverges in FORM (vectorized) from the oracle's per-row
`bisect`.
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution._strategies._categorical import (
    _WEIGHTED_CDF_RES,
    CategoricalStrategyHandler,
    _build_cdf,
)
from decoy_engine.execution.native._categorical_ext import native_categorical
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._index_ext import (
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.generation.pool import GenerationError
from decoy_engine.plan._types import ColumnSeed

_KAT_PATH = Path(__file__).parents[2] / "decoy-engine-native" / "vectors" / "categorical_kat.json"


def _index_kernels() -> list[tuple[str, object]]:
    """The reference kernel always; the compiled one too when the companion is
    present. Native categorical must be identical on both (the compiled kernel
    is a drop-in for the reference)."""
    kernels: list[tuple[str, object]] = [("reference", reference_index_derivation())]
    if native_companion_status().ok:
        kernels.append(("compiled", load_compiled_index_kernel()))
    return kernels


def _native(values, categories, weights, *, mask_key, namespace, kernel):
    cdf = tuple(_build_cdf([float(w) for w in weights])) if weights is not None else None
    return native_categorical(
        pa.array(values, type=pa.string()),
        categories=tuple(categories),
        cdf=cdf,
        mask_key=mask_key,
        namespace=namespace,
        index_kernel=kernel,
        native_threads=1,
    )


class _Ctx:
    # 8-byte seed (S3 spec SEED_LENGTH); DE-02 no-secret path uses job_seed.
    job_seed = (0x0123456789).to_bytes(8, "big")
    mask_key = job_seed


def _oracle(values, categories, weights, *, namespace="ns") -> list[object]:
    pc = {"categories": list(categories)}
    if weights is not None:
        pc["weights"] = list(weights)
    seed = ColumnSeed(
        namespace=namespace,
        strategy="categorical",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(sorted(pc.items())),
    )
    df = pd.DataFrame({"c": values})
    out, _ = CategoricalStrategyHandler().run(df, "c", seed, _Ctx())
    return out["c"].tolist()


# ── KAT ────────────────────────────────────────────────────────────


def _oracle_keyed(case: dict, mask_key: bytes) -> list[object]:
    """The pandas oracle handler for one KAT case, keyed with the KAT's own
    32-byte mask key (the KAT re-derivation must use the same key native did)."""
    pc = {"categories": list(case["categories"])}
    if case["weights"] is not None:
        pc["weights"] = list(case["weights"])
    seed = ColumnSeed(
        namespace=case["namespace"],
        strategy="categorical",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(sorted(pc.items())),
    )

    ctx = SimpleNamespace(job_seed=mask_key, mask_key=mask_key)
    df = pd.DataFrame({"c": case["source_values"]})
    out, _ = CategoricalStrategyHandler().run(df, "c", seed, ctx)
    return out["c"].tolist()


def test_kat_vectors_match_native_and_oracle() -> None:
    doc = json.loads(_KAT_PATH.read_text())
    mask_key = bytes.fromhex(doc["mask_key_hex"])
    assert doc["cases"], "KAT must carry at least one case"
    for kname, kernel in _index_kernels():
        for case in doc["cases"]:
            out = _native(
                case["source_values"],
                case["categories"],
                case["weights"],
                mask_key=mask_key,
                namespace=case["namespace"],
                kernel=kernel,
            )
            assert out.type == pa.string()
            assert out.to_pylist() == case["expected_output"], (
                f"{kname}/{case['name']}: native drifted from the pinned KAT"
            )
    # Drift guard: the pinned vector must also equal the live oracle, so a
    # future edit to EITHER the kernel or the oracle handler fails here.
    for case in doc["cases"]:
        assert _oracle_keyed(case, mask_key) == case["expected_output"], (
            f"{case['name']}: oracle drifted from the pinned KAT"
        )


# ── Native-vs-oracle differential (byte-identity, value + type) ──────


_DIFF_CASES = [
    ("uniform_populated", ["a", "b", "c", "d", "e"], ["red", "green", "blue"], None),
    ("uniform_mixed_null", ["a", None, "c", "d"], ["red", "green", "blue"], None),
    ("uniform_single", ["x", "y", "z"], ["only"], None),
    ("uniform_duplicate_src", ["same", "same", "same"], ["A", "B", "C"], None),
    ("uniform_unicode", ["café", "naïve", "日本", None], ["α", "β", "γ"], None),  # noqa: RUF001
    ("uniform_empty", [], ["A", "B"], None),
    ("uniform_all_null", [None, None, None], ["A", "B"], None),
    ("weighted_populated", ["a", "b", "c", "d", "e"], ["red", "green", "blue"], [1.0, 2.0, 3.0]),
    ("weighted_equal", ["p", "q", "r", "s"], ["X", "Y"], [1.0, 1.0]),
    ("weighted_zero_band", [f"v{i}" for i in range(40)], ["A", "B", "C"], [1.0, 0.0, 1.0]),
    ("weighted_skewed", [f"v{i}" for i in range(40)], ["A", "B"], [0.99, 0.01]),
    ("weighted_mixed_null", ["a", None, "c", None, "e"], ["A", "B", "C"], [3.0, 1.0, 1.0]),
    ("weighted_all_null", [None, None], ["A", "B"], [1.0, 2.0]),
]


@pytest.mark.parametrize("case", _DIFF_CASES, ids=[c[0] for c in _DIFF_CASES])
def test_native_matches_oracle_values(case) -> None:
    _label, values, categories, weights = case
    oracle = _oracle(values, categories, weights)
    for kname, kernel in _index_kernels():
        out = _native(
            values, categories, weights, mask_key=_Ctx.mask_key, namespace="ns", kernel=kernel
        )
        assert out.type == pa.string(), kname
        assert out.to_pylist() == oracle, f"{kname}/{_label}"


# ── searchsorted(side="right") == bisect_right ──────────────────────


def test_searchsorted_right_equals_bisect_right() -> None:
    """The one FORM divergence between native (vectorized) and oracle (per-row
    `bisect.bisect_right`) on the weighted path. Random integer CDFs plus bucket
    arrays that INCLUDE exact threshold hits (the boundary case)."""
    rng = np.random.default_rng(20260921)
    for _ in range(200):
        k = int(rng.integers(1, 8))
        cdf = sorted(int(x) for x in rng.integers(0, _WEIGHTED_CDF_RES, size=k))
        cdf[-1] = _WEIGHTED_CDF_RES  # mirror `_build_cdf`'s last-entry contract
        # Buckets: random draws PLUS every exact CDF threshold (boundary hits).
        buckets = list(rng.integers(0, _WEIGHTED_CDF_RES, size=30)) + list(cdf)
        buckets_np = np.asarray(buckets, dtype=np.int64)
        vectorized = np.searchsorted(np.asarray(cdf, dtype=np.int64), buckets_np, side="right")
        per_row = [bisect.bisect_right(cdf, int(b)) for b in buckets]
        assert list(vectorized) == per_row

    # Explicit DUPLICATE-threshold CDFs (zero-weight bands) the random sweep never
    # produces: a zero-weight category makes two CDF entries equal, so the
    # right-biased search must skip the empty band exactly as `bisect_right` does.
    # Buckets deliberately land ON the duplicated threshold and either side of it.
    _R = _WEIGHTED_CDF_RES
    explicit_cdfs = [
        [500_000, 500_000, _R],  # middle category zero-weight (band at 500000)
        [_R, _R, _R],  # first category takes everything; two empty bands
        [0, 500_000, _R],  # first category zero-weight (threshold at 0)
        [1, 1, _R],  # near-zero leading bands
    ]
    for cdf in explicit_cdfs:
        buckets = [0, 1, 499_999, 500_000, 500_001, 999_999, _R - 1, _R, *cdf]
        buckets_np = np.asarray(buckets, dtype=np.int64)
        vectorized = np.searchsorted(np.asarray(cdf, dtype=np.int64), buckets_np, side="right")
        per_row = [bisect.bisect_right(cdf, int(b)) for b in buckets]
        assert list(vectorized) == per_row, cdf


# ── Kernel fail-closed invariants (malformed compiled/stub kernel) ──


class _BadTypeKernel:
    def derive_index_batch(self, values, *, mask_key, namespace, pool_size, native_threads=None):
        return pa.array([0] * len(values), type=pa.int32())  # wrong width


class _BadNullMaskKernel:
    def derive_index_batch(self, values, *, mask_key, namespace, pool_size, native_threads=None):
        # Drop the null: return all-valid indices even though the source has a null.
        return pa.array([0] * len(values), type=pa.uint64())


def test_kernel_rejects_wrong_index_type() -> None:
    with pytest.raises(GenerationError) as exc:
        native_categorical(
            pa.array(["a", "b"], type=pa.string()),
            categories=("X", "Y"),
            cdf=None,
            mask_key=_Ctx.mask_key,
            namespace="ns",
            index_kernel=_BadTypeKernel(),
        )
    assert exc.value.code == "index_batch_type_mismatch"


def test_kernel_rejects_null_mask_mismatch() -> None:
    with pytest.raises(GenerationError) as exc:
        native_categorical(
            pa.array(["a", None, "c"], type=pa.string()),
            categories=("X", "Y"),
            cdf=None,
            mask_key=_Ctx.mask_key,
            namespace="ns",
            index_kernel=_BadNullMaskKernel(),
        )
    assert exc.value.code == "index_batch_null_mask_mismatch"

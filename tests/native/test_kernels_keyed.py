"""Task 2.5: `native_keyed_hash` (the node) vs `reference_keyed_derivation` (the oracle).

Mirrors `test_kernels_scalar.py`'s split: this file drives the NEW node
in-process against the pure-Python reference and pins its own contract
(namespace/truncate resolution, fail-closed order, output-type stability,
partition invariance). The cross-process and cross-kernel-mutation harness
lives in `tests/parity/native/test_keyed_hash_parity.py` per
crypto-testing-reference Section 5.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import MaskKeyRequiredError
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native._crypto_ext import HASH_KAT, reference_keyed_derivation
from decoy_engine.execution.native._kernels_keyed import native_keyed_hash

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

_MASK_KEY = bytes(range(32))
_NAMESPACE = "people.ssn"
_REFERENCE = reference_keyed_derivation()


def _resolve_truncate_like_handler(raw: Any) -> int | None:
    """The exact resolution `HashStrategyHandler.run` performs before calling
    `hash_array`; the node must match it so config that never reaches the
    kernel as a raw value still behaves the way the shipped strategy does."""
    return raw if isinstance(raw, int) and raw > 0 else None


# ---------------------------------------------------------------------------
# Fail-closed: missing namespace (no companion required -- checked first).
# ---------------------------------------------------------------------------


def test_missing_namespace_fails_closed_without_calling_the_kernel() -> None:
    with pytest.raises(StrategyError) as exc_info:
        native_keyed_hash(pa.array(["alice"]), mask_key=_MASK_KEY, namespace=None, truncate=None)
    assert exc_info.value.code == "hash_requires_namespace"
    assert exc_info.value.strategy == "hash"


# ---------------------------------------------------------------------------
# Fail-closed: missing/None/empty mask_key -- the loader contract, needs the
# compiled kernel actually loaded to prove the guard fires before output.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
@pytest.mark.parametrize("mask_key", [None, b""])
def test_missing_or_empty_mask_key_raises_before_output(mask_key: bytes | None) -> None:
    with pytest.raises(MaskKeyRequiredError):
        native_keyed_hash(
            pa.array(["alice"]), mask_key=mask_key, namespace=_NAMESPACE, truncate=None
        )


# ---------------------------------------------------------------------------
# Byte parity against the live reference (companion needed for the real node).
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_matches_reference_over_hash_kat_vectors() -> None:
    for vector in HASH_KAT:
        array = pa.array([vector.value])
        got = native_keyed_hash(
            array, mask_key=vector.mask_key, namespace=vector.namespace, truncate=vector.truncate
        )
        assert got.to_pylist() == [vector.expected], vector
        assert got.type == pa.string()


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "values",
    [
        ["alice", "bob", None, "", "café", "café"],
        [-128, -1, 0, 1, 127, None],
        [True, False, None],
    ],
)
def test_matches_reference_over_a_small_generated_corpus(values: list[Any]) -> None:
    array = pa.array(values)
    reference_out = _REFERENCE.derive_batch(
        array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
    )
    native_out = native_keyed_hash(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)
    assert native_out.type == reference_out.type
    assert native_out.equals(reference_out)


# ---------------------------------------------------------------------------
# truncate resolution: mirrors HashStrategyHandler's `raw if isinstance(raw,
# int) and raw > 0 else None` exactly -- a raw config value never reaches the
# kernel un-resolved.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "raw_truncate",
    [16, 1, 0, -1, -16, None, "16", 1.5, True, False],
)
def test_truncate_resolves_exactly_like_the_handler(raw_truncate: Any) -> None:
    array = pa.array(["alice", "bob", None])
    resolved = _resolve_truncate_like_handler(raw_truncate)

    expected = _REFERENCE.derive_batch(
        array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=resolved
    )
    got = native_keyed_hash(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=raw_truncate)
    assert got.to_pylist() == expected.to_pylist(), raw_truncate


# ---------------------------------------------------------------------------
# Output-type + batch-schema stability: pa.string() for value-bearing,
# all-null, and empty batches alike (the out-of-core writer's schema
# requirement; mirrors native_redact/native_truncate's pinned contract).
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    ("label", "values"),
    [
        ("value_bearing", ["alice", None, "bob"]),
        ("all_null", [None, None]),
        ("empty", []),
    ],
)
def test_output_type_is_stable_string_across_batch_shapes(label: str, values: list[Any]) -> None:
    array = pa.array(values, type=pa.string())
    out = native_keyed_hash(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)
    assert out.type == pa.string(), label


# ---------------------------------------------------------------------------
# Partition invariance: three batch sizes, two orders, empty batches, and
# multiple Arrow chunk layouts (crypto-testing-reference Section 6.2).
# ---------------------------------------------------------------------------


def _partition_sizes(total: int, size: int) -> list[int]:
    sizes = [size] * (total // size)
    if total % size:
        sizes.append(total % size)
    return sizes


@_NEEDS_COMPANION
@pytest.mark.parametrize("batch_size", [1, 3, 7])
@pytest.mark.parametrize("reversed_order", [False, True])
def test_partition_invariance_across_batch_sizes_and_orders(
    batch_size: int, reversed_order: bool
) -> None:
    values = ["alice", "bob", None, "carol", "", "dave", None, "eve", "frank", "gina"]
    if reversed_order:
        values = list(reversed(values))
    whole = native_keyed_hash(
        pa.array(values, type=pa.string()), mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=8
    )

    pieces: list[pa.Array] = []
    offset = 0
    for size in _partition_sizes(len(values), batch_size):
        chunk_values = values[offset : offset + size]
        offset += size
        pieces.append(
            native_keyed_hash(
                pa.array(chunk_values, type=pa.string()),
                mask_key=_MASK_KEY,
                namespace=_NAMESPACE,
                truncate=8,
            )
        )
    concatenated = pa.concat_arrays(pieces)
    assert concatenated.to_pylist() == whole.to_pylist()


@_NEEDS_COMPANION
def test_partition_invariance_with_empty_batches_interspersed() -> None:
    values = ["alice", "bob", None, "carol"]
    whole = native_keyed_hash(
        pa.array(values, type=pa.string()), mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
    )

    empty = pa.array([], type=pa.string())
    pieces = [
        native_keyed_hash(empty, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None),
        native_keyed_hash(
            pa.array(values[:2], type=pa.string()),
            mask_key=_MASK_KEY,
            namespace=_NAMESPACE,
            truncate=None,
        ),
        native_keyed_hash(empty, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None),
        native_keyed_hash(
            pa.array(values[2:], type=pa.string()),
            mask_key=_MASK_KEY,
            namespace=_NAMESPACE,
            truncate=None,
        ),
        native_keyed_hash(empty, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None),
    ]
    concatenated = pa.concat_arrays(pieces)
    assert concatenated.to_pylist() == whole.to_pylist()


@_NEEDS_COMPANION
def test_partition_invariance_across_chunked_array_layouts() -> None:
    values = ["alice", "bob", None, "carol", "dave", "eve"]
    whole = native_keyed_hash(
        pa.array(values, type=pa.string()), mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
    )

    chunked_two = pa.chunked_array(
        [pa.array(values[:2], type=pa.string()), pa.array(values[2:], type=pa.string())]
    )
    chunked_per_row = pa.chunked_array([pa.array([v], type=pa.string()) for v in values])

    for chunked in (chunked_two, chunked_per_row):
        out = native_keyed_hash(chunked, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)
        assert out.to_pylist() == whole.to_pylist()


# ---------------------------------------------------------------------------
# Purity: no state, no draw beyond the deterministic derivation itself.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_is_pure_no_state() -> None:
    array = pa.array(["alice", "bob", None])
    first = native_keyed_hash(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)
    second = native_keyed_hash(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)
    assert first.to_pylist() == second.to_pylist()

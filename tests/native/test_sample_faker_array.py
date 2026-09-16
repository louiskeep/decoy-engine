"""Task 4.6 slice 1: `sample_faker_array` (promoted from `_sample_faker_chunk`
in `_chunk_masking.py`) called directly, isolated from both the native
chunked route and the shadow operator that now share it.

Companion-independent (uses the pure-Python `reference_index_derivation()`
kernel), so this always runs: it proves the rename + signature change
(`col_seed: Any` -> `namespace: str`) left the selection itself byte-
identical to an independent recomputation, guarding the promotion against a
silent behavior change reaching either caller.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

from decoy_engine.execution.native._chunk_masking import sample_faker_array
from decoy_engine.execution.native._index_ext import reference_index_derivation
from decoy_engine.generation.pool import ValuePool


def _stub_pool(values: list[str]) -> ValuePool:
    return ValuePool(
        values=np.array(values, dtype=object),
        provider="person_first_name",
        locale="default",
        config_hash="test-hash",
        seed=b"test-seed",
        size=len(values),
        build_time_ms=0.0,
        backend_type="faker",
        backend_version="0",
        distinct_count=len(set(values)),
    )


def test_sample_faker_array_matches_an_independent_index_recomputation() -> None:
    pool = _stub_pool(["v0", "v1", "v2", "v3"])
    mask_key = b"\x09" * 32
    namespace = "ns_focused"
    source = pa.array(["a", "b", None, "a"], type=pa.string())

    ref = reference_index_derivation()
    expected_idx = ref.derive_index_batch(
        source, mask_key=mask_key, namespace=namespace, pool_size=pool.size
    ).to_pylist()
    expected = [None if i is None else pool.values[i] for i in expected_idx]

    out = sample_faker_array(
        source,
        pool=pool,
        namespace=namespace,
        mask_key=mask_key,
        index_kernel=ref,
        native_threads=None,
    )

    assert out.type == pa.string()
    assert out.to_pylist() == expected


def test_sample_faker_array_null_positions_are_positional_not_label_aligned() -> None:
    pool = _stub_pool([f"v{i}" for i in range(10)])
    mask_key = b"\x0a" * 32
    # A ChunkedArray with a non-zero-offset second chunk, mirroring a
    # mid-stream batch: the null must restore at its own POSITION.
    chunk_a = pa.array(["a", None, "b"], type=pa.string())
    chunk_b = pa.array(["c", "a", None], type=pa.string())
    source = pa.chunked_array([chunk_a, chunk_b])

    ref = reference_index_derivation()
    out = sample_faker_array(
        source,
        pool=pool,
        namespace="ns_null_position",
        mask_key=mask_key,
        index_kernel=ref,
        native_threads=None,
    )

    assert [v is None for v in out.to_pylist()] == [False, True, False, False, False, True]


def test_sample_faker_array_repeated_source_values_select_identically() -> None:
    """Deterministic-reuse: the SAME source value must select the SAME pool
    entry every time it appears, regardless of position."""
    pool = _stub_pool([f"v{i}" for i in range(20)])
    mask_key = b"\x0b" * 32
    source = pa.array(["dup", "other", "dup", "dup"], type=pa.string())

    ref = reference_index_derivation()
    out = sample_faker_array(
        source,
        pool=pool,
        namespace="ns_repeat",
        mask_key=mask_key,
        index_kernel=ref,
        native_threads=None,
    ).to_pylist()

    assert out[0] == out[2] == out[3]
    assert out[1] != out[0]

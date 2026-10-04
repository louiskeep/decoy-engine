"""C1b-ii unit tests: `native_categorical_positional` and the uint64 position key.

The key for the non-null row at local index `i` of a chunk starting at global `row_offset`
is `encode_int(row_offset + i)`; a dense `uint64` column fed to `derive_index_batch`
reproduces the oracle's scalar `derive_index(mask_key, ns, encode_int(g), pool_size)`.
Expected indices are FROZEN LITERALS captured once from direct scalar `derive_index` calls
with `MK`/`"ns"`, so a change to the key encoding or the column dtype fails here.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._strategies._categorical import _WEIGHTED_CDF_RES, _build_cdf
from decoy_engine.execution.native._categorical_ext import (
    native_categorical,
    native_categorical_positional,
)
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._index_ext import (
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.generation.pool import GenerationError

MK = (0x0123456789).to_bytes(8, "big")
CATS = ["A", "B", "C", "D"]
# derive_index(MK, "ns", encode_int(g), pool_size=4) for g = 0..11 (C1b-i KAT).
KAT_UNIFORM = [2, 3, 1, 3, 0, 0, 0, 1, 3, 3, 1, 1]
# g = 5..11 spelled again for the nonzero-offset slice.
KAT_FROM_FIVE = [0, 0, 1, 3, 3, 1, 1]
# derive_index(MK, "ns", encode_int(g), pool_size=4) at the uint64 boundaries.
KAT_ACROSS_2_63 = [2, 2, 0]  # g = 2**63-2, 2**63-1, 2**63
KAT_AT_DOMAIN_TOP = [0, 3, 3]  # g = 2**64-3, 2**64-2, 2**64-1


def _kernels() -> list[Any]:
    ks = [reference_index_derivation()]
    if native_companion_status().ok:
        ks.append(load_compiled_index_kernel())
    return ks


def _run(
    values: list[str | None],
    *,
    row_offset: int = 0,
    cdf: list[int] | None = None,
    kernel: Any = None,
    threads: int | None = None,
    categories: list[str] | None = None,
) -> list[str | None]:
    out = native_categorical_positional(
        pa.array(values, pa.string()),
        row_offset=row_offset,
        categories=categories or CATS,
        cdf=cdf,
        mask_key=MK,
        namespace="ns",
        index_kernel=kernel or reference_index_derivation(),
        native_threads=threads,
    )
    assert out.type == pa.string()
    return list(out.to_pylist())


@pytest.mark.parametrize("kernel", _kernels(), ids=lambda k: type(k).__name__)
class TestPositionKeyedKats:
    def test_uniform_matches_the_frozen_indices_from_offset_zero(self, kernel: Any) -> None:
        out = _run([f"v{i}" for i in range(12)], kernel=kernel)
        assert out == [CATS[i] for i in KAT_UNIFORM]

    def test_a_nonzero_offset_shifts_the_position_key(self, kernel: Any) -> None:
        out = _run(["x"] * 7, row_offset=5, kernel=kernel)
        assert out == [CATS[i] for i in KAT_FROM_FIVE]

    def test_the_draw_ignores_the_source_value(self, kernel: Any) -> None:
        same = _run(["same"] * 12, kernel=kernel)
        varied = _run([f"v{i}" for i in range(12)], kernel=kernel)
        assert same == varied
        assert len(set(same)) > 1

    def test_a_null_keeps_its_position_and_the_next_row_uses_the_next_index(
        self, kernel: Any
    ) -> None:
        out = _run([None, "a", None, "b"], kernel=kernel)
        assert out == [None, CATS[KAT_UNIFORM[1]], None, CATS[KAT_UNIFORM[3]]]

    def test_an_all_null_column_stays_all_null(self, kernel: Any) -> None:
        assert _run([None, None, None], kernel=kernel) == [None, None, None]

    def test_an_empty_column_gives_an_empty_string_array(self, kernel: Any) -> None:
        assert _run([], kernel=kernel) == []

    @pytest.mark.parametrize(
        ("base", "expected"),
        [(2**63 - 2, KAT_ACROSS_2_63), (2**64 - 3, KAT_AT_DOMAIN_TOP)],
        ids=["across_2_63", "domain_top"],
    )
    def test_uint64_boundary_offsets_match_frozen_literals(
        self, kernel: Any, base: int, expected: list[int]
    ) -> None:
        assert _run(["x", "y", "z"], row_offset=base, kernel=kernel) == [CATS[i] for i in expected]

    def test_the_first_row_at_2_63_minus_1_and_2_63_are_distinct_positions(
        self, kernel: Any
    ) -> None:
        low = _run(["x"], row_offset=2**63 - 1, kernel=kernel)
        high = _run(["x"], row_offset=2**63, kernel=kernel)
        assert low == [CATS[KAT_ACROSS_2_63[1]]]
        assert high == [CATS[KAT_ACROSS_2_63[2]]]

    def test_weighted_uses_the_bucket_through_the_shared_cdf(self, kernel: Any) -> None:
        weights = [9.0, 1.0, 0.0, 2.0]
        cdf = _build_cdf(weights)
        buckets = kernel.derive_index_batch(
            pa.array(range(8), type=pa.uint64()),
            mask_key=MK,
            namespace="ns",
            pool_size=_WEIGHTED_CDF_RES,
        ).to_pylist()
        assert buckets == [50862, 705795, 680373, 622207, 733696, 472172, 22976, 460237]
        out = _run([f"v{i}" for i in range(8)], cdf=cdf, kernel=kernel)
        assert out == [CATS[i] for i in [0, 1, 1, 1, 1, 0, 0, 0]]

    @pytest.mark.parametrize("threads", [None, 1, 4])
    def test_the_thread_budget_does_not_change_the_bytes(self, kernel: Any, threads: Any) -> None:
        out = _run([f"v{i}" for i in range(12)], kernel=kernel, threads=threads)
        assert out == [CATS[i] for i in KAT_UNIFORM]


def test_a_position_past_the_uint64_domain_is_refused() -> None:
    with pytest.raises(GenerationError) as info:
        _run(["x", "y", "z"], row_offset=2**64 - 2)
    assert info.value.code == "categorical_position_out_of_domain"


def test_a_negative_offset_is_refused() -> None:
    with pytest.raises(GenerationError) as info:
        _run(["x"], row_offset=-1)
    assert info.value.code == "categorical_position_out_of_domain"


@pytest.mark.skipif(not native_companion_status().ok, reason="companion not installed")
def test_compiled_and_reference_kernels_agree_at_random_offsets() -> None:
    ref, compiled = reference_index_derivation(), load_compiled_index_kernel()
    values = [None if i % 5 == 1 else f"v{i}" for i in range(257)]
    for base in (0, 1, 2**32 - 3, 2**63 - 100, 2**64 - 300):
        assert _run(values, row_offset=base, kernel=ref) == _run(
            values, row_offset=base, kernel=compiled
        )


def test_the_value_keyed_sibling_is_unchanged_and_still_value_keyed() -> None:
    kernel = reference_index_derivation()
    a = native_categorical(
        pa.array(["x", "y", "x"], pa.string()),
        categories=CATS,
        cdf=None,
        mask_key=MK,
        namespace="ns",
        index_kernel=kernel,
    ).to_pylist()
    assert a[0] == a[2]

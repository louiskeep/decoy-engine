"""Round 3 of the typed out-of-core FK seam: resource exhaustion is not a dtype refusal.

Class 4 (silent fallback): `pyarrow.ArrowMemoryError` subclasses both `MemoryError` and
`ArrowException`, so a handler that maps Arrow errors to `out_of_core_fk_key_dtype_unsupported`
must let it (and any `MemoryError`) propagate as the same object, never re-labelled as a key
type the route cannot hold. Injected at every guarded call of the seam: the value build and the
cast check in `fk_output_array`, the cast in `cast_fk_chunk`, the cross-batch concat
(`_concat_fk_chunks`), the batch joiner's cast (`_batch_join._cast_chunks`) and the shared
per-batch join step (`_append_output_batch`, used by the batch and stream joiners).

Labels: R3FB = red at 261887fe, green after; R3CH = green at both.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.execution.out_of_core import _batch_join
from decoy_engine.execution.out_of_core import _join as join_mod
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge


def _oom() -> pa.ArrowMemoryError:
    return pa.ArrowMemoryError("injected: allocation failed")


class _ExhaustedChunk:
    """An FK chunk whose cast runs out of memory (the stand-in for an allocator failure)."""

    def __init__(self, dtype: pa.DataType, exc: BaseException) -> None:
        self.type = dtype
        self.exc = exc

    def cast(self, *_a: Any, **_k: Any) -> Any:
        raise self.exc


def _raising(exc: BaseException) -> Any:
    def fn(*_a: Any, **_k: Any) -> Any:
        raise exc

    return fn


def _assert_same(raised: pytest.ExceptionInfo[BaseException], exc: BaseException) -> None:
    assert raised.value is exc, (type(raised.value), getattr(raised.value, "code", None))


@pytest.mark.parametrize(
    "target",
    [pa.timestamp("ns"), pa.int64(), None],
    ids=["temporal-R3FB", "typed-R3FB", "inferred-R3FB"],
)
def test_fk_output_array_value_build_propagates_memory_error(
    target: pa.DataType | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = _oom()
    monkeypatch.setattr(pa, "array", _raising(exc))
    with pytest.raises(MemoryError) as raised:
        join_mod.fk_output_array([1, 2], target)
    _assert_same(raised, exc)


def test_fk_output_array_cast_check_propagates_memory_error_R3FB(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exc = _oom()
    monkeypatch.setattr(join_mod, "cast_fk_chunk", _raising(exc))
    with pytest.raises(MemoryError) as raised:
        join_mod.fk_output_array([1, 2], pa.float64())
    _assert_same(raised, exc)


@pytest.mark.parametrize(
    "exc_type",
    [pa.ArrowMemoryError, MemoryError],
    ids=lambda t: f"{t.__name__}-{'R3FB' if t is pa.ArrowMemoryError else 'R3CH'}",
)
def test_cast_fk_chunk_propagates_memory_error_unchanged(exc_type: type[BaseException]) -> None:
    exc = exc_type("injected: allocation failed")
    with pytest.raises(MemoryError) as raised:
        join_mod.cast_fk_chunk(_ExhaustedChunk(pa.string(), exc), pa.int64())
    _assert_same(raised, exc)


def test_concat_fk_chunks_propagates_memory_error_unchanged_R3FB() -> None:
    # string + binary unify to binary, so the string chunk is cast: that cast runs out of memory.
    exc = _oom()
    chunks = [_ExhaustedChunk(pa.string(), exc), pa.array([b"x"])]
    with pytest.raises(MemoryError) as raised:
        join_mod._concat_fk_chunks(chunks)
    _assert_same(raised, exc)


def test_batch_joiner_cast_propagates_memory_error_unchanged_R3FB() -> None:
    exc = _oom()
    with pytest.raises(MemoryError) as raised:
        _batch_join._cast_chunks([_ExhaustedChunk(pa.string(), exc)], pa.int64())
    _assert_same(raised, exc)


def _join_batch() -> pa.RecordBatch:
    return pa.record_batch(
        {
            "__decoy_row_nr": pa.array([0, 1], pa.int64()),
            "__decoy_fk_join_key": pa.array(["a", "b"]),
            "__decoy_src_0": pa.array([1, 2], pa.int64()),
            "__decoy_parent_match": pa.array(["a", None]),
            "__decoy_parent_masked_0": pa.array([10, None], pa.int64()),
        }
    )


_EDGE = RelationshipEdge(
    parent_table="p",
    parent_columns=("k",),
    child_table="c",
    child_columns=("k",),
    namespace="n",
    orphan_policy=OrphanPolicy.PRESERVE,
)


@pytest.mark.parametrize(
    "output_types",
    [(pa.int64(),), (pa.timestamp("us"),), None],
    ids=["typed-R3FB", "temporal-R3FB", "oracle-R3FB"],
)
def test_the_join_batch_step_propagates_memory_error(
    output_types: tuple[pa.DataType, ...] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = _join_batch()
    exc = _oom()
    monkeypatch.setattr(pa, "array", _raising(exc))
    chunks: list[list[pa.Array]] = [[]]
    with pytest.raises(MemoryError) as raised:
        join_mod._append_output_batch(
            batch, edge=_EDGE, remap_values=None, output_chunks=chunks, output_types=output_types
        )
    _assert_same(raised, exc)
    assert chunks == [[]]


# The narrowing must keep every genuine dtype or cast failure on the registered code.
_GENUINE: dict[str, tuple[list[object], pa.DataType | None]] = {
    "range": ([2**63 + 5], pa.int64()),
    "fraction": ([1.5], pa.int64()),
    "unimplemented": ([True], pa.date32()),
    "mixed": ([1, "a"], None),
    "temporal_range": ([__import__("datetime").datetime(9999, 1, 1)], pa.timestamp("ns")),
}


@pytest.mark.parametrize("name", [f"{n}-R3CH" for n in sorted(_GENUINE)])
def test_genuine_dtype_failures_keep_the_registered_code(name: str) -> None:
    values, target = _GENUINE[name.removesuffix("-R3CH")]
    with pytest.raises(ExecutionError) as raised:
        join_mod.fk_output_array(list(values), target)
    assert raised.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


def test_a_genuine_cast_failure_keeps_the_arrow_family_R3CH() -> None:
    with pytest.raises(pa.ArrowInvalid):
        join_mod.cast_fk_chunk(pa.array(["x"]), pa.int64())

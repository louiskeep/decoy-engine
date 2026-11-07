"""Chunked ordered Arrow dictionaries: which measures reconcile them, at what
cost, and how null indices survive re-indexing."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

import decoy_engine.quality.anonymity._classes as classes_module
from decoy_engine.quality.anonymity import (
    measure_k_anonymity,
    measure_l_diversity,
    measure_t_closeness_equal,
    measure_t_closeness_ordered,
)


def _chunk(indices: list[int | None], values: list[float]) -> pa.DictionaryArray:
    return pa.DictionaryArray.from_arrays(
        pa.array(indices, pa.int32()), pa.array(values), ordered=True
    )


def _two_chunk_table() -> pa.Table:
    column = pa.chunked_array([_chunk([1, 2], [1.0, 3.0, 4.0]), _chunk([1, 0], [3.0, 4.0])])
    return pa.table({"q": ["x", "x", "y", "y"], "s": column})


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = classes_module._reconcile_ordered_chunks

    def counting(chunks: Any, field: Any) -> Any:
        calls.append(len(chunks))
        return real(chunks, field)

    monkeypatch.setattr(classes_module, "_reconcile_ordered_chunks", counting)
    return calls


def test_only_ordered_t_reconciles_its_sensitive_column(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    table = _two_chunk_table()
    measure_k_anonymity(table, ["s"])
    measure_l_diversity(table, ["q"], "s")
    measure_t_closeness_equal(table, ["q"], "s")
    assert calls == []
    measure_t_closeness_ordered(table, ["q"], "s")
    assert calls == [2]


def test_ordered_quasi_identifier_is_not_reconciled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    table = _two_chunk_table().append_column("v", pa.array([1.0, 2.0, 3.0, 4.0]))
    measure_t_closeness_ordered(table, ["s"], "v")
    assert calls == []


def test_many_chunks_complete() -> None:
    """Two thousand row groups sharing one dictionary, plus a few nested
    subsets: the search runs over distinct dictionaries only."""
    shared = [1.0, 2.0, 3.0, 4.0]
    chunks = [_chunk([i % 4, (i + 1) % 4], shared) for i in range(2000)]
    chunks += [_chunk([0, 1], [1.0, 3.0]), _chunk([0], [2.0])]
    column = pa.chunked_array(chunks)
    n = len(column)
    table = pa.table({"q": [i % 3 for i in range(n)], "s": column})
    report = measure_t_closeness_ordered(table, ["q"], "s")
    assert report.distinct_sensitive_values == 4
    assert report.row_count == n
    assert measure_k_anonymity(table, ["s"]).class_count == 4


def test_null_index_survives_reindexing() -> None:
    """Both chunks are re-indexed onto [1, 3, 4]; their null indices must stay
    null (not become the first dictionary value, 1.0)."""
    column = pa.chunked_array(
        [_chunk([0, None, 2], [1.0, 3.0, 4.0]), _chunk([None, 1], [3.0, 4.0])]
    )
    table = pa.table({"q": ["x", "x", "y", "y", "y"], "s": column})
    plain = pd.DataFrame({"q": ["x", "x", "y", "y", "y"], "s": [1.0, None, 4.0, None, 4.0]})
    report = measure_t_closeness_ordered(table, ["q"], "s")
    assert report.rows_with_null_sensitive == 2
    assert report == measure_t_closeness_ordered(plain, ["q"], "s")


def test_order_search_runs_over_distinct_dictionaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 identical chunks plus one disagreeing chunk: two distinct
    dictionaries, so the search makes 3 subsequence checks (whole [1..4]:
    two checks, the second fails; whole [2, 1]: one check, fails). Keyed by
    chunk instead of dictionary contents it would make hundreds."""
    calls: list[int] = []
    real = classes_module._subsequence_positions

    def counting(part: Any, whole: Any) -> Any:
        calls.append(1)
        return real(part, whole)

    monkeypatch.setattr(classes_module, "_subsequence_positions", counting)
    chunks = [_chunk([0, 3], [1.0, 2.0, 3.0, 4.0]) for _ in range(500)]
    chunks.append(_chunk([0, 1], [2.0, 1.0]))
    column = pa.chunked_array(chunks)
    table = pa.table({"q": [i % 2 for i in range(len(column))], "s": column})
    with pytest.raises(TypeError, match="'s'"):
        measure_t_closeness_ordered(table, ["q"], "s")
    assert len(calls) == 3

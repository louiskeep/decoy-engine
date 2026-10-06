"""C8-i acceptance tests 5 and 5a: `when:` on the chunked native route.

Native chunked output equals the chunked oracle leg chunk by chunk and the whole-frame
oracle run, for the four admitted operators, with values, order and types per plan 3f.
Every case asserts the native route ran. A chunk where the predicate selects no row makes no
kernel call, is counted nowhere and does not credit the compiled backend.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution.native import _operator_step
from tests.native._c8_i_support import (
    CHUNKINGS,
    PREDICATES,
    chunk_by_sizes,
    run_native,
    run_oracle_leg,
    source_table,
    when_config,
)
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    key_provider,
)

_NEEDS_COMPANION_KINDS = {"hash", "categorical"}


def _kind_param(kind: str) -> Any:
    marks = [NEEDS_COMPANION] if kind in _NEEDS_COMPANION_KINDS else []
    return pytest.param(kind, marks=marks, id=kind)


KINDS = [_kind_param(k) for k in ("redact", "truncate", "hash", "categorical")]


def _whole_frame(config: dict[str, Any], source: pa.Table, tmp_path: Path) -> pa.Table:
    path = str(tmp_path / "source.parquet")
    pq.write_table(source, path)
    config = copy.deepcopy(config)
    config["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    config["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    result = run_pipeline(
        config,
        {TABLE: source},
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=key_provider(),
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    return result.outputs[TABLE]


def _values(chunks: list[pa.Table], name: str) -> list[Any]:
    return [v for c in chunks for v in c.column(name).to_pylist()]


@pytest.mark.parametrize("predicate", sorted(PREDICATES))
@pytest.mark.parametrize("kind", KINDS)
def test_native_chunked_equals_the_oracle_leg_and_the_whole_frame_run(
    kind: str, predicate: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_table()
    config = when_config(kind, PREDICATES[predicate])
    whole = _whole_frame(config, source, tmp_path)
    for name, sizes in CHUNKINGS.items():
        chunks = chunk_by_sizes(source, sizes)
        native, evidence = run_native(config, chunks)
        assert evidence.native_admitted is True, (name, evidence.reroute_reason)
        assert {r.column: r.route for r in evidence.node_routes}["s"] == "native_kernel"
        oracle, oracle_evidence = run_oracle_leg(config, chunks, monkeypatch)
        assert oracle_evidence.native_admitted is False
        assert len(native) == len(oracle) == len(chunks), name
        for got, want in zip(native, oracle, strict=True):
            assert got.schema.names == want.schema.names
            for column in got.schema.names:
                assert got.column(column).to_pylist() == want.column(column).to_pylist(), (
                    name,
                    column,
                )
            # Plan 3f: an admitted `when` column is `string` on every chunk of both legs.
            assert got.schema.field("s").type == want.schema.field("s").type == pa.string()
        assert _values(native, "s") == whole.column("s").to_pylist(), name
        for column in ("p", "u", "n"):
            assert _values(native, column) == whole.column(column).to_pylist()
        assert whole.schema.field("s").type == pa.string()


@pytest.mark.parametrize("kind", KINDS)
def test_degenerate_chunks_keep_the_string_type_and_values(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero-row, single-row and all-null chunks in one stream, against the oracle leg."""
    base = source_table()
    all_null = base.slice(0, 3).set_column(0, "s", pa.array([None] * 3, pa.string()))
    all_null_pred = base.slice(3, 3).set_column(1, "p", pa.array([None] * 3, pa.string()))
    chunks = [base.slice(0, 0), all_null, base.slice(6, 1), all_null_pred, base.slice(0, 0)]
    for predicate in ("p == 'x'", "p != 'x'", "s == 'red'", "p == 'zzz'"):
        config = when_config(kind, predicate)
        native, evidence = run_native(config, chunks)
        assert evidence.native_admitted is True, evidence.reroute_reason
        oracle, _ = run_oracle_leg(config, chunks, monkeypatch)
        assert [t.schema.field("s").type for t in native] == [pa.string()] * len(chunks)
        assert [t.schema.field("s").type for t in oracle] == [pa.string()] * len(chunks)
        assert [t.to_pydict() for t in native] == [t.to_pydict() for t in oracle], predicate


@pytest.mark.parametrize("kind", KINDS)
def test_unselected_rows_keep_their_source_value_and_selected_rows_change(kind: str) -> None:
    source = source_table()
    config = when_config(kind, "p == 'x'")
    out, _ = run_native(config, chunk_by_sizes(source, [4, 4, 3]))
    got = _values(out, "s")
    want = source.column("s").to_pylist()
    selected = [v == "x" for v in source.column("p").to_pylist()]
    assert any(selected) and not all(selected)
    for i, sel in enumerate(selected):
        if not sel:
            assert got[i] == want[i]
        elif want[i] is None:
            assert got[i] is None
        else:
            assert got[i] != want[i]


# ---------------------------------------------------------------------------
# 5a. Zero-match chunks are skipped, idle and uncounted.
# ---------------------------------------------------------------------------


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    calls: list[int] = []
    real = getattr(_operator_step, name)

    def spy(source: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(len(source))
        return real(source, *args, **kwargs)

    monkeypatch.setattr(_operator_step, name, spy)
    return calls


def _per_chunk_executed(sink: list[Any], column: str) -> list[str]:
    out = []
    for result in sink:
        cols = {c["column"]: c for c in result.quality_metrics["chunked_route"]["columns"]}
        out.append(cols[column]["executed_backend"])
    return out


def test_a_zero_match_chunk_makes_no_kernel_call_and_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_table()
    # p == 'x' rows: 0, 3, 5, 8, 9. Chunks of [0:3), [3:6), [6:8), [8:11).
    config = when_config("redact", "p == 'x'")
    calls = _spy(monkeypatch, "native_redact")
    sink: list[Any] = []
    out, evidence = run_native(config, chunk_by_sizes(source, [3, 3, 2, 3]), sink=sink)
    assert evidence.native_admitted is True
    # [6:8) holds p = [None, 'y']: no selected row.
    assert calls == [3, 3, 3]
    assert evidence.kernel_calls == {"redact": 3}
    assert len(out) == 4
    assert out[2].column("s").to_pylist() == source.column("s").to_pylist()[6:8]


def test_an_all_zero_match_run_counts_no_kernel_call_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(monkeypatch, "native_redact")
    out, evidence = run_native(
        when_config("redact", "p == 'zzz'"), chunk_by_sizes(source_table(), [4, 4, 3])
    )
    assert calls == []
    assert evidence.kernel_calls == {} and evidence.compiled_kernel_executed is False
    assert _values(out, "s") == source_table().column("s").to_pylist()


def test_a_zero_row_chunk_is_a_zero_match_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "native_truncate")
    _out, evidence = run_native(
        when_config("truncate", "p == 'x'"), chunk_by_sizes(source_table(), [0, 5, 0, 6])
    )
    assert calls == [5, 6]
    assert evidence.kernel_calls == {"truncate": 2}


@NEEDS_COMPANION
def test_the_compiled_backend_is_credited_only_for_chunks_that_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_table()
    config = when_config("hash", "p == 'x'")
    sink: list[Any] = []
    _out, evidence = run_native(config, chunk_by_sizes(source, [3, 3, 2, 3]), sink=sink)
    assert _per_chunk_executed(sink, "s") == [
        "rust_companion",
        "rust_companion",
        "arrow_python",
        "rust_companion",
    ]
    assert evidence.compiled_kernel_executed is True
    assert evidence.kernel_calls == {"hash": 3}


@NEEDS_COMPANION
def test_a_run_of_only_zero_match_chunks_never_credits_the_compiled_backend() -> None:
    sink: list[Any] = []
    _out, evidence = run_native(
        when_config("hash", "p == 'zzz'"), chunk_by_sizes(source_table(), [4, 4, 3]), sink=sink
    )
    assert _per_chunk_executed(sink, "s") == ["arrow_python"] * 3
    assert evidence.compiled_kernel_executed is False
    assert evidence.kernel_calls == {}

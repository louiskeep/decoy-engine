"""SAMPLE SE2: the sample stage inside a run (AT-9, AT-10, AT-10a, AT-10b, AT-11, AT-13, AT-13a)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

import decoy_engine.subset as subset_pkg
from decoy_engine import run_pipeline
from decoy_engine.profile._lazy_csv import CsvLazySource
from decoy_engine.subset import sample_request
from tests.unit.execution import _sample_run_support as sup


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sup.VAR, "ab" * 32)


def _sample(cfg: dict[str, Any], sources: Any, rows: int = 5, **kw: Any) -> Any:
    request = sample_request(cfg, rows=rows, **kw)
    return run_pipeline(cfg, sources, engine_version="t", subset_request=request)


def _full(cfg: dict[str, Any], sources: Any) -> Any:
    return run_pipeline(cfg, sources, engine_version="t", subset_request="skip")


def _orphans(out: dict[str, pa.Table], edges: list[sup.Edge]) -> int:
    """Child rows whose (non-null) key tuple is absent from the masked parent."""
    missing = 0
    for parent, pcols, child, ccols in edges:
        have = set(zip(*(out[parent].column(c).to_pylist() for c in pcols), strict=True))
        for key in zip(*(out[child].column(c).to_pylist() for c in ccols), strict=True):
            missing += None not in key and key not in have
    return missing


def test_at9_an_edgeless_lazy_csv_head_is_read_once_and_never_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, data, _ = sup.chain_job(tmp_path, fmt="csv")
    lazy = sup.lazy_sources(cfg)
    real_count = CsvLazySource._count_pass
    real_iter = CsvLazySource.iter_batches
    pulled: list[int] = []
    closed: list[bool] = []

    def count(self: CsvLazySource) -> Any:
        if self.table == "notes":
            pytest.fail("the head table was counted")
        return real_count(self)

    def iter_batches(self: CsvLazySource, batch_rows: int, **kw: Any) -> Iterator[Any]:
        inner = real_iter(self, batch_rows, **kw)
        if self.table != "notes":
            return inner

        def probe() -> Iterator[Any]:
            assert batch_rows == 5
            try:
                for batch in inner:
                    pulled.append(batch.num_rows)
                    yield batch
            finally:
                closed.append(True)
                inner.close()

        return probe()

    real_select = subset_pkg.select_subset
    received: list[set[str]] = []

    def select(request: Any, inputs: Any, **kw: Any) -> Any:
        received.append(set(inputs))
        return real_select(request, inputs, **kw)

    monkeypatch.setattr(CsvLazySource, "_count_pass", count)
    monkeypatch.setattr(CsvLazySource, "iter_batches", iter_batches)
    monkeypatch.setattr(subset_pkg, "select_subset", select)
    res = _sample(cfg, lazy)
    assert received == [{"customers", "orders", "items"}]
    assert pulled == [5] and closed == [True]
    assert res.outputs["notes"].num_rows == 5
    full = _full(cfg, {n: t for n, t in data.items()})
    assert (
        res.outputs["notes"].column("nid").to_pylist()
        == (full.outputs["notes"].column("nid").to_pylist()[:5])
    )
    assert res.quality_metrics["subset"]["sample"]["head_tables"] == {"notes": 5}
    assert "notes" not in res.quality_metrics["subset"]["tables"]


def test_at10_default_seeds_close_upward_with_no_orphans(tmp_path: Path) -> None:
    cfg, data, _ = sup.chain_job(tmp_path)
    res = _sample(cfg, data)
    assert {n: t.num_rows for n, t in res.outputs.items()} == {
        "customers": 5,
        "orders": 5,
        "items": 5,
        "notes": 5,
    }
    assert _orphans(res.outputs, sup.CHAIN_EDGES) == 0
    full = _full(cfg, data)
    # The kept rows are the full run's first rows, so their masked keys are its first keys.
    assert (
        res.outputs["items"].column("iid").to_pylist()
        == (full.outputs["items"].column("iid").to_pylist()[:5])
    )


def _hr_job(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table], list[sup.Edge]]:
    """region <- emp (self-reference chain) <- ticket, and ticket -> region as a second parent."""
    region = pa.table({"rid": [f"R{i}" for i in range(5)]})
    emp = pa.table(
        {
            "eid": [f"E{i}" for i in range(20)],
            "mgr": [None] + [f"E{i - 1}" for i in range(1, 20)],
            "rid": [f"R{i % 5}" for i in range(20)],
        }
    )
    ticket = pa.table(
        {
            "tid": [f"T{i}" for i in range(40)],
            "eid": [f"E{10 + i % 10}" for i in range(40)],
            "rid": [f"R{i % 5}" for i in range(40)],
        }
    )
    edges: list[sup.Edge] = [
        ("region", ("rid",), "emp", ("rid",)),
        ("emp", ("eid",), "emp", ("mgr",)),
        ("emp", ("eid",), "ticket", ("eid",)),
        ("region", ("rid",), "ticket", ("rid",)),
    ]
    data = {"region": region, "emp": emp, "ticket": ticket}
    cfg, _ = sup.build(tmp_path, data, edges)
    return cfg, data, edges


def test_at10a_later_children_and_a_self_reference_chain(tmp_path: Path) -> None:
    cfg, data, edges = _hr_job(tmp_path)
    res = _sample(cfg, data, rows=3)
    rows = {n: t.num_rows for n, t in res.outputs.items()}
    # Three tickets need emps E10..E12, whose manager chain reaches E0: more than N rows in emp.
    assert rows == {"ticket": 3, "emp": 13, "region": 5}
    assert _orphans(res.outputs, edges) == 0
    assert sum(rows.values()) <= 1_000_000


def test_at10b_a_partly_null_composite_key_pulls_no_parent(tmp_path: Path) -> None:
    acct = pa.table(
        {
            "a": [f"A{i}" for i in range(10)],
            "b": ["B0", None, *[f"B{i}" for i in range(2, 10)]],
        }
    )
    txn = pa.table(
        {
            "tid": ["T0", "T1", "T2", "T3"],
            "a": ["A0", "A1", "A2", "A3"],
            "b": ["B0", None, "B2", "B3"],
        }
    )
    edges: list[sup.Edge] = [("acct", ("a", "b"), "txn", ("a", "b"))]
    cfg, _ = sup.build(tmp_path, {"acct": acct, "txn": txn}, edges)
    res = _sample(cfg, {"acct": acct, "txn": txn}, rows=2)
    assert res.outputs["txn"].num_rows == 2
    # T0 pulls (A0, B0); T1 has a null component, so it survives and pulls nothing (not A1, null).
    assert res.outputs["acct"].num_rows == 1
    full = _full(cfg, {"acct": acct, "txn": txn})
    assert (
        res.outputs["acct"].column("a").to_pylist()
        == (full.outputs["acct"].column("a").to_pylist()[:1])
    )


@pytest.mark.parametrize(
    ("fmt", "lazy"),
    [
        ("parquet", False),
        ("parquet", True),
        ("csv", False),
        ("csv", True),
        ("tab", False),
        ("tab", True),
        ("fixed_width", False),
    ],
)
def test_at11_every_format_keeps_the_same_rows(tmp_path: Path, fmt: str, lazy: bool) -> None:
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    cfg0, data, _ = sup.chain_job(base_dir)
    want = _sample(cfg0, data)
    cfg, _, lazy_sources = sup.chain_job(tmp_path, fmt=fmt)
    sources = lazy_sources if lazy else sup.resident_sources(cfg)
    got = _sample(cfg, sources)
    assert set(got.outputs) == set(want.outputs)
    for name, table in want.outputs.items():
        for column in table.schema.names:
            if column == "body":  # shuffled: valid output, not the same permutation
                continue
            assert got.outputs[name].column(column).to_pylist() == (
                table.column(column).to_pylist()
            ), (name, column)


def test_at13_metrics_shape_and_display_rows(tmp_path: Path) -> None:
    cfg, data, _ = sup.chain_job(tmp_path)
    res = _sample(cfg, data, display_rows=3)
    block = res.quality_metrics["subset"]
    assert block["sample"] == {
        "rows": 5,
        "roots": ["items", "notes"],
        "head_tables": {"notes": 5},
        "approximate_columns": [["notes", "body"]],
    }
    shown = res.sample_source_rows
    assert shown is not None and set(shown) == {"customers", "orders", "items", "notes"}
    assert all(t.num_rows == 3 for t in shown.values())
    assert shown["notes"].column("nid").to_pylist() == ["N0", "N1", "N2"]
    assert shown["items"].column("iid").to_pylist() == ["I0", "I1", "I2"]
    assert "sample_source_rows" not in repr(res)


def test_at13a_source_rows_are_off_unless_asked_for(tmp_path: Path) -> None:
    cfg, data, _ = sup.chain_job(tmp_path)
    assert _sample(cfg, data).sample_source_rows is None
    assert _full(cfg, data).sample_source_rows is None

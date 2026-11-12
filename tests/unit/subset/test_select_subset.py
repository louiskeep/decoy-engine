"""`select_subset`: one selection over resident, lazy and path inputs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons
from decoy_engine.fileaccess import (
    ENTRY_POINTS,
    FileAccessDenied,
    FileRole,
    Root,
    RootedFileAccess,
)
from decoy_engine.profile import LazySource
from decoy_engine.subset import (
    FanOutPolicy,
    SeedSpec,
    SubsetConfigError,
    SubsetRequest,
    SubsetSource,
    plan_subset,
    run_subset,
    run_subset_preflight,
    select_subset,
    subset_request_from_config,
)
from decoy_engine.subset._edges import build_subset_edges
from tests.fileaccess.support import ALL_ACCESS, RecordingFileAccess
from tests.unit.subset._fixtures_inputs import (
    CHILD,
    PARENT,
    csv_source,
    request,
    text_tables,
)
from tests.unit.subset.conftest import JOB_SEED, make_parquet, rel


def _open_policy(tmp_path: Path) -> RootedFileAccess:
    """A policy that serves any file under `tmp_path`, portable so it also runs off Linux."""
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    root = Root("tmp", fd, frozenset(FileRole), ALL_ACCESS, prefix=str(tmp_path))
    return RootedFileAccess([root], allow_portable_fallback=True)


def _parquet_inputs(tmp_path: Path) -> dict[str, SubsetSource]:
    return {
        name: SubsetSource(path=make_parquet(tmp_path, name, table.to_pydict()), format="parquet")
        for name, table in text_tables().items()
    }


def _kept_ids(tmp_path: Path, req: SubsetRequest) -> dict[str, list[str]]:
    out = tmp_path / "out"
    run_subset(
        sources=_parquet_inputs(tmp_path),
        relationships=req.relationships,
        seeds=req.seeds,
        policy=req.policy,
        job_seed=JOB_SEED,
        engine_version="t",
        output_dir=out,
    )
    return {
        "parent": pq.read_table(out / "parent.parquet")["id"].to_pylist(),
        "child": pq.read_table(out / "child.parquet")["cid"].to_pylist(),
    }


def _ids(selection, name: str, col: str, data: dict) -> list[str]:  # type: ignore[no-untyped-def]
    return [data[col][i] for i in sorted(selection.survivors[name])]


def test_resident_selection_equals_the_parquet_run(tmp_path: Path) -> None:
    req = request()
    sel = select_subset(req, text_tables(), job_seed=JOB_SEED)
    kept = _kept_ids(tmp_path, req)
    assert _ids(sel, "parent", "id", PARENT) == kept["parent"] == ["b", "c"]
    assert _ids(sel, "child", "cid", CHILD) == kept["child"] == ["3", "4"]
    assert sel.counts["parent"] == (5, 2)
    assert sel.estimate.total_surviving_rows == 4


def test_csv_text_keys_select_the_same_rows_as_text_parquet(tmp_path: Path) -> None:
    req = request()
    lazy = {
        "parent": csv_source(tmp_path, "parent", PARENT),
        "child": csv_source(tmp_path, "child", CHILD),
    }
    sel = select_subset(req, lazy, job_seed=JOB_SEED)
    kept = _kept_ids(tmp_path, req)
    assert _ids(sel, "parent", "id", PARENT) == kept["parent"]
    assert _ids(sel, "child", "cid", CHILD) == kept["child"]


@dataclass(frozen=True)
class _Recording(LazySource):
    calls: list = field(default_factory=list, compare=False)

    def iter_batches(self, batch_rows, *, columns=None):  # type: ignore[no-untyped-def]
        self.calls.append(columns)
        yield from super().iter_batches(batch_rows, columns=columns)


def test_only_key_columns_are_read(tmp_path: Path) -> None:
    pq.write_table(pa.table(PARENT), tmp_path / "p.parquet")
    pq.write_table(pa.table(CHILD), tmp_path / "c.parquet")
    p = _Recording(tmp_path / "p.parquet")
    c = _Recording(tmp_path / "c.parquet")
    select_subset(request(), {"parent": p, "child": c}, job_seed=JOB_SEED)
    assert p.calls and c.calls
    assert all(cols is not None and "label" not in cols for cols in p.calls)
    assert all(cols is not None for cols in c.calls)


def test_csv_text_key_against_parquet_int_key_refuses_with_names_only(tmp_path: Path) -> None:
    canary = "CANARY-77"
    parent = SubsetSource(path=make_parquet(tmp_path, "parent", {"id": [1, 2]}), format="parquet")
    child = csv_source(tmp_path, "child", {"cid": ["1", canary], "pid": ["1", "2"]})
    report = run_subset_preflight(
        sources={"parent": parent, "child": child},
        relationships=(rel("parent", ("id",), "child", ("pid",)),),
    )
    assert not report.passed
    assert report.failures[0].code == reasons.subset.SUBSET_RELATIONSHIP_TYPE_MISMATCH
    assert canary not in report.failures[0].message


def test_int_literal_seed_on_csv_text_key_refuses(tmp_path: Path) -> None:
    lazy = {
        "parent": csv_source(tmp_path, "parent", PARENT),
        "child": csv_source(tmp_path, "child", CHILD),
    }
    req = SubsetRequest(
        relationships=request().relationships,
        seeds=(SeedSpec(table="parent", mode="keys", key_columns=("id",), keys=((1,),)),),
        policy=FanOutPolicy(),
    )
    with pytest.raises(SubsetConfigError) as exc:
        select_subset(req, lazy, job_seed=JOB_SEED)
    assert exc.value.code == reasons.config.SUBSET_SEED_KEY_TYPE


def test_unlinked_table_is_whole_with_no_survivor_entry() -> None:
    sel = select_subset(request(), text_tables(), job_seed=JOB_SEED)
    assert sel.scope.whole == frozenset({"lonely"})
    assert "lonely" not in sel.survivors
    assert sel.counts["lonely"] == (2, 2)
    assert set(sel.survivors) == {"parent", "child"}


def test_file_access_rules(tmp_path: Path) -> None:
    # (a) the entry is listed and wrapped
    assert "decoy_engine.subset._api.select_subset" in ENTRY_POINTS
    assert select_subset.__decoy_entry_point__  # type: ignore[attr-defined]
    deny = RootedFileAccess([], allow_portable_fallback=True)
    # (b) a denying policy refuses run_subset before any output exists
    out = tmp_path / "out"
    req = request()
    with pytest.raises(FileAccessDenied):
        run_subset(
            sources=_parquet_inputs(tmp_path),
            relationships=req.relationships,
            seeds=req.seeds,
            policy=req.policy,
            job_seed=JOB_SEED,
            engine_version="t",
            output_dir=out,
            file_access=deny,
        )
    assert not out.exists()
    # (c) a LazySource keeps the policy it was built under
    pq.write_table(pa.table(PARENT), tmp_path / "p.parquet")
    pq.write_table(pa.table(CHILD), tmp_path / "c.parquet")
    rec = RecordingFileAccess(_open_policy(tmp_path))
    inputs = {
        "parent": LazySource(tmp_path / "p.parquet", deny),
        "child": LazySource(tmp_path / "c.parquet", deny),
    }
    with pytest.raises(FileAccessDenied):
        select_subset(req, inputs, job_seed=JOB_SEED, file_access=rec)
    assert rec.requests == []
    # (d) resident inputs open nothing
    select_subset(req, text_tables(), job_seed=JOB_SEED, file_access=rec)
    assert rec.requests == []


def test_standalone_entries_keep_their_output_and_parquet_only_gate(tmp_path: Path) -> None:
    req = request()
    srcs = _parquet_inputs(tmp_path)
    plan = plan_subset(
        sources=srcs,
        relationships=req.relationships,
        seeds=req.seeds,
        policy=req.policy,
        job_seed=JOB_SEED,
        engine_version="t",
    )
    result = run_subset(
        sources=srcs,
        relationships=req.relationships,
        seeds=req.seeds,
        policy=req.policy,
        job_seed=JOB_SEED,
        engine_version="t",
        output_dir=tmp_path / "out",
    )
    assert result.plan == plan
    assert pl.read_parquet(tmp_path / "out" / "parent.parquet")["id"].to_list() == ["b", "c"]
    csv_path = tmp_path / "parent.csv"
    csv_path.write_text("id\na\n", encoding="utf-8")
    bad = {**srcs, "parent": SubsetSource(path=str(csv_path), format="csv")}
    report = run_subset_preflight(sources=bad, relationships=req.relationships, seeds=req.seeds)
    assert report.failures[0].code == reasons.subset.SUBSET_REQUIRES_PARQUET


def test_two_table_cycle_closes_with_every_kept_fk_resolved() -> None:
    n = 5
    a = pa.table({"id": list(range(1, n + 1)), "b_id": [10 * i for i in range(1, n + 1)]})
    b = pa.table(
        {"id": [10 * i for i in range(1, n + 1)], "a_id": [min(i + 1, n) for i in range(1, n + 1)]}
    )
    req = SubsetRequest(
        relationships=(rel("b", ("id",), "a", ("b_id",)), rel("a", ("id",), "b", ("a_id",))),
        seeds=(SeedSpec(table="a", mode="keys", key_columns=("id",), keys=((1,),)),),
        policy=FanOutPolicy(),
    )
    sel = select_subset(req, {"a": a, "b": b}, job_seed=JOB_SEED)
    a_ids = {a["id"][i].as_py() for i in sel.survivors["a"]}
    b_ids = {b["id"][i].as_py() for i in sel.survivors["b"]}
    assert all(a["b_id"][i].as_py() in b_ids for i in sel.survivors["a"])
    assert all(b["a_id"][i].as_py() in a_ids for i in sel.survivors["b"])
    assert len(a_ids) == n


def test_upward_only_child_is_in_scope_with_no_rows_not_whole() -> None:
    base = request()
    (edge,) = build_subset_edges(base.relationships)
    req = SubsetRequest(
        relationships=base.relationships,
        seeds=base.seeds,
        policy=FanOutPolicy(edge_directions=((edge.edge_id, "upward"),), allow_dangling=True),
    )
    sel = select_subset(req, text_tables(), job_seed=JOB_SEED)
    assert "child" in sel.scope.tables
    assert "child" not in sel.scope.whole
    assert sel.survivors["child"] == frozenset()
    assert sel.counts["child"] == (5, 0)


def test_generate_kind_edge_is_dropped_and_listed_not_refused() -> None:
    config = {
        "tables": [
            {"name": "parent", "columns": [{"name": "id"}]},
            {"name": "gen", "generate_columns": [{"name": "pid"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "gen", "columns": ["pid"]}],
                "orphan_policy": "preserve",
            }
        ],
        "subset": {
            "seeds": [{"table": "parent", "mode": "keys", "key_columns": ["id"], "keys": [["b"]]}]
        },
    }
    req = subset_request_from_config(config)
    assert req is not None and req.relationships == ()
    sel = select_subset(req, {"parent": text_tables()["parent"]}, job_seed=JOB_SEED)
    assert sel.excluded_edges == ("parent.id -> gen.pid",)
    assert sel.counts["parent"] == (5, 1)


def test_generate_child_is_dropped_per_edge_and_the_mask_child_stays_in_scope() -> None:
    config = {
        "tables": [
            {"name": "parent", "columns": [{"name": "id"}]},
            {"name": "child", "columns": [{"name": "pid"}]},
            {"name": "gen", "generate_columns": [{"name": "pid"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [
                    {"table": "child", "columns": ["pid"]},
                    {"table": "gen", "columns": ["pid"]},
                ],
                "orphan_policy": "preserve",
            }
        ],
        "subset": {
            "seeds": [{"table": "parent", "mode": "keys", "key_columns": ["id"], "keys": [["b"]]}]
        },
    }
    req = subset_request_from_config(config)
    assert req is not None
    assert [e.edge_id for e in build_subset_edges(req.relationships)] == ["parent.id -> child.pid"]
    assert req.excluded_edges == ("parent.id -> gen.pid",)
    tables = text_tables()
    sel = select_subset(
        req, {"parent": tables["parent"], "child": tables["child"]}, job_seed=JOB_SEED
    )
    assert "child" in sel.scope.tables and "child" not in sel.scope.whole
    kept_parents = set(_ids(sel, "parent", "id", PARENT))
    assert kept_parents == {"b"}
    kept_fks = {CHILD["pid"][i] for i in sel.survivors["child"]}
    assert kept_fks and kept_fks <= kept_parents
    assert sel.excluded_edges == ("parent.id -> gen.pid",)

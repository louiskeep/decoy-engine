"""The config-to-request binding: generate-kind edges leave the run path, not standalone subset."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.subset import (
    FanOutPolicy,
    SubsetRequest,
    relationships_from_config,
    run_subset_preflight,
    select_subset,
    subset_inputs_from_config,
    subset_request_from_config,
)
from decoy_engine.subset._edges import build_subset_edges
from tests.unit.subset.conftest import JOB_SEED_CONFIG, make_parquet

IDS = ["a", "b", "c", "d", "e"]


def _config(
    tables: dict[str, str],
    relationships: list[tuple[str, str, str, str]],
    seed_table: str,
    sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`tables` maps a name to "mask" or "generate"; each relationship is (parent, pcol, child, ccol)."""
    config: dict[str, Any] = {
        "tables": [
            {"name": n, ("columns" if kind == "mask" else "generate_columns"): [{"name": "x"}]}
            for n, kind in tables.items()
        ],
        "relationships": [
            {
                "parent": {"table": p, "columns": [pc]},
                "children": [{"table": c, "columns": [cc]}],
                "orphan_policy": "preserve",
            }
            for p, pc, c, cc in relationships
        ],
        "subset": {
            "seeds": [{"table": seed_table, "mode": "keys", "key_columns": ["id"], "keys": [["b"]]}]
        },
    }
    if sources is not None:
        config["sources"] = sources
    return config


def _table(fk: str) -> pa.Table:
    return pa.table({"id": IDS, fk: IDS})


def _request(config: dict[str, Any], **policy: Any) -> SubsetRequest:
    req = subset_request_from_config(config)
    assert req is not None
    edge_directions = policy.get("edge_directions", ())
    return SubsetRequest(
        relationships=req.relationships,
        seeds=req.seeds,
        policy=FanOutPolicy(
            edge_directions=edge_directions, allow_dangling=policy.get("allow_dangling", False)
        ),
        excluded_edges=req.excluded_edges,
    )


def test_standalone_inputs_keep_a_generate_edge_and_preflight_refuses_it(tmp_path: Path) -> None:
    sources = {
        n: {
            "type": "file",
            "format": "parquet",
            "path": make_parquet(tmp_path, n, _table(fk).to_pydict()),
        }
        for n, fk in (("customers", "cid"), ("orders", "gid"))
    }
    config = _config(
        {"customers": "mask", "orders": "mask", "gen": "generate"},
        [("gen", "id", "orders", "gid")],
        "customers",
        sources,
    )
    src, rels, seeds, _policy = subset_inputs_from_config(config)
    assert rels == relationships_from_config(config) and rels != ()
    report = run_subset_preflight(sources=src, relationships=rels, seeds=seeds)
    assert not report.passed
    assert [f.code for f in report.failures] == [reasons.subset.SUBSET_UNKNOWN_TABLE]


def test_selection_lists_the_union_of_generate_and_none_edges_sorted() -> None:
    config = _config(
        {"z": "mask", "g": "generate", "a": "mask", "b": "mask"},
        [("z", "id", "g", "zid"), ("a", "id", "b", "aid")],
        "a",
    )
    base = subset_request_from_config(config)
    assert base is not None
    (edge,) = build_subset_edges(base.relationships)
    req = _request(config, edge_directions=((edge.edge_id, "none"),), allow_dangling=True)
    tables = {"z": _table("zid"), "a": _table("aid"), "b": _table("aid")}
    sel = select_subset(req, tables, config=JOB_SEED_CONFIG)
    assert sel.excluded_edges == ("a.id -> b.aid", "z.id -> g.zid")


def test_none_direction_child_stays_in_scope_with_no_rows() -> None:
    config = _config({"a": "mask", "b": "mask"}, [("a", "id", "b", "aid")], "a")
    base = subset_request_from_config(config)
    assert base is not None
    (edge,) = build_subset_edges(base.relationships)
    req = _request(config, edge_directions=((edge.edge_id, "none"),), allow_dangling=True)
    sel = select_subset(req, {"a": _table("aid"), "b": _table("aid")}, config=JOB_SEED_CONFIG)
    assert "b" in sel.scope.tables and "b" not in sel.scope.whole
    assert sel.survivors["b"] == frozenset()
    assert sel.counts["b"] == (5, 0)
    assert sel.excluded_edges == (edge.edge_id,)


def test_a_repeated_generate_relationship_is_excluded_once() -> None:
    edge = ("gen", "id", "c", "gid")
    config = _config(
        {"p": "mask", "d": "mask", "gen": "generate", "c": "mask"},
        [("p", "id", "d", "pid"), edge, edge],
        "p",
    )
    req = _request(config)
    assert req.excluded_edges == ("gen.id -> c.gid",)
    tables = {"p": _table("pid"), "d": _table("pid"), "c": _table("gid")}
    sel = select_subset(req, tables, config=JOB_SEED_CONFIG)
    assert sel.excluded_edges == ("gen.id -> c.gid",)


def test_generate_parent_with_a_mask_child_drops_the_edge_and_passes_the_child_whole() -> None:
    config = _config(
        {"p": "mask", "d": "mask", "gen": "generate", "c": "mask"},
        [("p", "id", "d", "pid"), ("gen", "id", "c", "gid")],
        "p",
    )
    req = _request(config)
    assert [e.edge_id for e in build_subset_edges(req.relationships)] == ["p.id -> d.pid"]
    assert req.excluded_edges == ("gen.id -> c.gid",)
    tables = {"p": _table("pid"), "d": _table("pid"), "c": _table("gid")}
    sel = select_subset(req, tables, config=JOB_SEED_CONFIG)
    assert "c" in sel.scope.whole and sel.counts["c"] == (5, 5)
    assert sel.excluded_edges == ("gen.id -> c.gid",)


def _stamp_config(seed: dict[str, Any]) -> dict[str, Any]:
    config = _config({"a": "mask"}, [], "a")
    config["subset"]["seeds"] = [{"table": "a", **seed}]
    return config


def _stamp_table() -> pa.Table:
    import datetime as dt

    stamps = [dt.datetime(2024, 1, d, 10, 0, 0) for d in (1, 2, 3, 4)]
    return pa.table({"id": ["a", "b", "c", "d"], "ts": pa.array(stamps, pa.timestamp("us"))})


def _kept(seed: dict[str, Any]) -> frozenset[int]:
    req = subset_request_from_config(_stamp_config(seed))
    assert req is not None
    sel = select_subset(req, {"a": _stamp_table()}, config=JOB_SEED_CONFIG)
    return sel.survivors["a"]


def test_naive_timestamp_literals_select_rows_of_a_zone_less_column() -> None:
    import datetime as dt

    d3 = dt.datetime(2024, 1, 3, 10, 0, 0)
    d2 = dt.datetime(2024, 1, 2, 10, 0, 0)
    flt = {"mode": "filter", "predicates": [{"column": "ts", "op": "ge", "value": d3}]}
    assert _kept(flt) == frozenset({2, 3})
    eq = {"mode": "filter", "predicates": [{"column": "ts", "op": "eq", "value": d2}]}
    assert _kept(eq) == frozenset({1})
    isin = {"mode": "filter", "predicates": [{"column": "ts", "op": "in", "value": [d2, d3]}]}
    assert _kept(isin) == frozenset({1, 2})
    keys = {"mode": "keys", "key_columns": ["ts"], "keys": [[d2], [d3]]}
    assert _kept(keys) == frozenset({1, 2})


def test_the_fingerprint_of_a_naive_literal_ignores_the_host_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("no tzset on this platform")
    naive = dt.datetime(2024, 1, 3, 10, 0, 0)
    aware = naive.replace(tzinfo=dt.timezone.utc)

    def fp(value: dt.datetime) -> str:
        seed = {"mode": "filter", "predicates": [{"column": "ts", "op": "ge", "value": value}]}
        req = subset_request_from_config(_stamp_config(seed))
        assert req is not None
        return req.fingerprint()

    seen = set()
    for zone in ("UTC", "America/Chicago"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        seen.add(fp(naive))
    monkeypatch.undo()
    time.tzset()
    assert len(seen) == 1
    assert seen != {fp(aware)}


def test_a_non_finite_literal_is_still_refused_with_one_code() -> None:
    from decoy_engine.subset import SubsetConfigError

    seed = {"mode": "filter", "predicates": [{"column": "x", "op": "ge", "value": float("nan")}]}
    with pytest.raises(SubsetConfigError) as caught:
        subset_request_from_config(_stamp_config(seed))
    assert caught.value.code == reasons.config.CONFIG_VALUE_NOT_JSON
    assert "nan" not in f"{caught.value}".lower().replace("finite", "")
    assert caught.value.__cause__ is None and caught.value.__context__ is None

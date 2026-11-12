"""Head seeds, the seed cap and the sample branch of `scope_for`."""

from __future__ import annotations

from dataclasses import replace

import polars as pl
import pyarrow as pa
import pytest

from decoy_engine.subset import (
    FanOutPolicy,
    SampleSpec,
    SeedSpec,
    SubsetRequest,
    sample_request,
    scope_for,
    select_subset,
)
from decoy_engine.subset._api import _seed_spec_public
from decoy_engine.subset._inputs import ResidentInput
from decoy_engine.subset._seed import select_seed_rows
from tests.unit.subset._sample_fixtures import config
from tests.unit.subset.conftest import JOB_SEED, JOB_SEED_CONFIG, rel

FK = ("p", "id", "c", "pid")


class SpyInput(ResidentInput):
    """Records the columns every `key_frame` call asked for."""

    def __init__(self, name: str, table: pa.Table) -> None:
        super().__init__(name, table)
        self.calls: list[tuple[str, ...]] = []

    def key_frame(self, columns) -> pl.DataFrame:  # type: ignore[override]
        self.calls.append(tuple(columns))
        return super().key_frame(columns)


def _tables(ids: list[str]) -> dict[str, pa.Table]:
    return {
        "p": pa.table({"id": ids}),
        "c": pa.table({"cid": [f"k{i}" for i in range(len(ids))], "pid": ids}),
    }


@pytest.mark.parametrize("ids", [["a", "b", "c", "d", "e", "f"], ["f", "a", "e", "b", "d", "c"]])
def test_head_root_takes_row_indices_not_key_order(ids: list[str]) -> None:
    cfg = config({"p": "mask", "c": "mask"}, [FK])
    req = sample_request(cfg, rows=3, root="p")
    tables = _tables(ids)
    spy = SpyInput("p", tables["p"])
    chosen = select_subset(req, {"p": spy, "c": tables["c"]}, config=JOB_SEED_CONFIG)
    assert chosen.survivors["p"] == frozenset({0, 1, 2})
    assert set(spy.calls) == {("id",)}


def test_a_seed_cap_keeps_the_first_rows_by_index_after_the_filter() -> None:
    flags = [False, False] + [True] * 10
    ids = [f"p{i}" for i in range(12)]
    tables = {
        "p": pa.table({"id": ids, "flag": flags}),
        "c": pa.table({"cid": [f"k{i}" for i in range(12)], "pid": ids}),
    }
    seed = {
        "table": "p",
        "mode": "filter",
        "predicates": [{"column": "flag", "op": "eq", "value": True}],
    }
    cfg = config({"p": "mask", "c": "mask"}, [FK], seeds=[seed])
    req = sample_request(cfg, rows=4)
    assert req.seeds[0].cap == 4
    chosen = select_subset(req, tables, config=JOB_SEED_CONFIG)
    assert chosen.survivors["p"] == frozenset({2, 3, 4, 5})


def test_a_head_seed_on_a_linked_table_is_not_head_scope() -> None:
    req = SubsetRequest(
        relationships=(rel("p", ("id",), "c", ("pid",)),),
        seeds=(SeedSpec(table="p", mode="head", count=2),),
        policy=FanOutPolicy(),
        sample=SampleSpec(rows=2, roots=("p",)),
    )
    scope = scope_for(req, ["p", "c", "e"])
    assert scope.tables == frozenset({"p", "c"})
    assert scope.head == frozenset({"e"})
    assert scope.whole == frozenset()


def test_scope_for_refuses_a_head_table_with_an_edge() -> None:
    req = SubsetRequest(
        relationships=(rel("p", ("id",), "c", ("pid",)),),
        seeds=(SeedSpec(table="e", mode="head", count=2),),
        policy=FanOutPolicy(),
        sample=SampleSpec(rows=2, roots=("e",)),
    )
    with pytest.raises(ValueError, match="a sample head table has a declared edge"):
        scope_for(req, ["p", "c", "e"])
    plain = replace(req, sample=None)
    assert scope_for(plain, ["p", "c", "e"]).whole == frozenset({"p", "c"})


def test_an_unknown_seed_mode_does_not_fall_through_to_keys() -> None:
    seed = SeedSpec(table="p", mode="head", count=2)
    object.__setattr__(seed, "mode", "bogus")
    frames = {"p": pl.DataFrame({"__subset_ri": [0, 1, 2], "id": ["a", "b", "c"]})}
    with pytest.raises(ValueError):
        select_seed_rows(seeds=(seed,), key_frames=frames, job_seed=JOB_SEED)


def test_public_seed_spec_carries_counts_and_cap_only() -> None:
    head = SeedSpec(table="p", mode="head", count=3, cap=2)
    assert _seed_spec_public(head) == {"table": "p", "mode": "head", "count": 3, "cap": 2}
    keyed = SeedSpec(table="p", mode="keys", key_columns=("id",), keys=(("secret",),), cap=2)
    shown = _seed_spec_public(keyed)
    assert shown["cap"] == 2 and "secret" not in str(shown)
    assert "cap" not in _seed_spec_public(replace(keyed, cap=None))

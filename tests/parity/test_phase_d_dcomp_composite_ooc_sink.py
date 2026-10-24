"""Phase D / Dcomp-OOC: composite FK on the out-of-core SINK path, on both lanes.

The sink path is where a composite tuple is genuinely keyed across batches: the child never
exists whole, the output schema is fixed before the first batch, and on the reorder lane the
join output is restored to row order through the bounded external sorter. The contract here
is twofold: the two lanes are equal on all nine strict dimensions INCLUDING metadata (route
choice changes timing and memory, never output), and each lane agrees with the oracle exactly
as the resident path does (plan section 4). A forced-reorder run must construct the reorder
joiner and never the batch joiner, so a lane comparison cannot pass vacuously.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_ooc_shapes as o
from tests.parity import _dcomp_shapes as d
from tests.parity import _strict_compare as sc

P, R, W = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN


def _both_lanes(shape: s.Shape, tmp_path: Path, **kw: object) -> tuple[o.OocRun, o.OocRun]:
    batch = o.run_ooc(shape, "sink-batch", tmp_path=tmp_path / "batch", **kw)  # type: ignore[arg-type]
    reorder = o.run_ooc(shape, "sink-reorder", tmp_path=tmp_path / "reorder", **kw)  # type: ignore[arg-type]
    assert (batch.batch_joiners > 0, batch.reorder_joiners) == (True, 0)
    assert (reorder.batch_joiners, reorder.reorder_joiners > 0) == (0, True)
    return batch, reorder


def _lanes_equal(batch: o.OocRun, reorder: o.OocRun) -> None:
    for table in batch.outputs:
        assert reorder.outputs[table].equals(batch.outputs[table], check_metadata=True), table
        assert sc.compare_strict(batch.outputs[table], reorder.outputs[table]) == d.ALL_TRUE, table
    assert o.warning_keys(reorder.warnings) == o.warning_keys(batch.warnings)


def _lanes_agree_with_the_oracle(shape: s.Shape, tmp_path: Path, **kw: object) -> None:
    oracle = s.run_full(shape)
    batch, reorder = _both_lanes(shape, tmp_path, **kw)
    _lanes_equal(batch, reorder)
    for run in (batch, reorder):
        assert o.strict(oracle, run) == {t: o.METADATA_ONLY for t in oracle.outputs}
        assert o.sorted_warnings(run.warnings) == o.sorted_warnings(oracle.warnings)


@pytest.mark.parametrize("policy", [P, R, W])
def test_o1_canonical_group_on_both_lanes(policy: OrphanPolicy, tmp_path: Path) -> None:
    _lanes_agree_with_the_oracle(s.composite_shape(policy), tmp_path)


@pytest.mark.parametrize("policy", [P, R])
def test_o4_null_bearing_integer_component_on_both_lanes(
    policy: OrphanPolicy, tmp_path: Path
) -> None:
    shape = d.int_component_shape(
        policy, parent_n=[1, None, 3], child_k=["a", "c", "q", "a"], child_n=[1, 3, 9, None]
    )
    _lanes_agree_with_the_oracle(shape, tmp_path, batch_rows=2)


def test_o11_golden_composite_key_on_both_lanes(tmp_path: Path) -> None:
    _lanes_agree_with_the_oracle(d.golden_composite_shape(W, orphan=True), tmp_path, batch_rows=3)


@pytest.mark.parametrize("policy", [P, R])
def test_o13_partial_null_tuples_straddling_batch_boundaries(
    policy: OrphanPolicy, tmp_path: Path
) -> None:
    # Five child rows at two rows per batch: the partial-null tuples (rows 1 and 3) sit on
    # different batches and the fully-null one shares a batch with a matched row.
    child = d.key_table(["a", None, "q", None, "b"], ["x", "y", "w", None, "y"])
    shape = d.composite_tables(d.key_table(["a", "b", "c"], ["x", "y", "z"]), child, policy)
    _lanes_agree_with_the_oracle(shape, tmp_path, batch_rows=2)


@pytest.mark.parametrize("child", ["empty", "all-null"])
@pytest.mark.parametrize("policy", [P, R])
def test_o12_empty_and_all_null_children_take_the_fixed_string_type_on_the_sink_path(
    policy: OrphanPolicy, child: str, tmp_path: Path
) -> None:
    """The documented `_batch_join` divergence: a sink writes one schema, so an FK column with
    no value to infer from carries the fixed type where the resident path and the oracle leave
    it null-typed. Values (all null) agree; the lanes agree with each other."""
    rows = ([], []) if child == "empty" else ([None, None], [None, None])
    shape = d.composite_tables(d.key_table(["a", "b"], ["x", "y"]), d.key_table(*rows), policy)
    oracle = s.run_full(shape)
    batch, reorder = _both_lanes(shape, tmp_path)
    _lanes_equal(batch, reorder)
    assert sc.schema_signature(oracle.outputs["C"]) == "k:null,j:null"
    assert sc.schema_signature(batch.outputs["C"]) == "k:string,j:string"
    assert batch.outputs["C"].to_pydict() == oracle.outputs["C"].to_pydict()
    assert o.strict(oracle, batch)["P"] == o.METADATA_ONLY

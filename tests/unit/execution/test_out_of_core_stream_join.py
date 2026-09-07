"""P4-A.3 Task A: the relanded streaming-join scaffolding still works.

`_stream_join.py` / `_payload_store.py` were relanded from
`origin/fix/ooc-b-memory-streaming-join` (adapted to this branch's current
`_join.py`/`_relation.py`/`_batch_join.py` APIs -- a mechanical port, no
behavior change). This is the tripwire test #9 from the P4-A.3 plan: a smoke
test over the relanded scaffolding on a single edge, so the reland cannot
silently rot before its consumers (Task 2's unordered join, Task 3's
`run_ordered_join`) land. Full byte-parity against the oracle is Task C.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.execution.out_of_core._stream_join import JoinRowCursor, StreamFkJoiner
from decoy_engine.relationships._graph import OrphanPolicy

from ._ooc_fixtures import remap_edge_fixture, simple_edge_fixture


def _resolve_whole_child(fx, temp_dir) -> tuple[pa.Table, int]:
    """Drive one edge end to end: stage_keys -> iter_join_rows -> resolve_batch."""
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=temp_dir / "join",
        remap_seeds=fx.remap_seeds,
        job_seed=fx.job_seed,
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        n = fx.child.num_rows
        cursor = JoinRowCursor(joiner.iter_join_rows(64), join_columns=fx.edge.child_columns)
        raw = cursor.take(n, 0)
        cursor.assert_exhausted()
        # `iter_join_rows` (the legacy shim) keeps the FULL projection, so the
        # one batch carries every column `resolve_batch` recombines: pass it as
        # both the slim and raw slice.
        fk_arrays = joiner.resolve_batch(raw, raw)
        assert fk_arrays[0].type == joiner.output_types[0]
        result = fx.child.set_column(
            fx.child.schema.get_field_index(fx.edge.child_columns[0]),
            fx.edge.child_columns[0],
            fk_arrays[0],
        )
        return result, joiner.orphan_total


def test_stream_join_scaffolding_smoke(tmp_path) -> None:
    fx = simple_edge_fixture(tmp_path / "simple")
    result, orphans = _resolve_whole_child(fx, tmp_path / "simple")

    assert result.num_rows == fx.child.num_rows
    # PRESERVE keeps the orphan's own source key; parent is a passthrough
    # identity mask, so a matched row's resolved value equals its source key.
    assert result.column("key").to_pylist() == ["c1", "c2", "orphan1", "c1", "c2"]
    assert orphans == 1


def test_stream_join_scaffolding_smoke_remap_edge(tmp_path) -> None:
    fx = remap_edge_fixture(tmp_path / "remap")
    assert fx.edge.orphan_policy is OrphanPolicy.REMAP
    result, orphans = _resolve_whole_child(fx, tmp_path / "remap")

    assert result.num_rows == fx.child.num_rows
    values = result.column("customer_id").to_pylist()
    # Matched rows resolve to the parent's hash-masked value (never the raw
    # source key); the one orphan ("missing") is re-minted through the same
    # strategy, so it is also masked, not preserved raw.
    assert "missing" not in values
    assert "c1" not in values
    assert "c2" not in values
    assert orphans == 1

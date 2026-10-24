"""Unit coverage of the multi-parent merge (`out_of_core/_multi_parent.py`).

`merge_parent_relations` folds N deduplicated parent relations into one first-hit-wins
relation keyed by edge rank; `resolve_incoming` groups a table's incoming edges by child
tuple and binds each multi-parent group's first edge to the merged relation. The parity
contract against the oracle is `tests/parity/test_phase_d_dmp_multi_parent_ooc.py`; this file
pins the mechanism.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import build_parent_key_relation
from decoy_engine.execution.out_of_core._multi_parent import (
    merge_parent_relations,
    resolve_incoming,
)
from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

_SEED = b"\x00" * 8


def _seed(strategy: str = "passthrough", namespace: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=(),
        coherent_with=(),
    )


def _plan(tables: dict[str, dict[str, ColumnSeed]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=tuple(
                (t, TableSeed(per_column=tuple(cols.items()), per_group=()))
                for t, cols in tables.items()
            ),
        )
    )


def _edge(parent: str, child_cols: tuple[str, ...] = ("ref",)) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=tuple(f"k{i}" if i else "k" for i in range(len(child_cols))),
        child_table="X",
        child_columns=child_cols,
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _relation(
    tmp_path: Path, parent: str, keys: list[Any], *, dtype: pa.DataType | None = None
) -> ParentKeyRelation:
    """A passthrough relation: masked value == key, so precedence is visible as a table name
    suffix only through which parent's relation a key came from."""
    plan = _plan({parent: {"k": _seed()}})
    table = pa.table({"k": pa.array(keys, dtype or pa.string())})
    return build_parent_key_relation(
        plan=plan, parent=table, edge=_edge(parent), temp_dir=tmp_path / parent
    )


def _relation_tagged(tmp_path: Path, parent: str, keys: list[str]) -> ParentKeyRelation:
    """A relation whose masked value carries the parent's name, so the winner is readable."""
    path = tmp_path / parent / "rel.parquet"
    path.parent.mkdir(parents=True)
    from decoy_engine.execution._fk_keys import fk_join_key_tuple

    pq.write_table(
        pa.table(
            {
                "__decoy_fk_join_key": pa.array(
                    [fk_join_key_tuple((k,)) for k in keys], pa.string()
                ),
                "__decoy_masked_key": pa.array([f"{parent}:{k}" for k in keys], pa.string()),
            }
        ),
        path,
    )
    return ParentKeyRelation(path=path, max_sort_payload_row_bytes=len(parent) + 10)


def _rows(relation: ParentKeyRelation) -> dict[str, str]:
    table = pq.read_table(relation.path)
    return dict(zip(table.column(0).to_pylist(), table.column(1).to_pylist(), strict=True))


def test_merge_keeps_one_row_per_key_and_the_lowest_rank_wins(tmp_path: Path) -> None:
    first = _relation_tagged(tmp_path, "P1", ["a", "shared"])
    second = _relation_tagged(tmp_path, "P2", ["b", "shared", "both23"])
    third = _relation_tagged(tmp_path, "P3", ["c", "shared", "both23"])
    merged = merge_parent_relations(
        (first, second, third), temp_dir=tmp_path / "m", memory_limit=None
    )
    values = set(_rows(merged).values())
    assert values == {"P1:a", "P1:shared", "P2:b", "P2:both23", "P3:c"}
    assert merged.masked_key_columns == first.masked_key_columns
    assert merged.max_sort_payload_row_bytes == max(
        r.max_sort_payload_row_bytes for r in (first, second, third)
    )
    assert [p.name for p in merged.path.parent.glob("*.parquet")] == [
        merged.path.name
    ]  # scratch removed


def test_merge_of_one_relation_is_the_relation_itself(tmp_path: Path) -> None:
    only = _relation_tagged(tmp_path, "P1", ["a"])
    assert merge_parent_relations((only,), temp_dir=tmp_path / "m", memory_limit=None) is only


def test_merge_refuses_differing_masked_key_types(tmp_path: Path) -> None:
    ints = _relation(tmp_path, "P1", [1, 2], dtype=pa.int64())
    floats = _relation(tmp_path, "P2", [2.0, 3.0], dtype=pa.float64())
    with pytest.raises(ExecutionError) as exc:
        merge_parent_relations((ints, floats), temp_dir=tmp_path / "m", memory_limit=None)
    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"
    assert "int64" in str(exc.value) and "double" in str(exc.value)  # the types, never a value
    assert "1" not in str(exc.value).replace("int64", "").replace("sequential", "")
    assert not (tmp_path / "m").exists() or not list((tmp_path / "m").rglob("*.parquet"))


def test_resolve_incoming_passes_singletons_through_and_groups_by_child_tuple(
    tmp_path: Path,
) -> None:
    a1, a2 = _relation_tagged(tmp_path, "A1", ["x"]), _relation_tagged(tmp_path, "A2", ["y"])
    b = _relation_tagged(tmp_path, "B", ["z"])
    e_a1, e_a2, e_b = _edge("A1", ("ref",)), _edge("A2", ("ref",)), _edge("B", ("other",))
    relations = {e_a1: a1, e_a2: a2, e_b: b}
    edges = resolve_incoming(
        (e_a1, e_b, e_a2), relations, temp_dir=tmp_path / "m", memory_limit=None
    )
    assert edges == (e_a1, e_b)  # the first edge represents its group; relative order kept
    assert relations[e_b] is b  # a singleton is untouched
    assert set(_rows(relations[e_a1]).values()) == {"A1:x", "A2:y"}  # rebound to the merge
    assert e_a2 not in relations  # the other member is gone from the mapping ...
    assert not a1.path.exists() and not a2.path.exists()  # ... and both member files from disk
    assert b.path.exists()


def test_resolve_incoming_refuses_edges_that_disagree_on_the_orphan_policy(tmp_path: Path) -> None:
    """The planner's `orphan_policy_conflict` rule, re-checked where a hand-built graph could
    bypass it: nothing is merged, nothing is unlinked, and the code is the planner's."""
    first = _edge("P1")
    second = RelationshipEdge(
        **{**first.__dict__, "parent_table": "P2", "orphan_policy": OrphanPolicy.REMAP}
    )
    relations = {
        first: _relation_tagged(tmp_path, "P1", ["a"]),
        second: _relation_tagged(tmp_path, "P2", ["b"]),
    }
    with pytest.raises(ExecutionError) as exc:
        resolve_incoming(
            [first, second], relations, temp_dir=tmp_path / "merged", memory_limit=None
        )
    assert exc.value.code == "orphan_policy_conflict"
    assert "['preserve', 'remap']" in str(exc.value) and "X.('ref',)" in str(exc.value)
    assert set(relations) == {first, second}
    assert all(r.path.exists() for r in relations.values())
    assert not (tmp_path / "merged").exists()


def test_resolve_incoming_with_no_multi_parent_group_builds_nothing(tmp_path: Path) -> None:
    b = _relation_tagged(tmp_path, "B", ["z"])
    e_b = _edge("B")
    relations = {e_b: b}
    assert resolve_incoming((e_b,), relations, temp_dir=tmp_path / "m", memory_limit=None) == (e_b,)
    assert relations == {e_b: b} and b.path.exists()
    assert not (tmp_path / "m").exists()

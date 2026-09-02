"""P4-A slice 1 acceptance tests #1-#4: `_build_relation`'s split dedup.

Companion to `tests/perf/test_out_of_core_relation_dedup_memory.py` (#5, the
measured-RSS boundedness proof). These four cover the query-shape and
correctness side: the split issues two structurally distinct physical plans
(#1), the split's VALUES match the exact pre-split combined query it replaced
(#2, the primary oracle), a successful build leaves no scratch behind (#3),
and a mid-build or cleanup-time failure still leaves both scratch files
independently handled and never a partial output (#4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._fk_keys import fk_join_key_tuple
from decoy_engine.execution.out_of_core import _relation as relation_mod
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.execution.out_of_core._join import _sql_string
from decoy_engine.execution.out_of_core._relation import (
    _build_relation,
    _column_tuple_slug,
    _relation_staging_batches,
    _staging_schema,
    build_parent_key_relation,
)
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

# Reuse the existing suite's fixtures rather than re-deriving them: `_plan`
# pairs a "customers"/"cust" hash column with `_edge`'s single-column FK, the
# same shape the parent-relation regression suite already exercises.
from tests.unit.execution.test_out_of_core_relation import _edge, _plan

# ---------------------------------------------------------------------------
# Shared plumbing: a connection-wrapping proxy (test #1, #4) and a manual
# staged-table + pre-split-oracle harness (test #2).
# ---------------------------------------------------------------------------


def _label_for_sql(sql: str) -> str | None:
    """Which of `_build_relation`'s three `COPY` statements `sql` is.

    The winners statement has a `GROUP BY` and no `JOIN`; the join-back
    statement has a `JOIN` and no `GROUP BY`; the initial staging `COPY` has
    neither and is not labeled (nothing in these tests targets it).
    """
    has_group_by = "GROUP BY" in sql
    has_join = " JOIN " in sql
    if has_group_by and not has_join:
        return "winners"
    if has_join and not has_group_by:
        return "join_back"
    return None


class _ExecuteSpy:
    """Wraps a live DuckDB connection to capture `EXPLAIN (FORMAT JSON)` for
    the winners and join-back statements, then runs the real statement
    unchanged. `duckdb.DuckDBPyConnection.execute` is a read-only attribute
    on the C extension type (`conn.execute = ...` raises `AttributeError:
    ... attribute 'execute' is read-only`), so the connection itself is
    wrapped rather than the method reassigned.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.plans: dict[str, str] = {}

    def execute(self, sql: str, params: Any = None) -> Any:
        label = _label_for_sql(sql)
        if label is not None:
            explain_sql = "EXPLAIN (FORMAT JSON) " + sql
            result = (
                self._conn.execute(explain_sql, params)
                if params is not None
                else self._conn.execute(explain_sql)
            )
            self.plans[label] = result.fetchall()[0][1]
        return self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _InjectedFailureError(RuntimeError):
    """Marker exception for test #4's failure injection, distinguishable from
    any real DuckDB or filesystem error the build might otherwise raise."""


class _RaisingExecuteProxy:
    """Wraps a live DuckDB connection, raising `_InjectedFailureError` the moment
    a statement matching `fail_label` is about to run, forwarding every
    other call unchanged."""

    def __init__(self, conn: Any, fail_label: str) -> None:
        self._conn = conn
        self._fail_label = fail_label

    def execute(self, sql: str, params: Any = None) -> Any:
        if _label_for_sql(sql) == self._fail_label:
            raise _InjectedFailureError(f"injected failure at {self._fail_label!r}")
        return self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _operator_names(explain_json: str) -> list[str]:
    """Flatten an `EXPLAIN (FORMAT JSON)` payload to its operator names."""
    parsed = json.loads(explain_json)
    root = parsed[0] if isinstance(parsed, list) else parsed
    names: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        names.append(node["name"])
        for child in node.get("children", []):
            walk(child)

    walk(root)
    return names


def _expected_paths(temp_dir: Path, edge: RelationshipEdge) -> tuple[Path, Path, Path]:
    """The staged/winners/output paths `_build_relation` derives internally,
    recomputed here (same formula) so tests can assert on them directly."""
    slug = _column_tuple_slug(edge.parent_columns)
    duckdb_dir = temp_dir / "duckdb"
    staged = duckdb_dir / f"{edge.parent_table}_{slug}_key_staged.parquet"
    winners = duckdb_dir / f"{edge.parent_table}_{slug}_key_winners.parquet"
    out = temp_dir / f"{edge.parent_table}_{slug}_key_relation.parquet"
    return staged, winners, out


def _masked_columns_for(edge: RelationshipEdge) -> tuple[str, ...]:
    """`_build_relation`'s own masked-column naming: one per join-key column."""
    return tuple(
        "__decoy_masked_key" if idx == 0 else f"__decoy_masked_key_{idx}"
        for idx in range(len(edge.parent_columns))
    )


def _aligned_masked_fn(masked_table: pa.Table):
    """A `masked_batch_fn` reading a row-aligned resident table positionally
    (column order, not name), matching `build_parent_key_relation_aligned`'s
    own resident-table masked callback shape."""

    def fn(start: int, key_arrays: list[pa.Array], keep_mask: pa.BooleanArray) -> list[pa.Array]:
        length = len(key_arrays[0])
        return [
            masked_table.column(i).slice(start, length).combine_chunks().filter(keep_mask)
            for i in range(masked_table.num_columns)
        ]

    return fn


def _materialize_staged_table(
    *,
    source_parent: pa.Table,
    edge: RelationshipEdge,
    masked_table: pa.Table,
    masked_types: tuple[pa.DataType, ...],
    batch_rows: int = 2,
) -> tuple[pa.Table, tuple[str, ...]]:
    """The staged (row_nr, join_key, masked...) rows `_build_relation` would
    stage for this input -- built by calling the SAME staging generator
    `_build_relation` uses internally, with the same arguments, so it is
    guaranteed byte-identical to what a real build sees (this function does
    not reimplement any dedup logic, only the deterministic pre-dedup
    staging step, which is shared, out-of-scope-for-this-slice code)."""
    masked_columns = _masked_columns_for(edge)
    schema = _staging_schema(masked_types, masked_columns)
    batches = list(
        _relation_staging_batches(
            source_parent=source_parent,
            parent_columns=edge.parent_columns,
            masked_columns=masked_columns,
            masked_types=masked_types,
            masked_batch_fn=_aligned_masked_fn(masked_table),
            batch_rows=batch_rows,
        )
    )
    table = pa.Table.from_batches(batches, schema=schema) if batches else schema.empty_table()
    return table, masked_columns


def _run_old_combined_oracle(
    staged_table: pa.Table, masked_columns: tuple[str, ...], work_dir: Path
) -> pa.Table:
    """The PRIMARY oracle for test #2: the exact single-query combined dedup
    `_build_relation` replaced (the winners aggregate as an inline join
    subquery), run against the given staged rows in its own connection.
    Kept verbatim against `_relation.py`'s git history as of 2026-09-02, the
    commit that split it -- not a re-derivation, so a drift between this
    string and the real removed query would be a real oracle bug, not a
    production one.
    """
    duckdb_dir = work_dir / "oracle_duckdb"
    conn = connect_duckdb(temp_dir=duckdb_dir)
    try:
        staged_path = duckdb_dir / "staged.parquet"
        conn.register("staged_keys", staged_table)
        conn.execute(f"COPY staged_keys TO {_sql_string(str(staged_path))} (FORMAT PARQUET)")
        staged_sql = f"read_parquet({_sql_string(str(staged_path))})"
        masked_select = ", ".join(f"s.{c} AS {c}" for c in masked_columns)
        out_path = duckdb_dir / "old_out.parquet"
        conn.execute(
            f"""
            COPY (
                SELECT s.__decoy_fk_join_key, {masked_select}
                FROM {staged_sql} s
                JOIN (
                    SELECT __decoy_fk_join_key,
                           max(__decoy_row_nr) AS __decoy_win_row_nr
                    FROM {staged_sql}
                    GROUP BY __decoy_fk_join_key
                ) w
                  ON s.__decoy_fk_join_key = w.__decoy_fk_join_key
                 AND s.__decoy_row_nr = w.__decoy_win_row_nr
            ) TO {_sql_string(str(out_path))} (FORMAT PARQUET)
            """
        )
        return pq.read_table(out_path)
    finally:
        conn.close()


def _rows_by_key(table: pa.Table, masked_columns: tuple[str, ...]) -> dict[str, tuple[Any, ...]]:
    keys = table.column("__decoy_fk_join_key").to_pylist()
    assert len(keys) == len(set(keys)), f"duplicate join key in dedup output: {keys}"
    cols = [table.column(c).to_pylist() for c in masked_columns]
    return {key: tuple(col[i] for col in cols) for i, key in enumerate(keys)}


def _assert_split_matches_old_combined(
    *,
    parent: pa.Table,
    masked_table: pa.Table,
    edge: RelationshipEdge,
    masked_types: tuple[pa.DataType, ...],
    tmp_path: Path,
) -> tuple[pa.Table, pa.Table, tuple[str, ...]]:
    """Runs the real split (`_build_relation`, production code) and the old
    combined oracle against the SAME staged input, and asserts their outputs
    are value-identical as a key -> typed-row mapping, row order ignored.
    Returns (new_out, old_out, masked_columns) for a case's own extra checks.
    """
    staged_table, masked_columns = _materialize_staged_table(
        source_parent=parent, edge=edge, masked_table=masked_table, masked_types=masked_types
    )

    relation = _build_relation(
        source_parent=parent,
        edge=edge,
        temp_dir=tmp_path / "new",
        memory_limit=None,
        batch_rows=2,
        masked_batch_fn=_aligned_masked_fn(masked_table),
        masked_types=masked_types,
    )
    new_out = pq.read_table(relation.path)
    old_out = _run_old_combined_oracle(staged_table, masked_columns, tmp_path / "old")

    new_rows = _rows_by_key(new_out, masked_columns)
    old_rows = _rows_by_key(old_out, masked_columns)
    assert new_rows == old_rows, (
        f"split dedup diverged from the pre-split oracle:\n{new_rows}\nvs\n{old_rows}"
    )
    return new_out, old_out, masked_columns


# ---------------------------------------------------------------------------
# #1 -- two separate physical plans.
# ---------------------------------------------------------------------------


def test_dedup_runs_as_two_separate_physical_plans(tmp_path, monkeypatch) -> None:
    real_connect = relation_mod.connect_duckdb
    spies: list[_ExecuteSpy] = []

    def fake_connect(*, temp_dir, memory_limit=None):
        conn = real_connect(temp_dir=temp_dir, memory_limit=memory_limit)
        spy = _ExecuteSpy(conn)
        spies.append(spy)
        return spy

    monkeypatch.setattr(relation_mod, "connect_duckdb", fake_connect)

    parent = pa.table({"customer_id": ["c1", "c2", "c1"]})
    build_parent_key_relation(plan=_plan(), parent=parent, edge=_edge(), temp_dir=tmp_path)

    assert len(spies) == 1
    plans = spies[0].plans
    assert set(plans) == {"winners", "join_back"}, plans

    winners_ops = _operator_names(plans["winners"])
    join_back_ops = _operator_names(plans["join_back"])
    assert any("GROUP_BY" in name for name in winners_ops), winners_ops
    assert not any("JOIN" in name for name in winners_ops), winners_ops
    assert any("JOIN" in name for name in join_back_ops), join_back_ops
    assert not any("GROUP_BY" in name for name in join_back_ops), join_back_ops


# ---------------------------------------------------------------------------
# #2 -- value-identical to the pre-split combined query.
# ---------------------------------------------------------------------------


def test_split_dedup_is_value_identical_plain_last_write_wins(tmp_path) -> None:
    parent = pa.table({"customer_id": ["c1", "c2", "c1"]})
    masked = pa.table({"masked": ["m1-early", "m2", "m1-late"]})
    edge = _edge()

    new_out, _old_out, masked_columns = _assert_split_matches_old_combined(
        parent=parent,
        masked_table=masked,
        edge=edge,
        masked_types=(pa.string(),),
        tmp_path=tmp_path,
    )

    # Secondary sanity check (not the primary oracle): a plain Python
    # last-write-wins dict, matching the pandas oracle's own dict-keyed
    # parent_map construction.
    expected: dict[str, str] = {}
    for cust, val in zip(["c1", "c2", "c1"], ["m1-early", "m2", "m1-late"], strict=True):
        expected[cust] = val
    rows = _rows_by_key(new_out, masked_columns)
    assert rows == {
        fk_join_key_tuple(("c1",)): (expected["c1"],),
        fk_join_key_tuple(("c2",)): (expected["c2"],),
    }


def test_split_dedup_is_value_identical_empty_input(tmp_path) -> None:
    parent = pa.table({"customer_id": pa.array([], type=pa.string())})
    masked = pa.table({"masked": pa.array([], type=pa.string())})
    edge = _edge()

    new_out, old_out, _masked_columns = _assert_split_matches_old_combined(
        parent=parent,
        masked_table=masked,
        edge=edge,
        masked_types=(pa.string(),),
        tmp_path=tmp_path,
    )
    assert new_out.num_rows == 0
    assert old_out.num_rows == 0


def test_split_dedup_is_value_identical_null_source_fk_row(tmp_path) -> None:
    """A null-source-FK parent row never reaches the staged dedup at all (the
    shared, out-of-scope staging filter in `_relation_staging_batches` drops
    any row with a null key component before either query form sees it) --
    so the invariant this proves is that BOTH forms exclude it identically,
    not that a `NULL_FK_KEY` sentinel string round-trips through a live row
    (no live row can carry it: see this test module's docstring reasoning
    and the plan's own audited non-NULL invariant)."""
    parent = pa.table({"customer_id": ["c1", None, "c2"]})
    masked = pa.table({"masked": ["m1", "m-null-row", "m2"]})
    edge = _edge()

    new_out, _old_out, masked_columns = _assert_split_matches_old_combined(
        parent=parent,
        masked_table=masked,
        edge=edge,
        masked_types=(pa.string(),),
        tmp_path=tmp_path,
    )
    rows = _rows_by_key(new_out, masked_columns)
    assert rows == {
        fk_join_key_tuple(("c1",)): ("m1",),
        fk_join_key_tuple(("c2",)): ("m2",),
    }


def _composite_edge() -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="accounts",
        parent_columns=("country", "account_id"),
        child_table="transactions",
        child_columns=("country", "account_id"),
        namespace="acct_rel",
        orphan_policy=OrphanPolicy.FAIL,
    )


def test_split_dedup_is_value_identical_composite_join_key(tmp_path) -> None:
    # `_build_relation` mints exactly one masked column PER join-key column
    # (each parent column carries its own seed/strategy), so a 2-column
    # composite key always means 2 masked columns -- there is no shape where
    # a composite key produces a single masked column.
    parent = pa.table({"country": ["US", "CA", "US"], "account_id": ["a1", "a2", "a1"]})
    masked = pa.table(
        {
            "masked_country": ["mUS-early", "mCA", "mUS-late"],
            "masked_account": ["ma1-early", "ma2", "ma1-late"],
        }
    )
    edge = _composite_edge()

    new_out, _old_out, masked_columns = _assert_split_matches_old_combined(
        parent=parent,
        masked_table=masked,
        edge=edge,
        masked_types=(pa.string(), pa.string()),
        tmp_path=tmp_path,
    )
    rows = _rows_by_key(new_out, masked_columns)
    assert rows == {
        fk_join_key_tuple(("US", "a1")): ("mUS-late", "ma1-late"),
        fk_join_key_tuple(("CA", "a2")): ("mCA", "ma2"),
    }


def test_split_dedup_is_value_identical_multi_masked_columns_with_nulls(tmp_path) -> None:
    parent = pa.table({"country": ["US", "CA", "US"], "account_id": ["a1", "a2", "a1"]})
    masked = pa.table(
        {
            "m1": pa.array(["x1-early", None, "x1-late"], type=pa.string()),
            "m2": pa.array([None, "y2", "y1-late"], type=pa.string()),
        }
    )
    edge = _composite_edge()

    new_out, _old_out, masked_columns = _assert_split_matches_old_combined(
        parent=parent,
        masked_table=masked,
        edge=edge,
        masked_types=(pa.string(), pa.string()),
        tmp_path=tmp_path,
    )
    rows = _rows_by_key(new_out, masked_columns)
    assert rows == {
        fk_join_key_tuple(("US", "a1")): ("x1-late", "y1-late"),
        fk_join_key_tuple(("CA", "a2")): (None, "y2"),
    }


# ---------------------------------------------------------------------------
# #3 -- scratch cleaned up on success.
# ---------------------------------------------------------------------------


def test_split_dedup_cleans_scratch(tmp_path) -> None:
    parent = pa.table({"customer_id": ["c1", "c2", "c1"]})
    relation = build_parent_key_relation(
        plan=_plan(), parent=parent, edge=_edge(), temp_dir=tmp_path
    )

    assert relation.path.exists()
    leftover = list(tmp_path.rglob("*_staged.parquet")) + list(tmp_path.rglob("*_winners.parquet"))
    assert leftover == []


# ---------------------------------------------------------------------------
# #4 -- fail-closed cleanup, independent guards.
# ---------------------------------------------------------------------------


def test_split_dedup_cleanup_is_fail_closed(tmp_path, monkeypatch) -> None:
    plan = _plan()
    edge = _edge()
    real_connect = relation_mod.connect_duckdb

    def _patched(fail_label: str):
        return lambda *, temp_dir, memory_limit=None: _RaisingExecuteProxy(
            real_connect(temp_dir=temp_dir, memory_limit=memory_limit), fail_label
        )

    # (i) the winners step raises: only the staged copy has landed by then;
    # both scratch files (staged: really there; winners: never written) and
    # any output must be gone afterward.
    work_i = tmp_path / "case_i"
    staged_i, winners_i, out_i = _expected_paths(work_i, edge)
    monkeypatch.setattr(relation_mod, "connect_duckdb", _patched("winners"))
    with pytest.raises(_InjectedFailureError):
        build_parent_key_relation(
            plan=plan,
            parent=pa.table({"customer_id": ["c1", "c2", "c1"]}),
            edge=edge,
            temp_dir=work_i,
        )
    assert not staged_i.exists()
    assert not winners_i.exists()
    assert not out_i.exists()

    # (ii) the join-back step raises: staged AND winners both genuinely
    # exist by then (steps 1 and 2 completed); both must still be cleaned.
    work_ii = tmp_path / "case_ii"
    staged_ii, winners_ii, out_ii = _expected_paths(work_ii, edge)
    monkeypatch.setattr(relation_mod, "connect_duckdb", _patched("join_back"))
    with pytest.raises(_InjectedFailureError):
        build_parent_key_relation(
            plan=plan,
            parent=pa.table({"customer_id": ["c1", "c2", "c1"]}),
            edge=edge,
            temp_dir=work_ii,
        )
    assert not staged_ii.exists()
    assert not winners_ii.exists()
    assert not out_ii.exists()

    # (iii) the build itself SUCCEEDS; only the FIRST scratch unlink (staged)
    # is injected to raise. The other scratch (winners) must still be
    # cleaned (independent guards), the injected-to-fail file is expected to
    # remain (its own unlink was forced to fail), and the injected error must
    # not mask the build's own outcome: the real, correct output stays on
    # disk rather than being treated as if the build itself had failed.
    monkeypatch.setattr(relation_mod, "connect_duckdb", real_connect)
    work_iii = tmp_path / "case_iii"
    staged_iii, winners_iii, out_iii = _expected_paths(work_iii, edge)
    real_unlink = Path.unlink

    def failing_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == staged_iii:
            raise OSError("injected: staged unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(OSError, match="injected"):
        build_parent_key_relation(
            plan=plan,
            parent=pa.table({"customer_id": ["c1", "c2", "c1"]}),
            edge=edge,
            temp_dir=work_iii,
        )
    assert staged_iii.exists()  # its own unlink was forced to fail
    assert not winners_iii.exists()  # the OTHER scratch is still cleaned
    assert out_iii.exists()  # the successful build's output is not masked
    result = pq.read_table(out_iii)
    assert set(result.column("__decoy_fk_join_key").to_pylist()) == {
        fk_join_key_tuple(("c1",)),
        fk_join_key_tuple(("c2",)),
    }

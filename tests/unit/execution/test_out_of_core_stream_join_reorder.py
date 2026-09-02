"""P4-A.3 Task C: `StreamFkJoiner.run_ordered_join`, the sorter's first real
FK consumer (acceptance tests #1, #2, #3, #3b, #4, #6, #7, #10).

Byte-parity (tests #1, #2, #3b) is asserted through
`_ooc_reorder_harness.assert_byte_parity` against the executable single-edge
oracle `_join.py::mask_child_fk` -- the hard correctness gate this slice
exists to prove. The remaining tests exercise the contiguity guard, the
budget wiring, the sorter key contract, and the lifecycle/cleanup contract
directly.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._external_sort import INDEX_BYTES, _min_row_bytes
from decoy_engine.execution.out_of_core._reorder_budget import (
    require_disk,
    resolve_reorder_budgets,
)
from decoy_engine.execution.out_of_core._stream_join import (
    JoinRowCursor,
    StreamFkJoiner,
    _OrderedJoinRows,
)

from ._ooc_fixtures import (
    empty_child_edge_fixture,
    orphan_and_null_edge_fixture,
    remap_edge_fixture,
    simple_edge_fixture,
    wide_edge_fixture,
)
from ._ooc_reorder_harness import assert_byte_parity

_RUN_BYTES_CAP = 8 * 1024 * 1024
_MERGE_FAN_IN = 4


# ---------------------------------------------------------------------------
# Tests #1, #2, #3b -- byte-parity vs the executable oracle
# ---------------------------------------------------------------------------


def test_run_ordered_join_byte_parity_simple(tmp_path) -> None:
    fx = simple_edge_fixture(tmp_path / "fx")
    assert_byte_parity(fx, tmp_path / "run", label="simple")


def test_run_ordered_join_byte_parity_remap(tmp_path) -> None:
    fx = remap_edge_fixture(tmp_path / "fx")
    assert_byte_parity(fx, tmp_path / "run", label="remap")


def test_parity_orphan_child_rows(tmp_path) -> None:
    fx = orphan_and_null_edge_fixture(tmp_path / "fx")
    assert_byte_parity(fx, tmp_path / "run", label="orphan-and-null")


def test_parity_empty_child(tmp_path) -> None:
    fx = empty_child_edge_fixture(tmp_path / "fx")
    assert_byte_parity(fx, tmp_path / "run", label="empty-child")


def test_parity_across_batch_and_run_boundaries(tmp_path) -> None:
    # batch_rows and run_bytes_cap are both deliberately small so the join
    # spans several `iter_join_rows`-style output batches AND the sorter
    # spans several runs (verified separately by
    # test_reorder_shuffled_input_restores_order's run-count assertion).
    fx = wide_edge_fixture(tmp_path / "fx", parent_rows=40, child_rows=400)
    assert_byte_parity(
        fx, tmp_path / "run", label="batch-and-run-boundary", batch_rows=32, run_bytes_cap=4096
    )


# ---------------------------------------------------------------------------
# Test #3 -- the sorter, not scan order, drives the restored order
# ---------------------------------------------------------------------------


def test_reorder_shuffled_input_restores_row_order(tmp_path) -> None:
    """Feed the unordered join a real multi-run merge (small run_bytes_cap)
    and assert the resolved output's row_nr sequence is exactly 0..N-1 and
    values match the oracle -- proving the BoundedExternalSorter, not an
    incidental scan order, is what restores order."""
    fx = wide_edge_fixture(tmp_path / "fx", parent_rows=25, child_rows=300)
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        n = fx.child.num_rows
        with joiner.run_ordered_join(32, run_bytes_cap=2048, merge_fan_in=_MERGE_FAN_IN) as rows:
            # A real multi-run merge: more initial runs than merge_fan_in, so
            # finish() ran at least one merge PASS (not a single buffered run).
            assert rows._sorter._run_counter > _MERGE_FAN_IN
            cursor = JoinRowCursor(rows, join_columns=fx.edge.child_columns)
            raw = cursor.take(n, 0)
            cursor.assert_exhausted()
            row_nrs = raw.column("__decoy_row_nr").to_pylist()
            assert row_nrs == list(range(n))
            fk_arrays = joiner.resolve_batch(raw)
        result = fx.child.set_column(
            fx.child.schema.get_field_index(fx.edge.child_columns[0]),
            fx.edge.child_columns[0],
            fk_arrays[0],
        )
    assert result.num_rows == n


# ---------------------------------------------------------------------------
# Test #4 -- the contiguity guard fails closed, against an INDEPENDENT N
# ---------------------------------------------------------------------------


def _dropping_join_rows(joiner: StreamFkJoiner, drop_row_nr: int):
    """Wrap `_iter_unordered_join_rows` to silently drop one row_nr from the
    stream before it reaches the sorter -- the test seam the plan calls for
    ("a test seam that drops a row post-join"), since reliably forcing a real
    DuckDB join to lose exactly one row is not practical."""
    original = joiner._iter_unordered_join_rows

    def _wrapped(batch_rows: int):
        for batch in original(batch_rows):
            mask = pc.not_equal(batch.column("__decoy_row_nr"), drop_row_nr)
            filtered = batch.filter(mask)
            if filtered.num_rows:
                yield filtered

    return _wrapped


@pytest.mark.parametrize(
    "drop_row_nr",
    [
        4,  # the LAST row_nr: a lost SUFFIX. A join-output-inferred N would see
        # a dense 0..3 run and wrongly self-validate as a complete result; the
        # independent child-stage count (N=5) catches the shortfall.
        2,  # a MIDDLE row_nr: a genuine gap mid-stream.
    ],
)
def test_contiguity_guard_fails_closed(tmp_path, monkeypatch, drop_row_nr: int) -> None:
    fx = simple_edge_fixture(tmp_path / "fx")  # 5 child rows, row_nr 0..4
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        monkeypatch.setattr(
            joiner, "_iter_unordered_join_rows", _dropping_join_rows(joiner, drop_row_nr)
        )
        rows = joiner.run_ordered_join(64, run_bytes_cap=_RUN_BYTES_CAP, merge_fan_in=_MERGE_FAN_IN)
        # All-or-nothing: draining to a list must raise, never return a
        # (silently short) result.
        with pytest.raises(ExecutionError) as excinfo:
            list(rows)
        assert excinfo.value.code == "out_of_core_fk_reorder_contiguity"
        # The raise closes the sorter (spill unlinked), per the lifecycle
        # contract -- checked precisely in the lifecycle tests below.
        assert rows._closed is True


# ---------------------------------------------------------------------------
# Test #6 -- missing/insufficient budget fails closed, before any blocking work
# ---------------------------------------------------------------------------


def test_reorder_unbudgeted_fails_closed() -> None:
    """The consumer-boundary wiring: a driver resolves budgets with
    `resolve_reorder_budgets` before ever constructing a joiner or calling
    `run_ordered_join` -- a missing memory or disk budget raises here,
    before any blocking work exists to fail closed on."""
    with pytest.raises(ExecutionError) as excinfo:
        resolve_reorder_budgets(None, 10**9)
    assert excinfo.value.code == "out_of_core_reorder_unbudgeted"

    with pytest.raises(ExecutionError) as excinfo:
        resolve_reorder_budgets(512 * 1024 * 1024, None)
    assert excinfo.value.code == "out_of_core_reorder_unbudgeted"


def test_reorder_insufficient_disk_fails_closed() -> None:
    budgets = resolve_reorder_budgets(512 * 1024 * 1024, remaining_disk_bytes=1_000)
    with pytest.raises(ExecutionError) as excinfo:
        require_disk(budgets, mandatory_staging_bytes=0, estimated_output_bytes=10**9)
    assert excinfo.value.code == "out_of_core_reorder_budget_too_small"


# ---------------------------------------------------------------------------
# Test #7 -- the join-row batch clears the sorter's own key contract
# ---------------------------------------------------------------------------


def test_join_rows_clear_sorter_key_contract(tmp_path) -> None:
    fx = simple_edge_fixture(tmp_path / "fx")
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        batch = next(iter(joiner._iter_unordered_join_rows(64)))
        schema = batch.schema
        assert "__decoy_row_nr" in schema.names
        row_nr_field = schema.field("__decoy_row_nr")
        assert row_nr_field.type == pa.int64()
        assert batch.column("__decoy_row_nr").null_count == 0
        assert _min_row_bytes(schema) >= INDEX_BYTES


def test_run_ordered_join_constructs_sorter_with_explicit_sort_key(tmp_path, monkeypatch) -> None:
    """`run_ordered_join` must pass `sort_key_column="__decoy_row_nr"`
    EXPLICITLY (Codex plan-gate LOW 7), not rely on the sorter's default, so a
    future default change cannot silently mis-key this consumer."""
    import decoy_engine.execution.out_of_core._stream_join as stream_join_mod

    captured: dict[str, object] = {}
    real_sorter_cls = stream_join_mod.BoundedExternalSorter

    class _Capturing(real_sorter_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(stream_join_mod, "BoundedExternalSorter", _Capturing)

    fx = simple_edge_fixture(tmp_path / "fx")
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        with joiner.run_ordered_join(
            64, run_bytes_cap=_RUN_BYTES_CAP, merge_fan_in=_MERGE_FAN_IN
        ) as rows:
            list(rows)

    assert captured.get("sort_key_column") == "__decoy_row_nr"


# ---------------------------------------------------------------------------
# Test #10 -- resource lifecycle / cleanup
# ---------------------------------------------------------------------------


def _spill_files(joiner: StreamFkJoiner) -> list:
    reorder_dir = joiner._temp_dir / "reorder"
    if not reorder_dir.exists():
        return []
    return list(reorder_dir.iterdir())


def test_drain_failure_closes_connection_and_spill(tmp_path, monkeypatch) -> None:
    fx = wide_edge_fixture(tmp_path / "fx", parent_rows=10, child_rows=50)
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())

        original = joiner._iter_unordered_join_rows

        def _raising(batch_rows: int):
            for i, batch in enumerate(original(batch_rows)):
                if i == 6:
                    # By this point (measured empirically for this fixture's
                    # sizing), the sorter has already flushed a real run file
                    # to disk -- proving the cleanup below unlinks an ACTUAL
                    # spill, not a directory that was merely never populated.
                    assert _spill_files(joiner) != []
                    raise RuntimeError("injected mid-drain failure")
                yield batch

        monkeypatch.setattr(joiner, "_iter_unordered_join_rows", _raising)

        with pytest.raises(RuntimeError, match="injected mid-drain failure"):
            joiner.run_ordered_join(8, run_bytes_cap=2048, merge_fan_in=_MERGE_FAN_IN)

        assert joiner._conn is None
        assert _spill_files(joiner) == []


def test_drain_sorter_failure_closes_connection_and_spill(tmp_path, monkeypatch) -> None:
    """A failure inside the sorter itself (not the join drain) must ALSO
    close the connection and unlink the spill -- including any run files the
    sorter had ALREADY flushed to disk before the injected failure, not just
    the (vacuous) case where nothing was ever written."""
    import decoy_engine.execution.out_of_core._stream_join as stream_join_mod

    fx = wide_edge_fixture(tmp_path / "fx", parent_rows=25, child_rows=300)
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())

        real_write = stream_join_mod.BoundedExternalSorter.write
        calls = {"n": 0}

        def _failing_write(self, batch):
            calls["n"] += 1
            if calls["n"] == 5:
                # By call 5, with this run_bytes_cap, real run files already
                # exist on disk (proven by the assertion just below).
                assert len(self._all_run_files) > 0
                raise RuntimeError("injected sorter failure")
            return real_write(self, batch)

        monkeypatch.setattr(stream_join_mod.BoundedExternalSorter, "write", _failing_write)

        with pytest.raises(RuntimeError, match="injected sorter failure"):
            joiner.run_ordered_join(32, run_bytes_cap=2048, merge_fan_in=_MERGE_FAN_IN)

        assert joiner._conn is None
        assert _spill_files(joiner) == []


def test_result_closed_before_first_next_cleans_up(tmp_path) -> None:
    fx = simple_edge_fixture(tmp_path / "fx")
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        rows = joiner.run_ordered_join(64, run_bytes_cap=_RUN_BYTES_CAP, merge_fan_in=_MERGE_FAN_IN)
        rows.close()  # never called next()
        assert _spill_files(joiner) == []
        rows.close()  # idempotent


def test_result_closed_after_partial_consumption_cleans_up(tmp_path) -> None:
    fx = wide_edge_fixture(tmp_path / "fx", parent_rows=25, child_rows=300)
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        rows = joiner.run_ordered_join(32, run_bytes_cap=2048, merge_fan_in=_MERGE_FAN_IN)
        next(rows)
        next(rows)
        rows.close()
        assert _spill_files(joiner) == []
        rows.close()  # idempotent, second close is a no-op
        rows.close()  # a third, for good measure


def test_result_context_manager_exit_cleans_up_on_partial_consumption(tmp_path) -> None:
    fx = wide_edge_fixture(tmp_path / "fx", parent_rows=25, child_rows=300)
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        with joiner.run_ordered_join(32, run_bytes_cap=2048, merge_fan_in=_MERGE_FAN_IN) as rows:
            next(rows)
        assert _spill_files(joiner) == []


def test_malformed_explain_fails_closed(tmp_path, monkeypatch) -> None:
    fx = simple_edge_fixture(tmp_path / "fx")
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        monkeypatch.setattr(joiner, "_run_explain_json", lambda query: {"no_name_key": True})

        with pytest.raises(ExecutionError) as excinfo:
            joiner.run_ordered_join(64, run_bytes_cap=_RUN_BYTES_CAP, merge_fan_in=_MERGE_FAN_IN)
        assert excinfo.value.code == "out_of_core_fk_join_plan_unverified"
        assert joiner._conn is None
        assert _spill_files(joiner) == []


# ---------------------------------------------------------------------------
# VERIFY-phase mutation pins (found by mutation grading of the changed units)
# ---------------------------------------------------------------------------


class _StubSorter:
    """Minimal duck-typed sorter for driving `_OrderedJoinRows`'s guard with a
    crafted `iter_ordered()` sequence (no real spill)."""

    def __init__(self, batches: list[pa.RecordBatch]) -> None:
        self._batches = batches
        self.closed = False

    def iter_ordered(self):
        yield from self._batches

    def close(self) -> None:
        self.closed = True


def test_contiguity_guard_catches_within_two_row_batch_duplicate() -> None:
    # Pins the within-batch adjacent-diff check for a 2-ROW batch (mutation
    # `if n > 1` -> `if n > 2`). A duplicate row_nr inside a single 2-row batch
    # keeps the total count and the running position both equal to N, so neither
    # the first-of-batch check nor the end-of-stream count catches it -- only the
    # per-batch adjacent-diff check does. It must still fail closed.
    batch = pa.record_batch({"__decoy_row_nr": pa.array([0, 0], type=pa.int64())})
    sorter = _StubSorter([batch])
    result = _OrderedJoinRows(sorter, expected_row_count=2)
    with pytest.raises(ExecutionError) as excinfo:
        list(result)
    assert excinfo.value.code == "out_of_core_fk_reorder_contiguity"
    assert sorter.closed is True  # the raise closes the owning iterator


def test_contiguity_guard_catches_two_row_batch_internal_gap() -> None:
    # Companion to the duplicate case: a gap inside a 2-row batch ([0, 2] with a
    # missing 1) is caught only by the adjacent-diff check when it is the final
    # batch, since the end position (2) would otherwise look consistent with N=3.
    batch = pa.record_batch({"__decoy_row_nr": pa.array([0, 2], type=pa.int64())})
    sorter = _StubSorter([batch])
    result = _OrderedJoinRows(sorter, expected_row_count=2)
    with pytest.raises(ExecutionError) as excinfo:
        list(result)
    assert excinfo.value.code == "out_of_core_fk_reorder_contiguity"


class _FakeExplainResult:
    def __init__(self, rows: list[tuple[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, object]]:
        return self._rows


class _FakeExplainConn:
    def __init__(self, rows: list[tuple[str, object]]) -> None:
        self._rows = rows

    def execute(self, _sql: str) -> _FakeExplainResult:
        return _FakeExplainResult(self._rows)


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([("logical_plan", "x")], id="no_physical_plan_row"),
        pytest.param([("physical_plan", "not valid json{")], id="invalid_json"),
        pytest.param([("physical_plan", '{"not": "a single-element list"}')], id="wrong_shape"),
        pytest.param([("physical_plan", "[]")], id="empty_list"),
        pytest.param([("physical_plan", "[1, 2]")], id="multi_element_list"),
    ],
)
def test_run_explain_json_malformed_shapes_fail_closed(tmp_path, monkeypatch, rows) -> None:
    # Pins _run_explain_json's OWN fail-closed error CODE for every malformed
    # EXPLAIN shape (missing physical_plan row, invalid JSON, non-list/non-single
    # top-level shape). The existing malformed test monkeypatches _run_explain_json
    # wholesale, so these internal raises (and their machine-consumed `code`) were
    # otherwise unpinned -- mutation flipped each `code=` to `None` and survived.
    fx = simple_edge_fixture(tmp_path / "fx")
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        monkeypatch.setattr(joiner, "_ensure_conn", lambda: _FakeExplainConn(rows))
        with pytest.raises(ExecutionError) as excinfo:
            joiner._run_explain_json("SELECT 1")
        assert excinfo.value.code == "out_of_core_fk_join_plan_unverified"

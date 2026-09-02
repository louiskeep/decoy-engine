"""Single-edge byte-parity harness: `run_ordered_join` vs the executable
oracle `_join.py::mask_child_fk` (P4-A.3 Task C).

`tests/parity/test_out_of_core_fk_parity.py` compares the FULL route
(`run_fk_out_of_core`) against the pandas adapter at the whole-plan level;
this reorder consumer is exercised directly against ITS OWN single-edge
oracle instead, so the thin seam here reuses that parity module's
value+order+null-exact compare helper (`_assert_value_equal`) rather than
rederiving it -- the plan's own instruction is to reuse, never weaken, that
comparison.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from decoy_engine.execution.out_of_core._join import mask_child_fk, orphan_fk_warning
from decoy_engine.execution.out_of_core._runner import _remap_values
from decoy_engine.execution.out_of_core._stream_join import JoinRowCursor, StreamFkJoiner
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity.test_out_of_core_fk_parity import _assert_value_equal

from ._ooc_fixtures import OocEdgeFixture

_DEFAULT_BATCH_ROWS = 64
_DEFAULT_RUN_BYTES_CAP = 8 * 1024 * 1024
_DEFAULT_MERGE_FAN_IN = 4


def oracle_output(
    fx: OocEdgeFixture, temp_dir: Path
) -> tuple[pa.Table, tuple[QualityWarning, ...]]:
    """The executable single-edge oracle: `_join.py::mask_child_fk` over the
    whole child, exactly as `test_out_of_core_batch_join.py`'s own
    `_whole_child_oracle` drives it for the resident-parent joiner."""
    remap_values = (
        _remap_values(fx.plan, fx.edge, fx.child)
        if fx.edge.orphan_policy is OrphanPolicy.REMAP
        else None
    )
    return mask_child_fk(
        child=fx.child,
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        temp_dir=temp_dir,
        remap_values=remap_values,
    )


def ordered_join_output(
    fx: OocEdgeFixture,
    temp_dir: Path,
    *,
    batch_rows: int = _DEFAULT_BATCH_ROWS,
    run_bytes_cap: int = _DEFAULT_RUN_BYTES_CAP,
    merge_fan_in: int = _DEFAULT_MERGE_FAN_IN,
) -> tuple[pa.Table, tuple[QualityWarning, ...]]:
    """Drive one edge end to end through the bounded-reorder path: stage_keys
    -> run_ordered_join -> JoinRowCursor -> resolve_batch, then rebuild the
    child table exactly like `mask_child_fk` does (its own FK column(s)
    replaced with the resolved output)."""
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
        with joiner.run_ordered_join(
            batch_rows, run_bytes_cap=run_bytes_cap, merge_fan_in=merge_fan_in
        ) as rows:
            if n == 0:
                # A zero-row child never yields a join-row batch to probe a
                # schema from (unlike DuckDB's own `to_arrow_reader`, which
                # can carry a schema on an empty result); there is nothing
                # for `resolve_batch` to resolve, so build the correctly
                # typed empty FK output directly from `output_types` instead
                # of routing an empty batch through `JoinRowCursor`.
                assert list(rows) == []
                fk_arrays: tuple[pa.Array, ...] = tuple(
                    pa.array([], type=t) for t in joiner.output_types
                )
            else:
                cursor = JoinRowCursor(rows, join_columns=fx.edge.child_columns)
                raw = cursor.take(n, 0)
                cursor.assert_exhausted()
                fk_arrays = joiner.resolve_batch(raw)
        result = fx.child
        for idx, child_col in enumerate(fx.edge.child_columns):
            result = result.set_column(
                result.schema.get_field_index(child_col), child_col, fk_arrays[idx]
            )
        warnings: tuple[QualityWarning, ...] = ()
        if joiner.orphan_total and fx.edge.orphan_policy is OrphanPolicy.WARN:
            warnings = (orphan_fk_warning(fx.edge, joiner.orphan_total),)
        return result, warnings


def _warning_key(warning: QualityWarning) -> tuple[object, ...]:
    return (warning.code, warning.provider, warning.column, warning.detail)


def assert_byte_parity(
    fx: OocEdgeFixture,
    temp_dir: Path,
    *,
    label: str,
    batch_rows: int = _DEFAULT_BATCH_ROWS,
    run_bytes_cap: int = _DEFAULT_RUN_BYTES_CAP,
    merge_fan_in: int = _DEFAULT_MERGE_FAN_IN,
) -> None:
    """Assert `run_ordered_join`'s resolved output is byte-identical (value,
    order, null, and warning) to the executable single-edge oracle. The hard
    correctness gate for P4-A.3 acceptance tests #1, #2, and #3b."""
    oracle_table, oracle_warnings = oracle_output(fx, temp_dir / "oracle")
    ooc_table, ooc_warnings = ordered_join_output(
        fx,
        temp_dir / "ooc",
        batch_rows=batch_rows,
        run_bytes_cap=run_bytes_cap,
        merge_fan_in=merge_fan_in,
    )
    _assert_value_equal(oracle_table, ooc_table, label)
    assert [_warning_key(w) for w in ooc_warnings] == [_warning_key(w) for w in oracle_warnings], (
        f"{label}: warnings diverge\n oracle={oracle_warnings}\n    ooc={ooc_warnings}"
    )


__all__ = ["assert_byte_parity", "oracle_output", "ordered_join_output"]

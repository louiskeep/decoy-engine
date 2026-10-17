"""F2: the production mixed generate+mask route is byte-identical to the full-frame
mixed oracle, and the mask half really ran on the chunked lane.

Every case runs the same config twice: `execution_mode="full_frame"` (the oracle) and
`"auto"` (the new route), then compares the output tables by Arrow IPC bytes. The
route is asserted from the result (`execution_mode == "mixed"`, several chunks) and the
backend from the lane's own per-column evidence, so a label alone cannot pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.parity._f2_mixed_support import (
    CHILD,
    CHUNK_ROWS,
    COMPANION_PRESENT,
    FK,
    NEEDS_COMPANION,
    PARENT,
    RecordingSink,
    assert_outputs_identical,
    child_table,
    chunk_count,
    executed_backends,
    ipc_bytes,
    lazy_source,
    mask_evidence,
    mixed_config,
    oracle,
    run,
    warning_signature,
)

STRING_KEYS = [str(i % 5) for i in range(40)]
INT_PARENT = [{"name": "id", "type": "categorical", "categories": [10, 11, 12, 13]}]


def _assert_mixed_route(result: Any, *, chunks_at_least: int = 2) -> None:
    assert result.quality_metrics["execution"]["execution_mode"] == "mixed"
    assert chunk_count(result) >= chunks_at_least


def test_independent_mixed_matches_the_oracle_and_runs_chunked(tmp_path: Path) -> None:
    child = child_table(pa.array(STRING_KEYS, pa.string()))
    config = mixed_config(tmp_path, child, coupled=False)
    want = oracle(config, {CHILD: child})
    got = run(config, {CHILD: child})
    _assert_mixed_route(got)
    assert got.quality_metrics["mixed_route"]["edge"] is None
    assert_outputs_identical(got.outputs, want.outputs)
    assert warning_signature(got) == warning_signature(want)


@NEEDS_COMPANION
def test_the_mask_half_executes_on_the_compiled_backend(tmp_path: Path) -> None:
    child = child_table(pa.array(STRING_KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    got = run(config, {CHILD: child})
    assert mask_evidence(got)["native_admitted"] is True
    backends = executed_backends(got)
    assert backends["email"] == "rust_companion"
    assert backends[FK] != "pandas_oracle"


@pytest.mark.skipif(COMPANION_PRESENT, reason="runs where the companion is absent")
def test_without_the_companion_the_oracle_leg_still_matches(tmp_path: Path) -> None:
    """A missing companion is a clean downgrade inside the chunked lane, not a route
    failure: the same bytes, every column on the oracle leg."""
    child = child_table(pa.array(STRING_KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    want = oracle(config, {CHILD: child})
    got = run(config, {CHILD: child})
    _assert_mixed_route(got)
    assert mask_evidence(got)["native_admitted"] is False
    assert executed_backends(got)["email"] == "pandas_oracle"
    assert_outputs_identical(got.outputs, want.outputs)


@pytest.mark.parametrize("key_kind", ["string", "int"])
def test_coupled_preserve_matches_across_chunks(tmp_path: Path, key_kind: str) -> None:
    if key_kind == "string":
        keys = pa.array([*STRING_KEYS[:-2], "orphan-a", "orphan-b"], pa.string())
        parent: list[dict[str, Any]] | None = None
    else:
        keys = pa.array([10 + i % 4 for i in range(38)] + [99, 98], pa.int64())
        parent = INT_PARENT
    child = child_table(keys)
    config = mixed_config(tmp_path, child, parent_columns=parent, parent_rows=12)
    want = oracle(config, {CHILD: child})
    got = run(config, {CHILD: child})
    _assert_mixed_route(got, chunks_at_least=3)
    assert got.quality_metrics["mixed_route"]["edge"]["child_column"] == FK
    assert_outputs_identical(got.outputs, want.outputs)


def test_coupled_keys_are_a_subset_of_the_generated_parent_keys(tmp_path: Path) -> None:
    child = child_table(pa.array(STRING_KEYS, pa.string()))
    config = mixed_config(tmp_path, child, parent_rows=5)
    got = run(config, {CHILD: child})
    assert set(got.outputs[CHILD].column(FK).to_pylist()) <= set(
        got.outputs[PARENT].column("id").to_pylist()
    )


def test_warn_policy_emits_one_edge_warning_with_the_oracle_count(tmp_path: Path) -> None:
    keys = [*STRING_KEYS[:30], *["zzz"] * 4, *STRING_KEYS[:6]]
    child = child_table(pa.array(keys, pa.string()))
    config = mixed_config(tmp_path, child, policy="warn")
    want = oracle(config, {CHILD: child})
    got = run(config, {CHILD: child})
    _assert_mixed_route(got, chunks_at_least=3)
    assert_outputs_identical(got.outputs, want.outputs)
    orphan = [w for w in got.warnings if w.code == "orphan_fk"]
    assert len(orphan) == 1  # one per edge, never one per chunk
    assert orphan[0].detail["orphan_rows"] == 4
    assert warning_signature(got) == warning_signature(want)


def test_fail_policy_raises_the_oracle_error_and_publishes_nothing(tmp_path: Path) -> None:
    from decoy_engine.execution import ExecutionError

    child = child_table(pa.array([*STRING_KEYS[:39], "zzz"], pa.string()))
    config = mixed_config(tmp_path, child, policy="fail")
    with pytest.raises(ExecutionError) as want:
        oracle(config, {CHILD: child})
    sink = RecordingSink()
    with pytest.raises(ExecutionError) as got:
        run(config, {CHILD: child}, sink=sink)
    assert got.value.code == want.value.code == "orphan_fk_violation"
    assert str(got.value) == str(want.value)
    assert sink.aborted == 1 and sink.committed == 0 and sink.published is None
    assert not [c for c in sink.calls if c[0].startswith("write")]


def test_remap_policy_with_only_matched_keys_matches_the_oracle(tmp_path: Path) -> None:
    child = child_table(pa.array(STRING_KEYS, pa.string()))
    config = mixed_config(tmp_path, child, policy="remap")
    want = oracle(config, {CHILD: child})
    got = run(config, {CHILD: child})
    _assert_mixed_route(got)
    assert_outputs_identical(got.outputs, want.outputs)


def test_remap_policy_with_an_orphan_raises_on_both_sides(tmp_path: Path) -> None:
    from decoy_engine.execution import ExecutionError

    child = child_table(pa.array([*STRING_KEYS[:39], "zzz"], pa.string()))
    config = mixed_config(tmp_path, child, policy="remap")
    with pytest.raises(ExecutionError) as want:
        oracle(config, {CHILD: child})
    with pytest.raises(ExecutionError) as got:
        run(config, {CHILD: child})
    assert got.value.code == want.value.code == "orphan_remap_parent_missing"
    assert str(got.value) == str(want.value)


def test_int_keys_above_two_to_the_53_stay_exact(tmp_path: Path) -> None:
    big = 2**53 + 12345
    keys = pa.array([10 + i % 4 for i in range(36)] + [big, big + 1, 11, 10], pa.int64())
    child = child_table(keys)
    config = mixed_config(tmp_path, child, parent_columns=INT_PARENT, parent_rows=12)
    want = oracle(config, {CHILD: child})
    got = run(config, {CHILD: child})
    _assert_mixed_route(got)
    assert_outputs_identical(got.outputs, want.outputs)
    out = got.outputs[CHILD].column(FK).to_pylist()
    assert big in out and big + 1 in out


@pytest.mark.parametrize("row_group", [5, 11])
def test_lazy_source_with_uneven_batches_nulls_and_a_partial_final_chunk(
    tmp_path: Path, row_group: int
) -> None:
    """Row groups of 5 or 11 against 7-row chunks: batch edges never line up with chunk
    edges, 41 rows leave a partial final chunk, and null keys sit across the seams."""
    keys = [None if i % 9 == 4 else str(i % 5) for i in range(41)]
    child = child_table(pa.array(keys, pa.string()))
    config = mixed_config(tmp_path, child, policy="preserve")
    want = oracle(config, {CHILD: child})
    lazy = lazy_source(tmp_path, child, row_group_size=row_group)
    sink = RecordingSink()
    got = run(config, {CHILD: lazy}, sink=sink)
    assert got.quality_metrics["execution"]["outputs_streamed"] is True
    assert got.quality_metrics["mixed_route"]["mask_tables"][CHILD]["auto_chunk"]["input"][
        "mode"
    ] == "lazy"
    assert chunk_count(got) == -(-41 // CHUNK_ROWS)
    assert got.outputs == {}
    assert sink.committed == 1 and sink.aborted == 0
    assert_outputs_identical(sink.published or {}, want.outputs)


def test_streamed_and_resident_outputs_are_the_same_bytes(tmp_path: Path) -> None:
    child = child_table(pa.array(STRING_KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    resident = run(config, {CHILD: child})
    sink = RecordingSink()
    streamed = run(config, {CHILD: child}, sink=sink)
    assert resident.quality_metrics["execution"]["outputs_streamed"] is False
    assert streamed.quality_metrics["execution"]["outputs_streamed"] is True
    assert_outputs_identical(sink.published or {}, resident.outputs)
    assert ipc_bytes((sink.published or {})[CHILD]) == ipc_bytes(resident.outputs[CHILD])

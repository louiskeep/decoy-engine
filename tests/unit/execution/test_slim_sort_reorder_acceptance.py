"""P4 Task 7 slim-sort acceptance matrix (single-edge, plan §6).

The slim sort re-fetches the raw child columns out-of-line in phase 3 instead
of carrying them through the bounded sorter, so a wide raw key, a wide orphan,
or a dictionary-encoded key can no longer overflow the sorter. These tests pin
the headline cases against the executable single-edge oracle
(`_ooc_reorder_harness.assert_byte_parity` vs `_join.py::mask_child_fk`):

- a matched 6 MiB raw string FK under HASH now COMPLETES through the reorder
  path (the exact shape a prior review round could not close), byte-identical
  to `_batch_join`;
- a 6 MiB ORPHAN under PRESERVE/WARN/REMAP/FAIL resolves identically;
- the compact nullable-boolean match token distinguishes a matched-null-masked
  row from an orphan by NULLNESS, not the masked value;
- binary raw keys resolve identically (dictionary-encoded MASKED values are
  covered by test_key_width_slim_bound.py);
- a composite key with one very wide raw component resolves identically.

Plus direct guards: the raw child columns never reach the sorter
(`_assert_slim_sorter_schema`), and the phase-3 lockstep child cursor fails
closed on any misalignment.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._mask import mask_table
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_from_tables
from decoy_engine.execution.out_of_core._runner import _column_seed
from decoy_engine.execution.out_of_core._stream_join import (
    ChildKeyLockstepCursor,
    StreamFkJoiner,
)
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

from ._ooc_fixtures import OocEdgeFixture
from ._ooc_reorder_harness import assert_byte_parity, ordered_join_output

_JOB_SEED = b"\x44" * 8
_WIDE = 6 * 1024 * 1024


def _seed(strategy: str, *, namespace: str | None = None) -> ColumnSeed:
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


def _plan(seeds: dict[str, ColumnSeed], *, parent: str, child: str) -> Any:
    per_column = tuple(seeds.items())
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (parent, TableSeed(per_column=per_column, per_group=())),
                (child, TableSeed(per_column=per_column, per_group=())),
            ),
        )
    )


def _build_fixture(
    temp_dir: Path,
    *,
    seeds: dict[str, ColumnSeed],
    parent: pa.Table,
    child: pa.Table,
    parent_columns: tuple[str, ...],
    child_columns: tuple[str, ...],
    orphan_policy: OrphanPolicy,
) -> OocEdgeFixture:
    plan = _plan(seeds, parent="parent", child="child")
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=parent_columns,
        child_table="child",
        child_columns=child_columns,
        namespace="ns",
        orphan_policy=orphan_policy,
    )
    masked_parent = mask_table(plan, edge.parent_table, parent, skip_columns=frozenset())
    relation = build_parent_key_relation_from_tables(
        source_parent=parent,
        masked_parent=masked_parent,
        edge=edge,
        temp_dir=temp_dir / "relation",
    )
    remap_seeds = (
        tuple(_column_seed(plan, edge.parent_table, col) for col in edge.parent_columns)
        if orphan_policy is OrphanPolicy.REMAP
        else None
    )
    return OocEdgeFixture(
        plan=plan,
        edge=edge,
        parent_relation=relation,
        child=child,
        child_key_types=tuple(child.column(c).type for c in child_columns),
        remap_seeds=remap_seeds,
        job_seed=plan.seed_envelope.job_seed if orphan_policy is OrphanPolicy.REMAP else None,
    )


def _hash_wide_fixture(
    temp_dir: Path, *, orphan_policy: OrphanPolicy, include_orphan: bool
) -> OocEdgeFixture:
    """A hash-masked FK edge with a matched 6 MiB raw key (and optionally a
    6 MiB ORPHAN). The masked value is a small hash, so the SLIM sorter row is
    tiny even though the raw child key is multi-MB."""
    wide = "w" * _WIDE
    keys = [wide, "k1"]
    child_keys = [wide, "k1", wide]
    if include_orphan:
        child_keys.append("o" * _WIDE)  # a 6 MiB orphan (no parent row)
    parent = pa.table({"key": pa.array(keys, type=pa.string())})
    child = pa.table({"key": pa.array(child_keys, type=pa.string())})
    return _build_fixture(
        temp_dir,
        seeds={"key": _seed("hash", namespace="ns")},
        parent=parent,
        child=child,
        parent_columns=("key",),
        child_columns=("key",),
        orphan_policy=orphan_policy,
    )


# ---------------------------------------------------------------------------
# Byte-parity: the 6 MiB cases the sorter used to reject now complete
# ---------------------------------------------------------------------------


def test_matched_6mib_raw_key_under_hash_completes_via_reorder(tmp_path: Path) -> None:
    fx = _hash_wide_fixture(
        tmp_path / "fx", orphan_policy=OrphanPolicy.PRESERVE, include_orphan=False
    )
    # A tiny run_bytes_cap that would REJECT the 6 MiB raw row if it still rode
    # through the sorter; the slim row is tiny, so this must complete.
    assert_byte_parity(
        fx, tmp_path / "run", label="matched-6mib-hash", run_bytes_cap=1 << 20, merge_fan_in=4
    )


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN])
def test_6mib_orphan_under_preserve_and_warn(tmp_path: Path, policy: OrphanPolicy) -> None:
    fx = _hash_wide_fixture(tmp_path / "fx", orphan_policy=policy, include_orphan=True)
    assert_byte_parity(
        fx,
        tmp_path / "run",
        label=f"orphan-6mib-{policy.value}",
        run_bytes_cap=1 << 20,
        merge_fan_in=4,
    )


def test_6mib_orphan_under_remap(tmp_path: Path) -> None:
    fx = _hash_wide_fixture(tmp_path / "fx", orphan_policy=OrphanPolicy.REMAP, include_orphan=True)
    assert_byte_parity(
        fx, tmp_path / "run", label="orphan-6mib-remap", run_bytes_cap=1 << 20, merge_fan_in=4
    )


def test_6mib_orphan_under_fail_precount(tmp_path: Path) -> None:
    # FAIL is a precount, not a resolve; the byte-parity harness resolves, so
    # exercise the FAIL count directly: a 6 MiB orphan must be counted, never
    # crash the sorter (the count never touches the sorter at all).
    fx = _hash_wide_fixture(tmp_path / "fx", orphan_policy=OrphanPolicy.FAIL, include_orphan=True)
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        assert joiner.total_orphans() == 1


# ---------------------------------------------------------------------------
# Compact match-token semantics: matched-null-masked vs orphan
# ---------------------------------------------------------------------------


def test_compact_token_matched_null_masked_vs_orphan(tmp_path: Path) -> None:
    """A matched row whose parent masks to NULL (redact) must resolve to that
    masked NULL, while an orphan resolves to its own key -- the token
    distinguishes them by NULLNESS, not by the masked value. Byte-identical to
    the oracle both ways."""
    seed = _seed("redact")
    parent = pa.table({"key": pa.array(["p0", "p1"], type=pa.string())})
    # p0 matched (masks to null under redact), an orphan, and p1 matched.
    child = pa.table({"key": pa.array(["p0", "orphanX", "p1"], type=pa.string())})
    fx = _build_fixture(
        tmp_path / "fx",
        seeds={"key": seed},
        parent=parent,
        child=child,
        parent_columns=("key",),
        child_columns=("key",),
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    assert_byte_parity(fx, tmp_path / "run", label="matched-null-masked-vs-orphan")


# ---------------------------------------------------------------------------
# Binary raw keys, matched and orphaned (the raw-key width vector, closed by
# the out-of-line re-fetch; dictionary-encoded MASKED values are covered by
# tests/unit/execution/test_key_width_slim_bound.py, and a dict-encoded child
# key COLUMN is not an admitted route input -- `_resolve_output_types` rejects
# it in both the batch and reorder routes).
# ---------------------------------------------------------------------------


def test_binary_raw_keys_matched_and_orphaned(tmp_path: Path) -> None:
    parent = pa.table({"key": pa.array([b"cc", b"dd"], type=pa.binary())})
    child = pa.table({"key": pa.array([b"cc", b"zz", b"dd", b"cc"], type=pa.binary())})
    fx = _build_fixture(
        tmp_path / "fx",
        seeds={"key": _seed("passthrough")},
        parent=parent,
        child=child,
        parent_columns=("key",),
        child_columns=("key",),
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    assert_byte_parity(fx, tmp_path / "run", label="binary-keys")


# ---------------------------------------------------------------------------
# Composite key with one very wide raw component
# ---------------------------------------------------------------------------


def test_composite_key_one_wide_raw_component(tmp_path: Path) -> None:
    wide = "w" * (2 * 1024 * 1024)
    parent = pa.table(
        {
            "a": pa.array([wide, "a1"], type=pa.string()),
            "b": pa.array(["b0", "b1"], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "a": pa.array([wide, "a1", wide, "orphanA"], type=pa.string()),
            "b": pa.array(["b0", "b1", "b0", "b9"], type=pa.string()),
        }
    )
    fx = _build_fixture(
        tmp_path / "fx",
        seeds={"a": _seed("hash", namespace="na"), "b": _seed("hash", namespace="nb")},
        parent=parent,
        child=child,
        parent_columns=("a", "b"),
        child_columns=("a", "b"),
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    assert_byte_parity(
        fx, tmp_path / "run", label="composite-wide-raw", run_bytes_cap=1 << 20, merge_fan_in=4
    )


# ---------------------------------------------------------------------------
# Direct guard: the raw child columns never reach the sorter
# ---------------------------------------------------------------------------


def test_sorter_input_schema_carries_no_raw_columns(tmp_path: Path) -> None:
    fx = _hash_wide_fixture(
        tmp_path / "fx", orphan_policy=OrphanPolicy.PRESERVE, include_orphan=False
    )
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        batch = next(iter(joiner._iter_unordered_join_rows(64)))
        names = batch.schema.names
        assert "__decoy_fk_join_key" not in names
        assert not any(n.startswith("__decoy_src_") for n in names)
        assert "__decoy_row_nr" in names
        assert "__decoy_parent_match" in names
        # The token is a compact nullable boolean, not the raw parent value.
        assert batch.schema.field("__decoy_parent_match").type == pa.bool_()


def test_run_ordered_join_asserts_slim_schema(tmp_path: Path, monkeypatch) -> None:
    """If a regression re-adds the raw columns to the sorter projection,
    `run_ordered_join` must fail closed (an internal-invariant guard, never a
    silent wide-row overflow)."""
    fx = _hash_wide_fixture(
        tmp_path / "fx", orphan_policy=OrphanPolicy.PRESERVE, include_orphan=False
    )
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        slim_iter = joiner._iter_unordered_join_rows

        def _fat(batch_rows: int):
            for batch in slim_iter(batch_rows):
                fat = batch.append_column(
                    "__decoy_src_0", pa.array(["x"] * batch.num_rows, type=pa.string())
                )
                yield fat

        monkeypatch.setattr(joiner, "_iter_unordered_join_rows", _fat)
        with pytest.raises(ExecutionError) as excinfo:
            joiner.run_ordered_join(64, run_bytes_cap=1 << 20, merge_fan_in=4)
        assert excinfo.value.code == "out_of_core_sorter_schema_not_slim"


# ---------------------------------------------------------------------------
# Phase-3 lockstep child cursor fails closed on misalignment
# ---------------------------------------------------------------------------


def _two_batch_joiner(tmp_path: Path) -> StreamFkJoiner:
    """A joiner whose child-key spill holds TWO batches (rows 0-1 and 2-4), so
    the lockstep cursor's per-batch alignment can be exercised."""
    seed = _seed("passthrough")
    fx = _build_fixture(
        tmp_path / "fx",
        seeds={"key": seed},
        parent=pa.table({"key": pa.array(["a", "b"], type=pa.string())}),
        child=pa.table({"key": pa.array(["a", "b", "a", "b", "a"], type=pa.string())}),
        parent_columns=("key",),
        child_columns=("key",),
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    joiner = StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    )
    joiner.begin_staging()
    joiner.stage_batch(pa.record_batch({"key": pa.array(["a", "b"], type=pa.string())}))
    joiner.stage_batch(pa.record_batch({"key": pa.array(["a", "b", "a"], type=pa.string())}))
    joiner.finalize_staging()
    return joiner


def test_child_cursor_wrong_row_nr_start_fails_closed(tmp_path: Path) -> None:
    joiner = _two_batch_joiner(tmp_path)
    try:
        cursor = ChildKeyLockstepCursor(joiner.open_child_key_reader())
        with pytest.raises(ExecutionError) as excinfo:
            cursor.take(2, expected_row_nr_start=5)  # spill starts at 0, not 5
        assert excinfo.value.code == "out_of_core_fk_row_alignment"
    finally:
        joiner.close()


def test_child_cursor_short_spill_fails_closed(tmp_path: Path) -> None:
    joiner = _two_batch_joiner(tmp_path)
    try:
        cursor = ChildKeyLockstepCursor(joiner.open_child_key_reader())
        # Ask for more rows than the spill holds (5): fails closed, never a
        # silent short read.
        with pytest.raises(ExecutionError) as excinfo:
            cursor.take(999, expected_row_nr_start=0)
        assert excinfo.value.code == "out_of_core_fk_row_alignment"
    finally:
        joiner.close()


def test_child_cursor_overshoot_fails_closed(tmp_path: Path) -> None:
    joiner = _two_batch_joiner(tmp_path)
    try:
        cursor = ChildKeyLockstepCursor(joiner.open_child_key_reader())
        # The first spill batch holds 2 rows; asking for 1 would need a partial
        # slice, which lockstep forbids (a retained remainder). Fail closed.
        with pytest.raises(ExecutionError) as excinfo:
            cursor.take(1, expected_row_nr_start=0)
        assert excinfo.value.code == "out_of_core_fk_row_alignment"
    finally:
        joiner.close()


def test_child_cursor_unconsumed_tail_fails_closed(tmp_path: Path) -> None:
    joiner = _two_batch_joiner(tmp_path)
    try:
        cursor = ChildKeyLockstepCursor(joiner.open_child_key_reader())
        cursor.take(2, expected_row_nr_start=0)  # first batch only; second remains
        with pytest.raises(ExecutionError) as excinfo:
            cursor.assert_exhausted()
        assert excinfo.value.code == "out_of_core_fk_row_alignment"
    finally:
        joiner.close()


def test_child_cursor_lockstep_take_succeeds(tmp_path: Path) -> None:
    joiner = _two_batch_joiner(tmp_path)
    try:
        cursor = ChildKeyLockstepCursor(joiner.open_child_key_reader())
        first = cursor.take(2, expected_row_nr_start=0)
        second = cursor.take(3, expected_row_nr_start=2)
        assert first.column("__decoy_row_nr").to_pylist() == [0, 1]
        assert second.column("__decoy_row_nr").to_pylist() == [2, 3, 4]
        cursor.assert_exhausted()
    finally:
        joiner.close()


# ---------------------------------------------------------------------------
# Single-source-read proof: a second raw read would permute; output is stable
# ---------------------------------------------------------------------------


def test_single_source_read_output_stable(tmp_path: Path) -> None:
    """The reorder path reads the child exactly once (keys staged in phase 1,
    resolved from that artifact). Driving it twice over the SAME staged spill
    yields identical output -- no second source read to permute against."""
    fx = _hash_wide_fixture(
        tmp_path / "fx", orphan_policy=OrphanPolicy.PRESERVE, include_orphan=True
    )
    first, _ = ordered_join_output(fx, tmp_path / "a", run_bytes_cap=1 << 20, merge_fan_in=4)
    second, _ = ordered_join_output(fx, tmp_path / "b", run_bytes_cap=1 << 20, merge_fan_in=4)
    assert first.column("key").to_pylist() == second.column("key").to_pylist()

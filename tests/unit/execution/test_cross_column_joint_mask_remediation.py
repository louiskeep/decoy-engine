"""Cross-column dependency PR1 joint_mask remediation (Codex final-gate findings).

Three joint_mask correctness bugs the Codex cross-model gate reproduced on the
PR1 branch, each with a reproduction that FAILS before the root-cause fix:

  F1 (wrong value): a scalar joint_mask node writes EVERY configured output, not
     just its dispatch column, but the writer-node map registered only the
     dispatch column. A dependent (`derived`) on a NON-dispatch joint_mask output
     then found no writer, got no ordering edge, and read the stale SOURCE value.
  F2 (fail-closed gap): with key_by naming no source column and no snapshot, the
     guard did not fail closed and the run selected a reference row via the
     synthetic null-key fallback instead of raising.
  F3 (regression): a `when`-gated joint_mask handed the handler a row subset while
     the key_by snapshot spanned the full table; the equal-length check raised
     `joint_mask_key_by_snapshot_misaligned` on a valid partial selection.

Each run goes through the real plan -> PandasExecutionAdapter path and is compared
to the full-frame oracle, matching the acceptance suite's construction in
test_cross_column_ordering.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = b"\xca\xfe" * 16  # 32 bytes

_ZIP_COLS = ("zip", "city", "state")


def _col(
    strategy: str,
    *,
    provider_config: tuple[tuple[str, Any], ...] = (),
    namespace: str | None = None,
    when: str | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
        when=when,
    )


def _plan(per_column: list[tuple[str, ColumnSeed]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=tuple(per_column), per_group=())),),
        )
    )


def _run(
    columns: dict[str, pa.Array],
    per_column: list[tuple[str, ColumnSeed]],
) -> dict[str, list]:
    table = pa.table(columns)
    result = PandasExecutionAdapter().run_single(
        _plan(per_column), table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    return {col: result.output.column(col).to_pylist() for col in result.output.column_names}


def _str(vals: list[Any]) -> pa.Array:
    return pa.array(vals, type=pa.string())


def _joint_cfg(key_col: str, columns: tuple[str, ...] = _ZIP_COLS) -> tuple[tuple[str, Any], ...]:
    return (
        ("columns", list(columns)),
        ("reference", "us_zip5_city_state"),
        ("key_by", key_col),
    )


class TestFinding1WriterMapMissesJointMaskOutputs:
    """F1: a `derived` column referencing a NON-dispatch joint_mask output must
    read the MASKED output, which requires the writer map to list every column
    the joint_mask node writes (not just its dispatch column)."""

    def test_derived_on_non_dispatch_joint_output_reads_masked_value(self) -> None:
        # One scalar joint_mask node: its dispatch column is "city" (first sorted
        # of zip/city/state), but it WRITES zip + city + state. A derived column
        # "a_out" (sorts before "city") references "zip", a NON-dispatch output.
        pids = ["P0001", "P0002", "P0003", "P0004"]
        n = len(pids)
        out = _run(
            {
                "pid": _str(pids),
                "zip": _str(["x"] * n),
                "city": _str(["x"] * n),
                "state": _str(["x"] * n),
                "a_out": _str(["x"] * n),
            },
            [
                ("pid", _col("passthrough")),
                ("city", _col("joint_mask", provider_config=_joint_cfg("pid"))),
                ("a_out", _col("derived", provider_config=(("expression", 'concat(zip, "!")'),))),
            ],
        )
        # The derived output must equal the MASKED zip plus "!", i.e. it read the
        # joint_mask output from the live frame under an ordering edge. Pre-fix,
        # no edge existed, so "a_out" ran first and read the source "x" -> "x!".
        assert out["a_out"] == [z + "!" for z in out["zip"]]
        # Sanity: the zips were actually masked (not the source placeholder), so
        # the equality above is not trivially satisfied by a stale read.
        assert out["zip"] != ["x"] * n
        assert all(z != "x" for z in out["zip"])


class TestFinding2MissingKeySnapshotFailsClosed:
    """F2: key_by names no source column and no snapshot exists -> the guard must
    fail closed with a coded error, never fall back to the synthetic null key."""

    def test_missing_key_by_column_and_snapshot_raises(self) -> None:
        n = 4
        with pytest.raises(StrategyError) as exc:
            _run(
                {
                    "zip": _str(["x"] * n),
                    "city": _str(["x"] * n),
                    "state": _str(["x"] * n),
                },
                # key_by="id": there is no "id" column and the adapter can build
                # no snapshot for it. Pre-fix this slipped past the guard (which
                # only fired when key_by WAS a frame column) and masked off a
                # synthetic null key.
                [("city", _col("joint_mask", provider_config=_joint_cfg("id")))],
            )
        assert exc.value.code == "joint_mask_key_by_snapshot_missing"


class TestFinding3WhenGatedPartialSelectionAligns:
    """F3: a `when`-gated joint_mask hands the handler a row subset; the full-table
    key_by snapshot must be selected down to the gated rows, not rejected for a
    length mismatch. The matched row is masked (matching the ungated oracle on that
    row's source key); the unmatched row passes through untouched."""

    def test_partial_when_gate_masks_matched_row_only(self) -> None:
        pids = ["P0001", "P0002"]
        # Oracle: the SAME joint_mask, ungated, on just the matched row's source
        # key. Its masked city is what the gated run must emit for that row.
        oracle = _run(
            {"pid": _str(["P0001"]), "city": _str(["x"])},
            [
                ("pid", _col("passthrough")),
                ("city", _col("joint_mask", provider_config=_joint_cfg("pid", ("city",)))),
            ],
        )
        oracle_city = oracle["city"][0]

        out = _run(
            {
                "pid": _str(pids),
                "city": _str(["x", "x"]),
                "keep": pa.array([1, 0], type=pa.int64()),
            },
            [
                ("pid", _col("passthrough")),
                ("keep", _col("passthrough")),
                (
                    "city",
                    _col(
                        "joint_mask",
                        provider_config=_joint_cfg("pid", ("city",)),
                        when="keep == 1",
                    ),
                ),
            ],
        )
        # Pre-fix this raised joint_mask_key_by_snapshot_misaligned (snapshot len 2
        # vs gated frame len 1). Post-fix: matched row masked, other untouched.
        assert out["city"][1] == "x"
        assert out["city"][0] != "x"
        # And the matched row keys off its OWN source pid, matching the oracle.
        assert out["city"][0] == oracle_city

"""Cross-column dependency ordering (V1 PR 1): the latent order-dependent read.

The bug (verified at runtime by the Phase-1 gate): `derived`, `group_key`, and
`joint_mask` read their declared sibling drivers from the CURRENT MUTABLE frame,
and non-FK nodes are ordered by a lexicographic column-name tie-break
(`order_work`). So a dependent read a sibling's masked-or-source value by
name-sort ACCIDENT, not by declared data dependency -- a silent wrong-value bug.

PR 1 adds an explicit dependency edge (dependent -> its driver columns) into
`order_work`, so the read is intentional and deterministic, per each strategy's
own documented contract:
  - `derived` (any expression, incl. `case_when`): read drivers MASKED, from the
    live frame under the ordering edge.
  - `group_key`'s `group_by`: read MASKED, from the live frame under the edge.
  - `joint_mask`'s `key_by`: read SOURCE, from a pre-mask snapshot (the
    `date_shift` group-anchor precedent), never the live frame.

These are the plan's acceptance tests 1 and 3 (and the determinism analogues),
written against the real plan -> `PandasExecutionAdapter` path. Each "sorts
before / sorts after" pair renames the columns to flip the lexicographic order;
a correct fix is invariant to it, the buggy main is not.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = b"\xca\xfe" * 16  # 32 bytes


def _col(
    strategy: str,
    *,
    provider_config: tuple[tuple[str, Any], ...] = (),
    namespace: str | None = None,
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
    )


def _plan(per_column: list[tuple[str, ColumnSeed]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=tuple(per_column), per_group=())),),
        )
    )


def _run(
    columns: dict[str, list[Any]],
    per_column: list[tuple[str, ColumnSeed]],
) -> dict[str, list]:
    arrays = {col: pa.array(vals, type=pa.string()) for col, vals in columns.items()}
    table = pa.table(arrays)
    result = PandasExecutionAdapter().run_single(
        _plan(per_column), table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    return {col: result.output.column(col).to_pylist() for col in result.output.column_names}


class TestDerivedMaskedDriverOrderInvariant:
    """Acceptance test 1 (the core regression): a derived column referencing a
    masked driver yields the SAME output whether its name sorts before or after
    the driver, AND that stable output equals the MASKED-driver result."""

    def test_derived_reads_masked_driver_regardless_of_sort_order(self) -> None:
        src = ["ALICE", "BOB", "CAROL"]
        # redact replaces every value with a constant; the masked driver is "Z",
        # so a masked read gives "Z!" for every row and a source read gives
        # "ALICE!"/"BOB!"/"CAROL!" -- unmistakably different.
        driver_cfg = (("redact_with", "Z"),)
        deriv_cfg_a = (("expression", 'concat(a_driver, "!")'),)
        deriv_cfg_z = (("expression", 'concat(z_driver, "!")'),)

        # Variant A: driver name sorts BEFORE the derived column.
        out_a = _run(
            {"a_driver": list(src), "z_out": ["x"] * 3},
            [
                ("a_driver", _col("redact", provider_config=driver_cfg)),
                ("z_out", _col("derived", provider_config=deriv_cfg_a)),
            ],
        )
        # Variant B: driver name sorts AFTER the derived column (this is the
        # case that read the SOURCE value on main).
        out_b = _run(
            {"z_driver": list(src), "a_out": ["x"] * 3},
            [
                ("z_driver", _col("redact", provider_config=driver_cfg)),
                ("a_out", _col("derived", provider_config=deriv_cfg_z)),
            ],
        )

        # Order-independence: the derived output does not depend on column-name sort.
        assert out_a["z_out"] == out_b["a_out"]
        # And it equals the MASKED-driver result, not the source. A fix that
        # deterministically read SOURCE would produce ["ALICE!", ...] and fail here.
        assert out_a["z_out"] == ["Z!", "Z!", "Z!"]
        assert out_b["a_out"] == ["Z!", "Z!", "Z!"]

    def test_derived_case_when_reads_masked_driver(self) -> None:
        # case_when is a construct in the derived grammar (if/else ternary): the
        # value assigned depends on the MASKED driver. masked state is "MX" for
        # every row, so every row takes the "Hispanic" branch; a source read
        # would branch on "CA"/"TX" and yield "other".
        src_state = ["CA", "TX", "NY"]
        when_expr = 'case_when(state == "MX", "Hispanic", "other")'
        out = _run(
            {"state": list(src_state), "ethnicity": ["?"] * 3},
            [
                ("state", _col("redact", provider_config=(("redact_with", "MX"),))),
                ("ethnicity", _col("derived", provider_config=(("expression", when_expr),))),
            ],
        )
        assert out["ethnicity"] == ["Hispanic", "Hispanic", "Hispanic"]


class TestGroupKeyMaskedGroupByOrderInvariant:
    """Acceptance test 3 (group_key half): group_key reads the MASKED group_by
    value deterministically under the ordering edge (per its docstring), not by
    lexicographic luck."""

    def test_group_key_reads_masked_group_by(self) -> None:
        # group_by redacted to one constant -> one group -> one shared key for
        # every row. A source read would see 3 distinct source values -> 3
        # distinct keys. Tested in both sort orders.
        src = ["g1", "g2", "g3"]
        gk_a = (("group_by", "a_group"),)
        gk_z = (("group_by", "z_group"),)

        out_a = _run(
            {"a_group": list(src), "z_key": ["?"] * 3},
            [
                ("a_group", _col("redact", provider_config=(("redact_with", "ONE"),))),
                ("z_key", _col("group_key", provider_config=gk_a)),
            ],
        )
        out_b = _run(
            {"z_group": list(src), "a_key": ["?"] * 3},
            [
                ("z_group", _col("redact", provider_config=(("redact_with", "ONE"),))),
                ("a_key", _col("group_key", provider_config=gk_z)),
            ],
        )
        # Masked group_by is one constant, so one group -> all keys identical.
        assert len(set(out_a["z_key"])) == 1
        assert len(set(out_b["a_key"])) == 1
        # Order-independent AND keyed off the MASKED value (same namespace suffix
        # differs -> the keys themselves differ between runs, but the "one group"
        # invariant is what proves the masked read; a source read would give 3).


class TestJointMaskSourceKeyByOrderInvariant:
    """Acceptance test 3 (joint_mask half): joint_mask reads the SOURCE key_by
    value via the pre-mask snapshot, so the emitted tuple is the same for the
    same SOURCE key regardless of whether key_by was masked or how it sorted."""

    _ZIP_COLS = ["zip", "city", "state"]

    def _joint_cfg(self, key_col: str) -> tuple[tuple[str, Any], ...]:
        return (
            ("columns", self._ZIP_COLS),
            ("reference", "us_zip5_city_state"),
            ("key_by", key_col),
        )

    def _joint_plan(self, key_col: str, key_seed: ColumnSeed) -> list[tuple[str, ColumnSeed]]:
        cols: list[tuple[str, ColumnSeed]] = [(key_col, key_seed)]
        cols += [
            (c, _col("joint_mask", provider_config=self._joint_cfg(key_col)))
            for c in self._ZIP_COLS
        ]
        return cols

    def _tuples(self, out: dict[str, list]) -> list[tuple]:
        return list(zip(*(out[c] for c in self._ZIP_COLS), strict=True))

    def test_joint_mask_reads_source_key_regardless_of_mask_and_order(self) -> None:
        pids = ["P0001", "P0002", "P0003", "P0004"]
        placeholders = {c: ["x"] * len(pids) for c in self._ZIP_COLS}

        # Reference: key_by passthrough (never masked) -> pure source selection.
        ref = _run(
            {"patient_id": list(pids), **placeholders},
            self._joint_plan("patient_id", _col("passthrough")),
        )
        ref_tuples = self._tuples(ref)

        # key_by masked to a constant, name sorts BEFORE the joint columns
        # (city/state/zip): on main this node masks first, so joint_mask read the
        # masked constant -> one identical tuple for every row (the bug).
        redact_const = (("redact_with", "ZZZ"),)
        out_before = _run(
            {"aaa_id": list(pids), **placeholders},
            self._joint_plan("aaa_id", _col("redact", provider_config=redact_const)),
        )
        # key_by masked, name sorts AFTER the joint columns.
        out_after = _run(
            {"zzz_id": list(pids), **placeholders},
            self._joint_plan("zzz_id", _col("redact", provider_config=redact_const)),
        )

        before_tuples = self._tuples(out_before)
        after_tuples = self._tuples(out_after)

        # Source read: both masked-key runs select the SAME tuples as the
        # passthrough-source run, regardless of the mask or the sort order.
        assert before_tuples == ref_tuples
        assert after_tuples == ref_tuples
        # Sanity: the source keys are distinct enough to select >1 distinct tuple,
        # so "equals ref" is not trivially satisfied by a constant.
        assert len(set(ref_tuples)) > 1

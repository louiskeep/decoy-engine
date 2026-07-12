"""Sprint B1b: wire the byte-level estimator (B1a -- `_mem_estimate.py` /
`_mem_estimate_schema.py`) into `decide_execution_route`, flag-gated and
additive (OOM-avoidance routing redesign,
docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §3.1/§3.2/§3.3, and
especially §13's conservative-filter ruling, which this test suite pins as
the routing rule).

Four groups of teeth:

  - `TestFlagOffUnchanged`: `use_byte_estimate_routing` defaults to `False`
    and, when off (or unset), `decide_execution_route` is BYTE-FOR-BYTE the
    pre-B1b row-count logic -- these tests pass a `full_frame_fits_estimate`
    that would flip the outcome if honored, to prove the flag truly gates
    it off rather than merely defaulting a param the code still reads.
  - `TestByteEstimateSignalFunction`: the new
    `_pipeline_routing_signals.byte_estimate_full_frame_fits` signal in
    isolation -- resident-sample pricing, lazy/UNPRICEABLE, and
    fixed-width-only schemas that need no sample at all.
  - `TestFlagOnByteEstimateRouting`: `decide_execution_route` with the flag
    ON, in its scope (relationship-bearing pure-mask jobs) -- comfortably
    fits, doesn't-fit-so-bounded, UNPRICEABLE treated as doesn't-fit, the
    irreducible out_of_core-ineligible-and-too-big reject, a lean numeric
    schema the OLD row-count gate would have mis-routed, and out-of-scope
    jobs (generate+mask, no relationships) staying on the untouched path
    even with the flag on.
  - `TestEndToEndWiring`: drives `run_pipeline` itself (not just the pure
    decision function) to prove the flag threads all the way from the
    public entrypoint, including the "a tight cgroup-style budget shrinks
    what qualifies as full_frame" property via the existing
    `out_of_core_budget_bytes` knob.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import byte_estimate_full_frame_fits
from decoy_engine.profile._types import ColumnProfile, TableProfile
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_MB = 1024 * 1024
_GB = 1024 * _MB

_OUT_OF_CORE_THRESHOLD_DEFAULT = 5_000_000
_FULL_FRAME_REJECT_DEFAULT = 7_500_000


def _col_profile(name: str, *, dtype: str, row_count: int) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=row_count,
        null_count=0,
        distinct_count=row_count,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


class _FakeProfile:
    """Minimal stand-in for the real `Profile`: `decide_execution_route`
    only reads `.relationships`; `byte_estimate_full_frame_fits` only reads
    `.tables`. Using a fake here (rather than the real profiling pipeline)
    keeps these tests at the routing-DECISION layer, matching the existing
    `_FakeProfile` pattern in `test_pipeline_routing.py`."""

    def __init__(
        self,
        *,
        relationships: tuple[Any, ...] = (),
        tables: tuple[TableProfile, ...] = (),
    ) -> None:
        self.relationships = relationships
        self.tables = tables


def _acyclic_graph() -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("id",),
        child_table="child",
        child_columns=("parent_id",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _cyclic_graph() -> RelationshipGraph:
    edge_ab = RelationshipEdge(
        parent_table="a",
        parent_columns=("id",),
        child_table="b",
        child_columns=("ref_a",),
        namespace="na",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    edge_ba = RelationshipEdge(
        parent_table="b",
        parent_columns=("id",),
        child_table="a",
        child_columns=("ref_b",),
        namespace="nb",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    return RelationshipGraph(edges=(edge_ab, edge_ba), ordering=())


# ---------------------------------------------------------------------------
# Flag OFF: byte-for-byte unchanged (SC5 contract). Each case also passes a
# `full_frame_fits_estimate` that would FLIP the outcome if it were honored,
# to prove the flag genuinely gates the estimator off rather than merely
# defaulting a parameter the code still consults.
# ---------------------------------------------------------------------------


class TestFlagOffUnchanged:
    @pytest.mark.parametrize("use_byte_estimate_routing", [False, None])
    def test_eligible_small_job_routes_sequential_regardless_of_estimate(
        self, use_byte_estimate_routing: bool | None
    ) -> None:
        kwargs: dict[str, Any] = {}
        if use_byte_estimate_routing is not None:
            kwargs["use_byte_estimate_routing"] = use_byte_estimate_routing
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=False,
            largest_table_rows=10,
            full_frame_fits_estimate=True,  # would flip to full_frame if honored
            **kwargs,
        )
        assert (route, reason) == ("sequential", "pure_mask_fk")

    def test_eligible_large_compatible_job_routes_out_of_core_by_row_threshold(self) -> None:
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=True,
            largest_table_rows=_OUT_OF_CORE_THRESHOLD_DEFAULT,
            out_of_core_threshold_rows=_OUT_OF_CORE_THRESHOLD_DEFAULT,
            use_byte_estimate_routing=False,
            full_frame_fits_estimate=False,  # would push to bounded/reject if honored -- moot here
        )
        assert (route, reason) == ("out_of_core", "out_of_core_large_fk")

    def test_cyclic_small_job_routes_full_frame_regardless_of_estimate(self) -> None:
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_cyclic_graph(),
            out_of_core_compatible=False,
            largest_table_rows=10,
            use_byte_estimate_routing=False,
            full_frame_fits_estimate=False,  # would push to reject if honored
        )
        assert (route, reason) == ("full_frame", "cross_table_cycle")

    def test_cyclic_large_job_rejects_regardless_of_estimate(self) -> None:
        with pytest.raises(ExecutionError) as exc_info:
            decide_execution_route(
                _FakeProfile(relationships=(object(),)),
                has_generate_table=False,
                has_mask_table=True,
                validators=[],
                fidelity_report=False,
                vault_writer=None,
                execution_mode="auto",
                graph=_cyclic_graph(),
                out_of_core_compatible=False,
                largest_table_rows=_FULL_FRAME_REJECT_DEFAULT,
                full_frame_reject_rows=_FULL_FRAME_REJECT_DEFAULT,
                use_byte_estimate_routing=False,
                full_frame_fits_estimate=True,  # would allow full_frame if honored
            )
        assert exc_info.value.code == "fk_full_frame_oom_risk_rejected"

    def test_no_relationships_always_full_frame_regardless_of_estimate(self) -> None:
        route, reason = decide_execution_route(
            _FakeProfile(relationships=()),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=RelationshipGraph(edges=(), ordering=()),
            use_byte_estimate_routing=False,
            full_frame_fits_estimate=False,
        )
        assert (route, reason) == ("full_frame", "no_relationships")

    def test_generate_plus_mask_large_job_rejects_regardless_of_estimate(self) -> None:
        with pytest.raises(ExecutionError) as exc_info:
            decide_execution_route(
                _FakeProfile(relationships=(object(),)),
                has_generate_table=True,
                has_mask_table=True,
                validators=[],
                fidelity_report=False,
                vault_writer=None,
                execution_mode="auto",
                graph=_acyclic_graph(),
                out_of_core_compatible=False,
                largest_table_rows=_FULL_FRAME_REJECT_DEFAULT,
                full_frame_reject_rows=_FULL_FRAME_REJECT_DEFAULT,
                use_byte_estimate_routing=False,
                full_frame_fits_estimate=True,
            )
        assert exc_info.value.code == "fk_full_frame_oom_risk_rejected"


# ---------------------------------------------------------------------------
# `byte_estimate_full_frame_fits` signal function, in isolation.
# ---------------------------------------------------------------------------


class TestByteEstimateSignalFunction:
    def test_no_mask_tables_returns_none(self) -> None:
        profile = _FakeProfile(tables=())
        result = byte_estimate_full_frame_fits(
            profile, caller_sources={}, table_kinds={}, budget_bytes=1 * _GB
        )
        assert result is None

    def test_resident_variable_width_column_prices_from_the_real_sample(self) -> None:
        table_profile = TableProfile(
            name="t",
            row_count=3,
            columns=(_col_profile("s", dtype="object", row_count=3),),
        )
        profile = _FakeProfile(tables=(table_profile,))
        resident = pa.table({"s": pa.array(["aa", "bb", "cc"])})
        # A big budget: the tiny 3-row table trivially fits.
        result = byte_estimate_full_frame_fits(
            profile,
            caller_sources={"t": resident},
            table_kinds={"t": "mask"},
            budget_bytes=1 * _GB,
        )
        assert result is True

    def test_lazy_table_with_variable_width_column_is_unpriceable(self) -> None:
        table_profile = TableProfile(
            name="t",
            row_count=3,
            columns=(_col_profile("s", dtype="object", row_count=3),),
        )
        profile = _FakeProfile(tables=(table_profile,))
        # No resident source for "t" -- nothing to sample the string column
        # from, so the estimate must be UNPRICEABLE, never a guessed fit.
        result = byte_estimate_full_frame_fits(
            profile,
            caller_sources={},
            table_kinds={"t": "mask"},
            budget_bytes=1 * _GB,
        )
        assert result is None

    def test_fixed_width_only_schema_needs_no_sample_at_all(self) -> None:
        table_profile = TableProfile(
            name="t",
            row_count=1_000,
            columns=(_col_profile("n", dtype="int64", row_count=1_000),),
        )
        profile = _FakeProfile(tables=(table_profile,))
        result = byte_estimate_full_frame_fits(
            profile, caller_sources={}, table_kinds={"t": "mask"}, budget_bytes=1 * _GB
        )
        assert result is True

    def test_non_mask_tables_are_excluded_from_the_estimate(self) -> None:
        mask_table = TableProfile(
            name="m", row_count=10, columns=(_col_profile("n", dtype="int64", row_count=10),)
        )
        generate_table = TableProfile(
            name="g", row_count=10, columns=(_col_profile("bio", dtype="object", row_count=10),)
        )
        profile = _FakeProfile(tables=(mask_table, generate_table))
        # "g" is unpriceable (no sample), but it is classified "generate",
        # not "mask" -- must not poison the mask-only estimate.
        result = byte_estimate_full_frame_fits(
            profile,
            caller_sources={},
            table_kinds={"m": "mask", "g": "generate"},
            budget_bytes=1 * _GB,
        )
        assert result is True

    def test_a_small_and_a_large_budget_flip_the_same_schema(self) -> None:
        rows = 1_000_000
        num_cols = 50
        table_profile = TableProfile(
            name="t",
            row_count=rows,
            columns=tuple(
                _col_profile(f"c{i}", dtype="int64", row_count=rows) for i in range(num_cols)
            ),
        )
        profile = _FakeProfile(tables=(table_profile,))
        # raw = 1,000,000 * 50 * 8 = 400,000,000 B; K=3.0 -> 1,200,000,000 B;
        # margin 1.3x -> 1,560,000,000 B needed to "fit".
        small_budget = 1_000_000_000  # 1 GB < 1.56 GB required
        large_budget = 2_000_000_000  # 2 GB > 1.56 GB required
        assert (
            byte_estimate_full_frame_fits(
                profile, caller_sources={}, table_kinds={"t": "mask"}, budget_bytes=small_budget
            )
            is False
        )
        assert (
            byte_estimate_full_frame_fits(
                profile, caller_sources={}, table_kinds={"t": "mask"}, budget_bytes=large_budget
            )
            is True
        )


# ---------------------------------------------------------------------------
# Flag ON: `decide_execution_route`, in scope (relationship-bearing
# pure-mask jobs) -- the §13 conservative-filter ruling.
# ---------------------------------------------------------------------------


class TestFlagOnByteEstimateRouting:
    def test_comfortably_fits_routes_full_frame_even_at_huge_row_count(self) -> None:
        """A byte estimate that confirms full_frame fits wins outright --
        even at a row count far above BOTH legacy thresholds, proving the
        estimator supersedes the row-count gate entirely when in scope."""
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=True,
            largest_table_rows=50_000_000,
            out_of_core_threshold_rows=_OUT_OF_CORE_THRESHOLD_DEFAULT,
            full_frame_reject_rows=_FULL_FRAME_REJECT_DEFAULT,
            use_byte_estimate_routing=True,
            full_frame_fits_estimate=True,
        )
        assert (route, reason) == ("full_frame", "byte_estimate_full_frame_fits")

    def test_does_not_fit_routes_out_of_core_when_eligible_and_compatible(self) -> None:
        """Does NOT fit, eligible + not cyclic + out_of_core-compatible --
        even at a TINY row count (below the legacy out_of_core threshold),
        proving out_of_core is chosen by ELIGIBILITY, not by the row-count
        threshold, once the flag is on."""
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=True,
            largest_table_rows=10,
            out_of_core_threshold_rows=_OUT_OF_CORE_THRESHOLD_DEFAULT,
            use_byte_estimate_routing=True,
            full_frame_fits_estimate=False,
        )
        assert (route, reason) == ("out_of_core", "byte_estimate_bounded_out_of_core")

    def test_does_not_fit_routes_sequential_when_not_out_of_core_compatible(self) -> None:
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=False,
            largest_table_rows=10,
            use_byte_estimate_routing=True,
            full_frame_fits_estimate=False,
        )
        assert (route, reason) == ("sequential", "pure_mask_fk")

    def test_unpriceable_estimate_is_treated_identically_to_does_not_fit(self) -> None:
        """UNPRICEABLE (`None`) must never be trusted for full_frame
        admission (§13) -- it takes the exact same bounded path as a
        confirmed `False`."""
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=True,
            largest_table_rows=10,
            use_byte_estimate_routing=True,
            full_frame_fits_estimate=None,
        )
        assert (route, reason) == ("out_of_core", "byte_estimate_bounded_out_of_core")

    def test_out_of_core_ineligible_and_does_not_fit_full_frame_still_rejects(self) -> None:
        """The irreducible reject class (§13): a cross-table FK cycle has
        NO bounded route at all -- when the byte estimate also does not
        confirm full_frame fits, this MUST still reject (never silently
        run full_frame), even at a tiny row count that the OLD row-count
        gate would have waved through."""
        with pytest.raises(ExecutionError) as exc_info:
            decide_execution_route(
                _FakeProfile(relationships=(object(),)),
                has_generate_table=False,
                has_mask_table=True,
                validators=[],
                fidelity_report=False,
                vault_writer=None,
                execution_mode="auto",
                graph=_cyclic_graph(),
                out_of_core_compatible=False,
                largest_table_rows=10,  # far below the legacy reject threshold
                full_frame_reject_rows=_FULL_FRAME_REJECT_DEFAULT,
                use_byte_estimate_routing=True,
                full_frame_fits_estimate=False,
            )
        assert exc_info.value.code == "fk_full_frame_oom_risk_rejected"

    def test_not_sequential_eligible_and_does_not_fit_full_frame_rejects(self) -> None:
        """Same irreducible-reject class, reached via `validators_present`
        (not sequential-eligible) instead of a cyclic graph."""
        with pytest.raises(ExecutionError) as exc_info:
            decide_execution_route(
                _FakeProfile(relationships=(object(),)),
                has_generate_table=False,
                has_mask_table=True,
                validators=[{"name": "luhn"}],
                fidelity_report=False,
                vault_writer=None,
                execution_mode="auto",
                graph=_acyclic_graph(),
                out_of_core_compatible=False,
                largest_table_rows=10,
                full_frame_reject_rows=_FULL_FRAME_REJECT_DEFAULT,
                use_byte_estimate_routing=True,
                full_frame_fits_estimate=False,
            )
        assert exc_info.value.code == "fk_full_frame_oom_risk_rejected"

    def test_generate_plus_mask_job_ignores_the_flag_and_estimate_entirely(self) -> None:
        """Out of scope for B1b (see `byte_estimate_full_frame_fits`'s
        docstring): a generate+mask job keeps the OLD row-count logic even
        with the flag on and a `full_frame_fits_estimate` that would flip
        the outcome if honored."""
        route, reason = decide_execution_route(
            _FakeProfile(relationships=(object(),)),
            has_generate_table=True,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=False,
            largest_table_rows=10,  # below the legacy reject threshold
            full_frame_reject_rows=_FULL_FRAME_REJECT_DEFAULT,
            use_byte_estimate_routing=True,
            full_frame_fits_estimate=False,  # would reject if honored
        )
        assert (route, reason) == ("full_frame", "generate_plus_mask")

    def test_no_relationships_job_ignores_the_flag_and_estimate_entirely(self) -> None:
        route, reason = decide_execution_route(
            _FakeProfile(relationships=()),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=RelationshipGraph(edges=(), ordering=()),
            use_byte_estimate_routing=True,
            full_frame_fits_estimate=False,
        )
        assert (route, reason) == ("full_frame", "no_relationships")

    def test_lean_numeric_schema_beyond_row_count_blind_spot_routes_out_of_core(self) -> None:
        """The exact failure class §13 exists to prevent: a schema with a
        TINY row count (far below the legacy `out_of_core_threshold_rows`)
        but wide/lean enough that its real bytes do not comfortably fit
        full_frame. The byte estimate correctly recognizes this and routes
        the properly RAM-capped out_of_core path; the OLD row-count-only
        gate would have picked `sequential` purely because rows were
        small, ignoring the schema's actual width."""
        rows = 10  # far below out_of_core_threshold_rows_default (5,000,000)
        num_cols = 50
        table_profile = TableProfile(
            name="t",
            row_count=rows,
            columns=tuple(
                _col_profile(f"c{i}", dtype="int64", row_count=rows) for i in range(num_cols)
            ),
        )
        profile = _FakeProfile(relationships=(object(),), tables=(table_profile,))
        small_budget = 1  # trivially fails to fit anything but 0 bytes
        estimate = byte_estimate_full_frame_fits(
            profile, caller_sources={}, table_kinds={"t": "mask"}, budget_bytes=small_budget
        )
        assert estimate is False

        route, reason = decide_execution_route(
            profile,
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=True,
            largest_table_rows=rows,
            out_of_core_threshold_rows=_OUT_OF_CORE_THRESHOLD_DEFAULT,
            use_byte_estimate_routing=True,
            full_frame_fits_estimate=estimate,
        )
        assert (route, reason) == ("out_of_core", "byte_estimate_bounded_out_of_core")

        # Contrast: the SAME tiny row count, flag OFF, takes the OLD
        # row-count path straight to `sequential` (never out_of_core,
        # since rows are far below out_of_core_threshold_rows) -- the
        # blind spot the byte estimate closes.
        old_route, old_reason = decide_execution_route(
            profile,
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=_acyclic_graph(),
            out_of_core_compatible=True,
            largest_table_rows=rows,
            out_of_core_threshold_rows=_OUT_OF_CORE_THRESHOLD_DEFAULT,
            use_byte_estimate_routing=False,
        )
        assert (old_route, old_reason) == ("sequential", "pure_mask_fk")


# ---------------------------------------------------------------------------
# End-to-end wiring: `run_pipeline` itself, not just the pure decision
# function -- proves the flag threads from the public entrypoint through
# `resolve_budget` + the mask-table adapters.
# ---------------------------------------------------------------------------


def _faker_col(name: str, namespace: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": namespace,
    }


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _fk_pure_mask_config(tmp_path: Path, n: int = 20) -> dict[str, Any]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "age": pa.array([str(i * 10) for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "b1b-byte-estimate-routing", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    _faker_col("id", "ns"),
                    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                ],
            },
            {"name": "child", "columns": [_faker_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }


def _fk_sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


class TestEndToEndWiring:
    def test_flag_off_default_matches_pre_b1b_behavior(self, tmp_path: Path) -> None:
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0")
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert result.quality_metrics["execution"]["route_reason"] == "pure_mask_fk"

    def test_flag_on_with_generous_budget_routes_full_frame(self, tmp_path: Path) -> None:
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            out_of_core_budget_bytes=1 * _GB,
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert (
            result.quality_metrics["execution"]["route_reason"] == "byte_estimate_full_frame_fits"
        )

    def test_flag_on_with_tight_cgroup_style_budget_routes_bounded_not_full_frame(
        self, tmp_path: Path
    ) -> None:
        """A tight slot budget (standing in for a cgroup-limited container)
        shrinks what qualifies as full_frame. `resolve_budget` floors at 64
        MB regardless of how small a budget is requested, so a job must be
        big enough to clear THAT floor's margin for this to bite -- a
        bigger row count than the other end-to-end tests, still tiny in
        absolute terms, makes the point: the same job that would fit under
        a 1 GB budget must NOT get full_frame under the 64 MB floor."""
        config = _fk_pure_mask_config(tmp_path, n=200_000)
        sources = _fk_sources(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            out_of_core_budget_bytes=64 * 1024 * 1024,  # resolve_budget's own floor
        )
        assert result.quality_metrics["execution"]["execution_mode"] != "full_frame"
        # Not out_of_core-compatible (faker + bucketize aren't in the
        # out-of-core supported-strategy set), so the safe downgrade lands
        # on sequential -- still correct, still bounded, never full_frame.
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"

    def test_end_to_end_output_is_byte_identical_regardless_of_flag(self, tmp_path: Path) -> None:
        """The flag changes ROUTING, never OUTPUT: every route is
        byte-output-neutral versus full_frame by construction (S2)."""
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        off = run_pipeline(config, sources, engine_version="0.1.0")
        on_full_frame = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            out_of_core_budget_bytes=1 * _GB,
        )
        assert off.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert on_full_frame.quality_metrics["execution"]["execution_mode"] == "full_frame"
        for table in off.outputs:
            assert off.outputs[table].equals(on_full_frame.outputs[table]), f"{table} differs"

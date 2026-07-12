"""Sprint B2: wiring the two-point probe (`_probe.py`) into routing --
`_pipeline_routing_signals.resolve_probe_recovery` and
`_pipeline_routing.decide_execution_route`'s `use_probe_routing` /
`probe_recovers_full_frame` params. OOM-avoidance routing redesign,
docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §3.3/§11/§13.

Three groups, mirroring `test_byte_estimate_routing.py`'s structure:

  - `TestDecideExecutionRouteProbeParam`: `decide_execution_route` in
    isolation, with `probe_recovers_full_frame` passed directly -- proves
    the ROUTING rule (recover full_frame ONLY on a confirmed `True`, and
    ONLY when `use_probe_routing` is itself on) without needing a real
    probe run.
  - `TestResolveProbeRecovery`: the signal function in isolation, with
    `_probe.probe_peak_bytes` MONKEYPATCHED -- proves every no-op/skip
    condition (flags off, static estimate already fits, no mask table, a
    non-resident mask table, the static "clearly busts" pre-filter) never
    even calls the probe, and that a real probe call's `uniqueness_risk_
    columns`/verdict thread through correctly.
  - `TestEndToEndWiring`: drives `run_pipeline` itself (not just the pure
    functions), with `_probe.probe_peak_bytes` monkeypatched, mirroring
    `test_byte_estimate_routing.TestEndToEndWiring`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import resolve_probe_recovery
from decoy_engine.execution._probe import (
    DEFAULT_PROBE_TIMEOUT_S,
    MIN_PLAUSIBLE_K_FULL_FRAME,
    ProbePoint,
    ProbeResult,
)
from decoy_engine.profile._types import ColumnProfile, TableProfile
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_MB = 1024 * 1024
_GB = 1024 * _MB


def _col_profile(
    name: str, *, dtype: str, row_count: int, distinct_count: int | None = None
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=row_count,
        null_count=0,
        distinct_count=row_count if distinct_count is None else distinct_count,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


class _FakeProfile:
    """Minimal `Profile` stand-in, matching the pattern in
    `test_byte_estimate_routing.py` / `test_pipeline_routing.py`."""

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


# ---------------------------------------------------------------------------
# `decide_execution_route`, probe param in isolation
# ---------------------------------------------------------------------------


class TestDecideExecutionRouteProbeParam:
    def _route(self, **overrides: Any) -> tuple[str, str]:
        kwargs: dict[str, Any] = dict(
            profile=_FakeProfile(relationships=(object(),)),
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
            full_frame_fits_estimate=False,
        )
        kwargs.update(overrides)
        return decide_execution_route(kwargs.pop("profile"), **kwargs)

    def test_probe_flag_off_ignores_a_true_recovery_signal(self) -> None:
        """Defense-in-depth (mirrors `use_byte_estimate_routing`'s own
        discipline): a `probe_recovers_full_frame=True` passed in with
        `use_probe_routing` OFF must have NO effect."""
        route, reason = self._route(use_probe_routing=False, probe_recovers_full_frame=True)
        assert route != "full_frame"
        assert reason != "probe_recovered_full_frame"

    def test_probe_flag_on_and_recovery_confirmed_routes_full_frame(self) -> None:
        """A CONFIRMED probe recovery wins even over an out_of_core-eligible
        + compatible job -- the measured fast path beats the bounded
        default."""
        route, reason = self._route(use_probe_routing=True, probe_recovers_full_frame=True)
        assert (route, reason) == ("full_frame", "probe_recovered_full_frame")

    def test_probe_flag_on_but_inconclusive_falls_through_to_bounded(self) -> None:
        route, reason = self._route(use_probe_routing=True, probe_recovers_full_frame=None)
        assert (route, reason) == ("out_of_core", "byte_estimate_bounded_out_of_core")

    def test_probe_flag_on_but_confirmed_false_falls_through_to_bounded(self) -> None:
        route, reason = self._route(use_probe_routing=True, probe_recovers_full_frame=False)
        assert (route, reason) == ("out_of_core", "byte_estimate_bounded_out_of_core")

    def test_static_fit_short_circuits_before_the_probe_signal_is_even_consulted(self) -> None:
        """`full_frame_fits_estimate=True` wins outright; a probe recovery
        of `False` alongside it must not matter (the static estimate
        already confirmed the fit -- the probe is a RECOVERY for cases the
        static estimate DIDN'T confirm, not a veto over one that did)."""
        route, reason = self._route(
            full_frame_fits_estimate=True,
            use_probe_routing=True,
            probe_recovers_full_frame=False,
        )
        assert (route, reason) == ("full_frame", "byte_estimate_full_frame_fits")

    def test_probe_recovery_has_no_effect_when_byte_estimate_routing_is_off(self) -> None:
        """`use_probe_routing` composes with (never substitutes for)
        `use_byte_estimate_routing` -- with the latter off, the whole B1b/B2
        branch is unreachable regardless of the probe params."""
        route, reason = self._route(
            use_byte_estimate_routing=False,
            use_probe_routing=True,
            probe_recovers_full_frame=True,
        )
        assert route != "full_frame"
        assert reason != "probe_recovered_full_frame"


# ---------------------------------------------------------------------------
# `resolve_probe_recovery` in isolation -- `_probe.probe_peak_bytes` mocked
# ---------------------------------------------------------------------------


def _never_probe(*args: Any, **kwargs: Any) -> ProbeResult:
    raise AssertionError("probe_peak_bytes must not be called for this case")


class TestResolveProbeRecovery:
    def _numeric_profile(self, *, row_count: int, num_cols: int) -> _FakeProfile:
        table = TableProfile(
            name="t",
            row_count=row_count,
            columns=tuple(
                _col_profile(f"c{i}", dtype="int64", row_count=row_count) for i in range(num_cols)
            ),
        )
        return _FakeProfile(tables=(table,))

    def test_both_flags_off_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _never_probe)
        result = resolve_probe_recovery(
            False,
            False,
            self._numeric_profile(row_count=10, num_cols=1),
            {"t": pa.table({"c0": [1] * 10})},
            {"t": "mask"},
            None,
            False,
            config={},
            engine_version="test",
        )
        assert result is None

    def test_probe_flag_alone_without_byte_estimate_flag_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _never_probe)
        result = resolve_probe_recovery(
            True,
            False,
            self._numeric_profile(row_count=10, num_cols=1),
            {"t": pa.table({"c0": [1] * 10})},
            {"t": "mask"},
            None,
            False,
            config={},
            engine_version="test",
        )
        assert result is None

    def test_static_estimate_already_true_skips_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _never_probe)
        result = resolve_probe_recovery(
            True,
            True,
            self._numeric_profile(row_count=10, num_cols=1),
            {"t": pa.table({"c0": [1] * 10})},
            {"t": "mask"},
            None,
            True,  # full_frame_fits_estimate already True -- nothing to recover
            config={},
            engine_version="test",
        )
        assert result is None

    def test_no_mask_tables_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _never_probe)
        result = resolve_probe_recovery(
            True,
            True,
            _FakeProfile(tables=()),
            {},
            {},
            None,
            False,
            config={},
            engine_version="test",
        )
        assert result is None

    def test_non_resident_mask_table_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lazy (`source_loader`) mask table has no resident data for the
        probe to serialize into a child process -- documented scope limit,
        same as `byte_estimate_full_frame_fits`'s sampling requirement."""
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _never_probe)
        result = resolve_probe_recovery(
            True,
            True,
            self._numeric_profile(row_count=10, num_cols=1),
            {},  # NOT resident
            {"t": "mask"},
            None,
            False,
            config={},
            engine_version="test",
        )
        assert result is None

    def test_clearly_busting_static_estimate_skips_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even under `MIN_PLAUSIBLE_K_FULL_FRAME` (the most favorable real
        full_frame k on record), a wide numeric schema at huge scale still
        busts the (64 MB-floored) slot budget -- no measured k could
        possibly recover this, so the probe cost is not worth paying."""
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _never_probe)
        row_count, num_cols = 1_000_000, 100  # raw = 800,000,000 B
        budget_bytes = 64 * _MB  # resolve_budget's own floor -- an explicit tiny slot budget
        assert row_count * num_cols * 8 * MIN_PLAUSIBLE_K_FULL_FRAME > budget_bytes
        result = resolve_probe_recovery(
            True,
            True,
            self._numeric_profile(row_count=row_count, num_cols=num_cols),
            {"t": pa.table({f"c{i}": [1] for i in range(num_cols)})},
            {"t": "mask"},
            budget_bytes,
            False,
            config={},
            engine_version="test",
        )
        assert result is None

    def test_uniqueness_risk_columns_thread_through_to_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_probe(*args: Any, **kwargs: Any) -> ProbeResult:
            captured.update(kwargs)
            return ProbeResult(conclusive=False, reason="stub")

        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _fake_probe)
        row_count = 1_000
        table = TableProfile(
            name="t",
            row_count=row_count,
            columns=(
                _col_profile("id", dtype="int64", row_count=row_count, distinct_count=row_count),
                _col_profile("code", dtype="int64", row_count=row_count, distinct_count=5),
            ),
        )
        resolve_probe_recovery(
            True,
            True,
            _FakeProfile(tables=(table,)),
            {"t": pa.table({"id": list(range(row_count)), "code": [1] * row_count})},
            {"t": "mask"},
            1 * _GB,
            False,
            config={},
            engine_version="test",
        )
        assert captured["uniqueness_risk_columns"] == (("t", "id"),)
        assert captured["reference_table"] == "t"
        assert captured["target_rows"] == row_count
        # MED-2: an explicit probe-appropriate timeout and a mem_cap (the
        # slot budget is a natural cap) must be threaded through -- never
        # left at the primitive-disabling `None` default.
        assert captured["timeout_s"] == DEFAULT_PROBE_TIMEOUT_S
        assert captured["mem_cap_bytes"] == 1 * _GB
        # MED-1: the raw_bytes physical floor, computed from the SAME
        # profile at full (target) scale -- two int64 columns, 1,000 rows.
        assert captured["raw_floor_bytes"] == row_count * 8 * 2

    def test_conclusive_fit_propagates_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_probe(*args: Any, **kwargs: Any) -> ProbeResult:
            return ProbeResult(
                conclusive=True,
                reason="measured",
                estimated_peak_bytes=1 * _MB,
                slope_bytes_per_row=1.0,
                intercept_bytes=0.0,
                low_point=ProbePoint(rows=1, peak_bytes=1),
                high_point=ProbePoint(rows=2, peak_bytes=2),
            )

        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _fake_probe)
        result = resolve_probe_recovery(
            True,
            True,
            self._numeric_profile(row_count=10, num_cols=1),
            {"t": pa.table({"c0": [1] * 10})},
            {"t": "mask"},
            1 * _GB,
            False,
            config={},
            engine_version="test",
        )
        assert result is True

    def test_inconclusive_probe_propagates_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_probe(*args: Any, **kwargs: Any) -> ProbeResult:
            return ProbeResult(conclusive=False, reason="stub")

        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _fake_probe)
        result = resolve_probe_recovery(
            True,
            True,
            self._numeric_profile(row_count=10, num_cols=1),
            {"t": pa.table({"c0": [1] * 10})},
            {"t": "mask"},
            1 * _GB,
            False,
            config={},
            engine_version="test",
        )
        assert result is None


# ---------------------------------------------------------------------------
# End-to-end through `run_pipeline`
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


def _fk_pure_mask_config(tmp_path: Path, n: int = 200_000) -> dict[str, Any]:
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
        "global_settings": {"job_name": "b2-probe-routing", "seed": 7},
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
    # A tight slot budget (`resolve_budget`'s own 64 MB floor) so the static
    # estimate does NOT confirm full_frame -- the ambiguous band the probe
    # exists to recover, same setup `test_byte_estimate_routing.py`'s tight-
    # budget case uses to prove the static estimate downgrades this job.
    _TIGHT_BUDGET = 64 * 1024 * 1024

    def test_probe_flag_off_matches_byte_estimate_only_behavior(self, tmp_path: Path) -> None:
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            out_of_core_budget_bytes=self._TIGHT_BUDGET,
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"

    def test_probe_confirms_fit_recovers_full_frame(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_probe(*args: Any, **kwargs: Any) -> ProbeResult:
            return ProbeResult(
                conclusive=True,
                reason="measured",
                estimated_peak_bytes=1 * _MB,  # trivially fits any real budget
                slope_bytes_per_row=1.0,
                intercept_bytes=0.0,
                low_point=ProbePoint(rows=1, peak_bytes=1),
                high_point=ProbePoint(rows=2, peak_bytes=2),
            )

        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _fake_probe)
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            use_probe_routing=True,
            out_of_core_budget_bytes=self._TIGHT_BUDGET,
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert result.quality_metrics["execution"]["route_reason"] == "probe_recovered_full_frame"

    def test_probe_inconclusive_still_routes_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_probe(*args: Any, **kwargs: Any) -> ProbeResult:
            return ProbeResult(conclusive=False, reason="stub inconclusive")

        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _fake_probe)
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            use_probe_routing=True,
            out_of_core_budget_bytes=self._TIGHT_BUDGET,
        )
        assert result.quality_metrics["execution"]["execution_mode"] != "full_frame"

    def test_probe_flag_without_byte_estimate_flag_has_no_effect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`use_probe_routing=True` alone (byte-estimate routing OFF) must
        never even call the probe -- and must match the plain legacy
        row-count route exactly."""
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _never_probe)
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0", use_probe_routing=True)
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert result.quality_metrics["execution"]["route_reason"] == "pure_mask_fk"

    def test_output_is_byte_identical_regardless_of_probe_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe changes ROUTING only -- every route is byte-output-
        neutral versus full_frame by construction (S2)."""

        def _fake_probe(*args: Any, **kwargs: Any) -> ProbeResult:
            return ProbeResult(
                conclusive=True,
                reason="measured",
                estimated_peak_bytes=1 * _MB,
                slope_bytes_per_row=1.0,
                intercept_bytes=0.0,
                low_point=ProbePoint(rows=1, peak_bytes=1),
                high_point=ProbePoint(rows=2, peak_bytes=2),
            )

        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        bounded = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            out_of_core_budget_bytes=self._TIGHT_BUDGET,
        )
        monkeypatch.setattr("decoy_engine.execution._probe.probe_peak_bytes", _fake_probe)
        recovered = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=True,
            use_probe_routing=True,
            out_of_core_budget_bytes=self._TIGHT_BUDGET,
        )
        assert bounded.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert recovered.quality_metrics["execution"]["execution_mode"] == "full_frame"
        for table in bounded.outputs:
            assert bounded.outputs[table].equals(recovered.outputs[table]), f"{table} differs"

"""engine-v2 S9 slice 2f: date_shift (derive offset) + formula (V1 safe-eval)."""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x99).to_bytes(8, "big")


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    provider_config: tuple[tuple[str, Any], ...] = (),
    when: str | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=provider_config,
        coherent_with=(),
        when=when,
    )


def _plan(col_name: str, seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=((col_name, seed),), per_group=())),),
        )
    )


def _plan_cols(cols: tuple[tuple[str, ColumnSeed], ...]) -> Any:
    """Multi-column variant of `_plan`, for group_by cases (HC-3a): the
    entity column must be declared with a real strategy too, else the
    fail-closed output-projection gate would warn on it as undeclared."""
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=cols, per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


def _run_sequential(plan: Any, table_name: str, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_sequential(
        plan,
        lambda t: {table_name: table}[t],
        registry=_REG,
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
    )


class TestDateShift:
    def test_shifts_within_range_reproducible_null_preserved(self) -> None:
        src = pa.table({"d": ["2020-01-15", "2020-06-30", None, "2020-01-15"]})
        seed = _col(
            "date_shift",
            namespace="dates",
            deterministic=True,
            provider_config=(("min_days", -10), ("max_days", 10), ("date_format", "%Y-%m-%d")),
        )
        out1 = _run(_plan("d", seed), src).output.column("d").to_pylist()
        out2 = _run(_plan("d", seed), src).output.column("d").to_pylist()
        assert out1 == out2  # reproducible
        assert out1[2] is None  # null preserved
        assert out1[0] == out1[3]  # same source date -> same shift
        shifted = datetime.datetime.strptime(out1[0], "%Y-%m-%d").date()
        assert abs((shifted - datetime.date(2020, 1, 15)).days) <= 10

    def test_requires_namespace(self) -> None:
        src = pa.table({"d": ["2020-01-15"]})
        seed = _col(
            "date_shift",
            namespace=None,
            deterministic=True,
            provider_config=(("date_format", "%Y-%m-%d"),),
        )
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("d", seed), src)
        assert exc.value.code == "date_shift_requires_namespace"


class TestDateShiftGroupBy:
    """HC-3(a): entity-anchored date_shift via `group_by`.

    Standard entity-anchored date-shift de-identification technique (cf.
    HIPAA Safe Harbor date-shift guidance: shift consistently per patient,
    not per event) -- all dates for one entity shift by the SAME offset so
    intra-entity intervals (e.g. admission -> discharge length-of-stay)
    survive the shift.
    """

    def _cols(
        self, *, min_days: int = -100, max_days: int = 100
    ) -> tuple[tuple[str, ColumnSeed], ...]:
        date_seed = _col(
            "date_shift",
            namespace="dates",
            deterministic=True,
            provider_config=(
                ("min_days", min_days),
                ("max_days", max_days),
                ("date_format", "%Y-%m-%d"),
                ("group_by", "patient_id"),
            ),
        )
        entity_seed = _col("passthrough")
        return (("d", date_seed), ("patient_id", entity_seed))

    def test_preserves_intra_patient_intervals_and_differs_across_patients(self) -> None:
        """CORE assertion (HC-3a): two dates for the SAME patient shift by the
        SAME offset (day-delta preserved); different patients get different
        offsets."""
        src = pa.table(
            {
                "d": ["2020-01-01", "2020-01-15", "2019-05-01", "2019-05-20"],
                "patient_id": ["p1", "p1", "p2", "p2"],
            }
        )
        out = _run(_plan_cols(self._cols()), src).output.column("d").to_pylist()
        in_dates = [datetime.date.fromisoformat(v) for v in src.column("d").to_pylist()]
        out_dates = [datetime.datetime.strptime(v, "%Y-%m-%d").date() for v in out]

        # Same patient -> same offset -> the INPUT interval survives in the OUTPUT.
        assert (out_dates[1] - out_dates[0]).days == (in_dates[1] - in_dates[0]).days
        assert (out_dates[3] - out_dates[2]).days == (in_dates[3] - in_dates[2]).days

        # Different patients -> not forced onto the same offset.
        offset_p1 = (out_dates[0] - in_dates[0]).days
        offset_p2 = (out_dates[2] - in_dates[2]).days
        assert offset_p1 != offset_p2

    def test_deterministic_across_runs(self) -> None:
        src = pa.table(
            {
                "d": ["2020-01-01", "2020-01-15", "2019-05-01", "2019-05-20"],
                "patient_id": ["p1", "p1", "p2", "p2"],
            }
        )
        plan = _plan_cols(self._cols())
        out1 = _run(plan, src).output.column("d").to_pylist()
        out2 = _run(plan, src).output.column("d").to_pylist()
        assert out1 == out2

    def test_no_group_by_matches_degenerate_self_group_by(self) -> None:
        """Backwards-compat: group_by absent is byte-identical to the
        pre-HC-3a digest input (the date value itself), which is also what
        you get from the degenerate case group_by == the date column
        itself (each row anchors to its own value either way)."""
        src = pa.table({"d": ["2020-01-15", "2020-06-30", None, "2020-01-15"]})
        base_seed = _col(
            "date_shift",
            namespace="dates",
            deterministic=True,
            provider_config=(("min_days", -10), ("max_days", 10), ("date_format", "%Y-%m-%d")),
        )
        grouped_seed = _col(
            "date_shift",
            namespace="dates",
            deterministic=True,
            provider_config=(
                ("min_days", -10),
                ("max_days", 10),
                ("date_format", "%Y-%m-%d"),
                ("group_by", "d"),
            ),
        )
        out_base = _run(_plan("d", base_seed), src).output.column("d").to_pylist()
        out_grouped = _run(_plan("d", grouped_seed), src).output.column("d").to_pylist()
        assert out_base == out_grouped

    def test_null_group_value_self_anchors_no_crash(self) -> None:
        """Null group_by policy: a null group value falls back to
        self-anchoring on that row's own date value -- deterministic, no
        crash (see _date_shift.py module docstring)."""
        src = pa.table(
            {
                "d": ["2020-01-01", "2020-02-02", "2020-03-03"],
                "patient_id": [None, "p2", None],
            }
        )
        plan = _plan_cols(self._cols())
        out1 = _run(plan, src).output.column("d").to_pylist()
        out2 = _run(plan, src).output.column("d").to_pylist()
        assert out1 == out2  # deterministic
        assert all(v is not None for v in out1)  # no crash, real dates emitted

    def test_null_and_unparseable_rows_unaffected_by_group_by(self) -> None:
        """Null-source passthrough and non-null format_error rows behave
        exactly as without group_by (Sprint 2 honesty pack D7 contract)."""
        src = pa.table(
            {
                "d": ["2020-01-01", None, "not-a-date"],
                "patient_id": ["p1", "p1", "p1"],
            }
        )
        result = _run(_plan_cols(self._cols()), src)
        out = result.output.column("d").to_pylist()
        assert out[1] is None  # null source preserved
        assert out[2] == "not-a-date"  # unparseable cell left untouched (trap T4)
        assert len(result.row_errors) == 1
        assert result.row_errors[0].trigger == "format_error"
        assert result.row_errors[0].row_index == 2

    def test_anchor_is_pre_mask_when_group_column_is_itself_masked(self) -> None:
        """Codex R1 P1 #1 regression (mirrors the masked-anchor repro).

        The group column (patient_id) is itself masked with `hash`, and
        when-gated (`flag == 1`) so only ONE of each patient's two rows is
        rewritten. Node order is lexicographic, so `patient_id` masks BEFORE
        `visit_date` -- on the pre-fix code the date_shift handler read the
        LIVE patient_id, anchoring one row on hash(p1) and its sibling on the
        raw p1, splitting one patient onto two offsets and breaking the
        interval. With the pre-mask snapshot both rows anchor on raw p1, so
        the interval survives. This test FAILS on the pre-fix code.
        """
        src = pa.table(
            {
                "flag": pa.array([1, 0, 1, 0], type=pa.int64()),
                "patient_id": ["p1", "p1", "p2", "p2"],
                "visit_date": ["2020-01-01", "2020-01-15", "2019-05-01", "2019-05-20"],
            }
        )
        date_seed = _col(
            "date_shift",
            namespace="dates",
            deterministic=True,
            provider_config=(
                ("min_days", -100),
                ("max_days", 100),
                ("date_format", "%Y-%m-%d"),
                ("group_by", "patient_id"),
            ),
        )
        # hash masks patient_id in place; the `when` gate rewrites only the
        # flag==1 rows (row0, row2), leaving row1/row3 raw -> the exact
        # split-anchor shape the bug needs.
        pid_seed = _col("hash", namespace="pids", when="flag == 1")
        flag_seed = _col("passthrough")
        plan = _plan_cols(
            (("visit_date", date_seed), ("patient_id", pid_seed), ("flag", flag_seed))
        )
        result = _run(plan, src)
        out = result.output
        masked_pid = out.column("patient_id").to_pylist()
        # Precondition: the scenario is real -- the gated row IS masked, its
        # sibling is NOT, so the live column holds two different values for p1.
        assert masked_pid[0] != "p1"
        assert masked_pid[1] == "p1"
        assert masked_pid[0] != masked_pid[1]

        out_dates = [
            datetime.datetime.strptime(v, "%Y-%m-%d").date()
            for v in out.column("visit_date").to_pylist()
        ]
        in_dates = [datetime.date.fromisoformat(v) for v in src.column("visit_date").to_pylist()]
        # Interval preserved for BOTH patients despite the split mask state.
        assert (out_dates[1] - out_dates[0]).days == (in_dates[1] - in_dates[0]).days
        assert (out_dates[3] - out_dates[2]).days == (in_dates[3] - in_dates[2]).days

    def test_sequential_route_matches_full_frame(self) -> None:
        """The sequential (memory-scaling) runner is SEPARATE wiring from
        run() -- it builds its own pre-mask group anchor snapshot per table.
        Its date_shift+group_by output must be byte-identical to full-frame,
        including the null-group self-anchor row."""
        src = pa.table(
            {
                "d": ["2020-01-01", "2020-01-15", "2019-05-01", "2019-05-20", "2018-03-03"],
                "patient_id": ["p1", "p1", "p2", "p2", None],
            }
        )
        plan = _plan_cols(self._cols())
        full = _run(plan, src).output.column("d").to_pylist()
        seq = _run_sequential(plan, "t", src).outputs["t"].column("d").to_pylist()
        assert seq == full

    def test_nullable_int_group_column_no_crash(self) -> None:
        """Codex R1 P1 #2 regression: an integer group column that carries a
        null must not widen to float64 on ingestion (which would make
        `_canonicalize_source` reject the valid ids). No crash; deterministic;
        same-int rows share an offset; the null row self-anchors."""
        src = pa.table(
            {
                "d": ["2020-01-01", "2020-02-02", "2020-03-03", "2020-01-20"],
                "patient_id": pa.array([1, None, 2, 1], type=pa.int64()),
            }
        )
        date_seed = _col(
            "date_shift",
            namespace="dates",
            deterministic=True,
            provider_config=(
                ("min_days", -100),
                ("max_days", 100),
                ("date_format", "%Y-%m-%d"),
                ("group_by", "patient_id"),
            ),
        )
        plan = _plan_cols((("d", date_seed), ("patient_id", _col("passthrough"))))
        out1 = _run(plan, src).output.column("d").to_pylist()
        out2 = _run(plan, src).output.column("d").to_pylist()
        assert out1 == out2  # deterministic, no crash

        in_dates = [datetime.date.fromisoformat(v) for v in src.column("d").to_pylist()]
        out_dates = [datetime.datetime.strptime(v, "%Y-%m-%d").date() for v in out1]
        # Rows 0 and 3 share patient_id == 1 -> same offset -> interval preserved.
        assert (out_dates[3] - out_dates[0]).days == (in_dates[3] - in_dates[0]).days


class TestFormula:
    def test_applies_expression_preserves_null(self) -> None:
        src = pa.table({"n": [1, 2, None]})
        seed = _col("formula", provider_config=(("formula", "value * 2"),))
        out = _run(_plan("n", seed), src).output.column("n").to_pylist()
        assert float(out[0]) == 2.0
        assert float(out[1]) == 4.0
        assert out[2] is None


class TestGroupAnchorSnapshotMisalignedFailsClosed:
    """Dennis R2 LOW-1 backstop: if a snapshot ever fails to row-align with the
    frame being masked (a future/unknown route handing a filtered or reordered
    frame), the handler FAILS CLOSED instead of silently mis-anchoring. On every
    supported route the indexes are identical by construction; this proves the
    guard fires when that invariant is violated."""

    def test_misaligned_snapshot_index_raises(self) -> None:
        import pandas as pd

        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.execution._errors import StrategyError
        from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
        from decoy_engine.generation.pool._cache import PoolCache

        seed = _col(
            "date_shift",
            namespace="ds",
            provider_config=(("min_days", -30), ("max_days", 30), ("group_by", "pid")),
        )
        df = pd.DataFrame({"d": ["2020-01-01", "2020-02-01", "2020-03-01"]})  # index 0,1,2
        ctx = StrategyContext(
            registry=None,  # type: ignore[arg-type]
            pool_cache=PoolCache(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
            job_seed=(0x1).to_bytes(8, "big"),
        )
        object.__setattr__(ctx, "current_table", "t")
        # A snapshot whose index does NOT match the frame's (0,1,2) -> misaligned.
        object.__setattr__(
            ctx,
            "group_anchor_snapshots",
            {("t", "pid"): pd.Series(["p1", "p1", "p2"], index=[10, 11, 12])},
        )
        with pytest.raises(StrategyError) as exc:
            DateShiftStrategyHandler().run(df, "d", seed, ctx)
        assert exc.value.code == "date_shift_group_anchor_snapshot_misaligned"

    def test_missing_snapshot_raises(self) -> None:
        import pandas as pd

        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.execution._errors import StrategyError
        from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
        from decoy_engine.generation.pool._cache import PoolCache

        seed = _col(
            "date_shift",
            namespace="ds",
            provider_config=(("min_days", -30), ("max_days", 30), ("group_by", "pid")),
        )
        df = pd.DataFrame({"d": ["2020-01-01", "2020-02-01"]})
        ctx = StrategyContext(
            registry=None,  # type: ignore[arg-type]
            pool_cache=PoolCache(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
            job_seed=(0x1).to_bytes(8, "big"),
        )
        object.__setattr__(ctx, "current_table", "t")  # no snapshot populated
        with pytest.raises(StrategyError) as exc:
            DateShiftStrategyHandler().run(df, "d", seed, ctx)
        assert exc.value.code == "date_shift_group_anchor_snapshot_missing"


class TestBooleanAnchorNumpyPythonParity:
    """Codex R3 P1: a boolean (or other numpy-materialized) group_by anchor must
    hash identically whether it arrives as a numpy scalar (null-free numpy-backed
    chunk) or a Python scalar (nullable full frame / polars snapshot). Otherwise
    the same anchor shifts differently across chunk boundaries and substrates."""

    def _run_with_snapshot(self, snapshot):
        import pandas as pd

        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
        from decoy_engine.generation.pool._cache import PoolCache

        seed = _col(
            "date_shift",
            namespace="ds",
            provider_config=(("min_days", -100), ("max_days", 100), ("group_by", "flag")),
        )
        df = pd.DataFrame({"d": ["2020-01-01", "2020-06-01", "2020-12-01"]})
        ctx = StrategyContext(
            registry=None,  # type: ignore[arg-type]
            pool_cache=PoolCache(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
            job_seed=(0x7).to_bytes(8, "big"),
        )
        object.__setattr__(ctx, "current_table", "t")
        object.__setattr__(ctx, "group_anchor_snapshots", {("t", "flag"): snapshot})
        out, _ = DateShiftStrategyHandler().run(df, "d", seed, ctx)
        return list(out["d"])

    def test_numpy_bool_matches_python_bool_anchor(self) -> None:
        import pandas as pd

        # numpy.bool_ elements (bool dtype) vs Python bool elements (object dtype),
        # same logical values + same index.
        numpy_snap = pd.Series([True, False, True], dtype=bool)
        python_snap = pd.Series([True, False, True], dtype=object)
        assert self._run_with_snapshot(numpy_snap) == self._run_with_snapshot(python_snap)

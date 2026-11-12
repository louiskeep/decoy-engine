"""The generation capacity floor: a resident generate run is refused when its output cannot fit.

`generation_floor_bytes` is a lower bound on what a resident run must allocate, compared with the
whole memory ceiling (not the 25% planning budget), so it only refuses runs that cannot fit. Both
public callers, `run_generation` and `estimate_job_capacity`, use the one function.
"""

from __future__ import annotations

import ast
import gc
import time
import tracemalloc
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine import reasons
from decoy_engine.execution import capacity as capacity_mod
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._probe_scale import downscale_config
from decoy_engine.execution.capacity import estimate_job_capacity
from decoy_engine.execution.out_of_core import _budget as ooc_budget
from decoy_engine.execution.out_of_core._capacity_eval import CapacityVerdict
from decoy_engine.generation import _bounds as bounds_mod
from decoy_engine.generation import _column_streams, _faker_pool, synthesize
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._bounds import generation_floor_bytes
from decoy_engine.generation._budget_ledger import GenerationBudgetExceeded, GenerationBudgetLedger
from decoy_engine.generation._streamed import run_generation
from tests.parity import _f0_fixtures as fx
from tests.parity._fr0_support import prepared_config

MIB = 1024**2
GIB = 1024**3
HUGE_ROWS = 5_000_000_000
SECRET_PREFIX = "SECRET-PREFIX-"
FORMULA = {"name": "legacy", "type": "formula", "formula": "i"}


def _patch_detection(mp: pytest.MonkeyPatch, detect: Any) -> None:
    mp.setattr(ooc_budget, "detect_effective_memory_bytes", detect)


def _patch_memory(mp: pytest.MonkeyPatch, nbytes: int) -> None:
    _patch_detection(mp, lambda: nbytes)


def _patch_memory_undetectable(mp: pytest.MonkeyPatch) -> None:
    def fail() -> int:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_MEMORY_DETECTION_FAILED, message="no memory source"
        )

    _patch_detection(mp, fail)


def _huge_config() -> dict[str, Any]:
    cols = [
        {"name": "id", "type": "sequence", "prefix": SECRET_PREFIX},
        {"name": "who", "type": "faker", "faker_type": "city"},
    ]
    return fx.config([fx.table("big", HUGE_ROWS, cols)])


class FloorSpy:
    """Wraps the real floor so a test can tell whether a caller reached it."""

    def __init__(self, mp: pytest.MonkeyPatch, *modules: Any) -> None:
        self.calls = 0
        real = generation_floor_bytes

        def spy(config: dict[str, Any], admission: Any) -> int:
            self.calls += 1
            return real(config, admission)

        for module in modules:
            mp.setattr(module, "generation_floor_bytes", spy)


class ProviderSpies:
    """Counts every way a Faker provider can be reached on any path."""

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.count = 0

        def wrap(real: Any) -> Any:
            def spy(*a: Any, **k: Any) -> Any:
                self.count += 1
                return real(*a, **k)

            return spy

        mp.setattr(
            _column_streams, "get_faker_providers", wrap(_column_streams.get_faker_providers)
        )
        mp.setattr(synthesize, "get_faker_providers", wrap(synthesize.get_faker_providers))
        mp.setattr(_faker_pool, "build_pool_values", wrap(_faker_pool.build_pool_values))
        mp.setattr(
            _column_streams.PerRowFakerStream,
            "next_batch",
            wrap(_column_streams.PerRowFakerStream.next_batch),
        )


class BoundedSink:
    """Carries the bounded marker and records the calls made on it."""

    bounded_write_batches = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.calls.append("write_batches")
        for _ in batches:
            pass

    def commit(self) -> None:
        self.calls.append("commit")

    def abort(self) -> None:
        self.calls.append("abort")


# ---- AT-9 (a): the floor never exceeds what the run allocates ---------------------------------

_COLUMN_MENU: dict[str, dict[str, Any]] = {
    "sequence": {"type": "sequence", "start": 1},
    "categorical_str": {"type": "categorical", "categories": ["a", "bb", "ccc"]},
    "categorical_bool": {"type": "categorical", "categories": [True, False]},
    "categorical_int": {"type": "categorical", "categories": [1, 2, 3]},
    "categorical_float": {"type": "categorical", "categories": [1.5, 2.5]},
    "faker": {"type": "faker", "faker_type": "city"},
    "reference": {"type": "reference", "reference_table": "parent", "reference_column": "k"},
}


def _property_config(kinds: list[str], rows: int, legacy: bool) -> dict[str, Any]:
    cols = [{"name": f"c{i}", **_COLUMN_MENU[kind]} for i, kind in enumerate(kinds)]
    if legacy:
        cols.append(dict(FORMULA))
    parent = fx.table("parent", max(rows, 1), [{"name": "k", "type": "sequence", "start": 1}])
    return prepared_config(fx.config([parent, fx.table("child", rows, cols)]))


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(
    kinds=st.lists(st.sampled_from(sorted(_COLUMN_MENU)), min_size=1, max_size=5),
    rows=st.integers(min_value=0, max_value=2_000),
    legacy=st.booleans(),
)
def test_floor_is_at_most_the_measured_allocation_of_its_own_part(
    kinds: list[str], rows: int, legacy: bool
) -> None:
    config = _property_config(kinds, rows, legacy)
    admission = classify_plan(config)
    assert admission.eligible is not legacy
    floor = generation_floor_bytes(config, admission)
    gc.collect()
    if legacy:
        # The legacy part is the Python list slots, so the Python allocator peak is its measure.
        tracemalloc.start()
        try:
            result = run_generation(config)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert result.mode == "resident"
        assert floor <= peak
    else:
        # The Arrow part is the column buffers, which the resident output still holds when the
        # run returns, so the live Arrow allocation is at most the peak and at least the floor.
        before = pa.total_allocated_bytes()
        result = run_generation(config)
        held = pa.total_allocated_bytes() - before
        assert result.tables is not None
        assert floor <= held


# ---- AT-9 (b): refused before any provider, naming sizes and the table only -------------------


@pytest.mark.serial
def test_over_memory_run_is_refused_fast_before_any_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_memory(monkeypatch, GIB)
    spies = ProviderSpies(monkeypatch)
    started = time.perf_counter()
    with pytest.raises(GenerationBudgetExceeded) as err:
        run_generation(_huge_config())
    assert time.perf_counter() - started < 1.0
    assert err.value.code == "generation_budget_exceeded"
    assert spies.count == 0, "a provider ran before the floor refused the plan"
    text = str(err.value)
    assert "big" in text and str(GIB) in text
    assert SECRET_PREFIX not in text and "city" not in text


# ---- AT-9 (c): compared with the whole ceiling, not the 25% planning budget -------------------


def test_a_table_needing_sixty_percent_of_memory_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    config = fx.config([fx.table("t", 10_000, [{"name": "k", "type": "sequence"}])])
    floor = generation_floor_bytes(config, classify_plan(config))
    assert floor == 8 * 10_000
    _patch_memory(monkeypatch, int(floor / 0.6))
    result = run_generation(config)
    assert result.tables is not None and result.tables["t"].num_rows == 10_000


def test_the_floor_refuses_only_above_the_memory_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    config = fx.config([fx.table("t", 10_000, [{"name": "k", "type": "sequence"}])])
    floor = generation_floor_bytes(config, classify_plan(config))
    _patch_memory(monkeypatch, floor)
    assert run_generation(config).tables is not None
    _patch_memory(monkeypatch, floor - 1)
    with pytest.raises(GenerationBudgetExceeded):
        run_generation(config)


# ---- AT-9 (d), (e): which runs reach the floor ------------------------------------------------


def test_floor_is_not_called_when_a_budget_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = FloorSpy(monkeypatch, bounds_mod)
    config = fx.config([fx.table("t", 50, [{"name": "k", "type": "sequence"}])])
    assert run_generation(config, budget_bytes=256 * 1024 * 1024).tables is not None
    assert spy.calls == 0


def test_floor_is_not_called_with_a_bounded_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = FloorSpy(monkeypatch, bounds_mod)
    config = fx.config([fx.table("t", 50, [{"name": "k", "type": "sequence"}])])
    ledger = GenerationBudgetLedger(256 * MIB)
    assert run_generation(config, ledger=ledger).tables is not None
    assert spy.calls == 0


def test_eligible_plan_with_a_bounded_sink_does_not_call_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = FloorSpy(monkeypatch, bounds_mod)
    _patch_memory(monkeypatch, 1)
    sink = BoundedSink()
    config = fx.config([fx.table("t", 50, [{"name": "k", "type": "sequence"}])])
    result = run_generation(config, sink=sink)
    assert result.mode == "streamed" and sink.calls and spy.calls == 0


def test_legacy_plan_with_a_bounded_sink_still_calls_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = FloorSpy(monkeypatch, bounds_mod)
    sink = BoundedSink()
    config = prepared_config(
        fx.config([fx.table("t", 50, [{"name": "k", "type": "sequence"}, dict(FORMULA)])])
    )
    result = run_generation(config, sink=sink)
    assert result.mode == "resident" and sink.calls == [] and spy.calls == 1
    _patch_memory(monkeypatch, 1)
    with pytest.raises(GenerationBudgetExceeded):
        run_generation(config, sink=sink)


def test_resident_eligible_plan_calls_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = FloorSpy(monkeypatch, bounds_mod)
    config = fx.config([fx.table("t", 50, [{"name": "k", "type": "sequence"}])])
    assert run_generation(config).tables is not None
    assert spy.calls == 1


# ---- AT-9 (f): the probe route prices the scaled row count ------------------------------------


def test_probe_route_uses_the_scaled_row_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_memory(monkeypatch, GIB)
    config = _huge_config()
    scaled = downscale_config(config, 1e-9)
    scaled_rows = scaled["tables"][0]["row_count"]
    assert scaled_rows < HUGE_ROWS
    assert generation_floor_bytes(scaled, classify_plan(scaled)) < GIB
    assert generation_floor_bytes(config, classify_plan(config)) > GIB
    result = run_generation(scaled)
    assert result.tables is not None and result.tables["big"].num_rows == scaled_rows
    with pytest.raises(GenerationBudgetExceeded):
        run_generation(config)


# ---- AT-9 (g): the capacity estimate ----------------------------------------------------------


def _profile_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the floor must answer before any source is profiled")

    monkeypatch.setattr("decoy_engine.profile.profile_source", refuse)


@pytest.mark.serial
def test_estimate_reports_the_floor_as_insufficient_without_profiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_memory(monkeypatch, GIB)
    _profile_must_not_run(monkeypatch)
    config = _huge_config()
    est = estimate_job_capacity(config, tmp_path)
    assert est.verdict is CapacityVerdict.INSUFFICIENT
    assert est.code == "generation_budget_exceeded"
    assert est.available_bytes == GIB
    assert est.needed_bytes == generation_floor_bytes(config, classify_plan(config))
    assert isinstance(est.needed_bytes, int) and est.needed_bytes > GIB
    assert SECRET_PREFIX not in est.message and "big" in est.message


def test_estimate_honors_an_explicit_budget_over_detected_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def must_not_detect() -> int:
        raise AssertionError("an explicit budget replaces detection")

    _patch_detection(monkeypatch, must_not_detect)
    _profile_must_not_run(monkeypatch)
    config = fx.config([fx.table("t", 20_000_000, [{"name": "k", "type": "sequence"}])])
    est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * MIB)
    assert est.verdict is CapacityVerdict.INSUFFICIENT
    assert est.code == "generation_budget_exceeded"
    assert est.available_bytes == 64 * MIB and est.needed_bytes == 160_000_000


def test_estimate_resolves_a_small_budget_the_way_the_run_does(tmp_path: Path) -> None:
    # The run raises a budget below the route minimum to that minimum, so the estimate must too,
    # or it refuses a job the run accepts.
    config = fx.config([fx.table("t", 300_000, [{"name": "k", "type": "sequence"}])])
    assert generation_floor_bytes(config, classify_plan(config)) > 1_000_000
    est = estimate_job_capacity(config, tmp_path, budget_bytes=1_000_000)
    assert est.code != "generation_budget_exceeded"
    assert run_generation(config, budget_bytes=1_000_000).tables is not None


def test_estimate_rejects_a_non_positive_budget_as_the_run_does(tmp_path: Path) -> None:
    config = fx.config([fx.table("t", 10, [{"name": "k", "type": "sequence"}])])
    with pytest.raises(ExecutionError) as err:
        estimate_job_capacity(config, tmp_path, budget_bytes=0)
    assert err.value.code == "out_of_core_budget_invalid"


def test_estimate_within_the_floor_falls_through_to_the_existing_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_memory(monkeypatch, GIB)
    config = fx.config([fx.table("t", 100, [{"name": "k", "type": "sequence"}])])
    est = estimate_job_capacity(config, tmp_path)
    assert est.verdict is CapacityVerdict.NOT_APPLICABLE and est.code is None


def test_mask_only_estimate_never_reaches_the_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = FloorSpy(monkeypatch, bounds_mod)
    config = {
        "version": 2,
        "global_settings": {"job_name": "no-fk", "seed": 1},
        "sources": {},
        "targets": {},
        "tables": [],
    }
    est = estimate_job_capacity(config, tmp_path)
    assert spy.calls == 0
    assert est.verdict is CapacityVerdict.NOT_APPLICABLE
    assert est.code is None and est.route == "none"
    assert est.needed_bytes is None and est.available_bytes is None


# ---- AT-9 (h): an undetectable host fails open ------------------------------------------------


def test_run_is_unchanged_when_memory_cannot_be_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_memory_undetectable(monkeypatch)
    config = fx.config([fx.table("t", 50, [{"name": "k", "type": "sequence"}])])
    result = run_generation(config)
    assert result.tables is not None and result.tables["t"].num_rows == 50


def test_estimate_is_unknown_when_memory_cannot_be_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_memory_undetectable(monkeypatch)
    _profile_must_not_run(monkeypatch)
    est = estimate_job_capacity(_huge_config(), tmp_path)
    assert est.verdict is CapacityVerdict.UNKNOWN
    assert est.needed_bytes is None and est.available_bytes is None


# ---- AT-9 (i): integer byte counts, including a boolean column --------------------------------


def test_boolean_column_floor_is_an_integer_ceil_division(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = 5_000_000_001
    cols = [
        {"name": "flag", "type": "categorical", "categories": [True, False]},
        {"name": "id", "type": "sequence"},
    ]
    config = fx.config([fx.table("t", rows, cols)])
    expected = -(-rows // 8) + 8 * rows
    assert generation_floor_bytes(config, classify_plan(config)) == expected
    _patch_memory(monkeypatch, GIB)
    est = estimate_job_capacity(config, tmp_path)
    assert est.verdict is CapacityVerdict.INSUFFICIENT
    assert type(est.needed_bytes) is int and est.needed_bytes == expected
    assert type(est.available_bytes) is int


# ---- Floor arithmetic per route ---------------------------------------------------------------


def test_floor_by_column_type() -> None:
    cols = [
        {"name": "s", "type": "sequence"},
        {"name": "i", "type": "categorical", "categories": [1, 2]},
        {"name": "f", "type": "categorical", "categories": [1.5]},
    ]
    config = fx.config([fx.table("t", 1000, cols)])
    assert generation_floor_bytes(config, classify_plan(config)) == 8 * 1000 + 8 * 1000 + 8 * 1000


def test_floor_counts_eight_bytes_per_cell_when_the_plan_runs_the_legacy_route() -> None:
    cols = [{"name": "s", "type": "sequence"}, dict(FORMULA)]
    config = fx.config([fx.table("t", 1000, cols)])
    admission = classify_plan(config)
    assert not admission.eligible
    assert generation_floor_bytes(config, admission) == 8 * 2 * 1000


def test_legacy_floor_takes_the_largest_table_because_each_table_list_is_freed() -> None:
    small = fx.table("a", 100, [dict(FORMULA)])
    large = fx.table("b", 1000, [dict(FORMULA), {"name": "s", "type": "sequence"}])
    config = fx.config([small, large])
    assert generation_floor_bytes(config, classify_plan(config)) == 8 * 2 * 1000


def test_a_repeated_column_name_is_priced_once() -> None:
    # Legacy generation keeps columns in a dict by name, so only one of the three stays alive.
    flag = {"name": "flag", "type": "categorical", "categories": [True, False]}
    config = fx.config([fx.table("t", 1000, [dict(flag), dict(flag), dict(flag)])])
    admission = classify_plan(config)
    assert not admission.eligible
    assert generation_floor_bytes(config, admission) == 8 * 1000


def test_eligible_floor_sums_every_resident_table() -> None:
    config = fx.config(
        [
            fx.table("a", 100, [{"name": "s", "type": "sequence"}]),
            fx.table("b", 1000, [{"name": "s", "type": "sequence"}]),
        ]
    )
    assert generation_floor_bytes(config, classify_plan(config)) == 8 * 1100


def test_mask_tables_and_empty_plans_have_no_floor() -> None:
    config = fx.config([{"name": "m", "columns": []}])
    assert generation_floor_bytes(config, classify_plan(config)) == 0


# ---- the capacity reason-code set is pinned in tests/unit/test_capacity_reason_codes.py ------


def _private_imports(path: str, package: str) -> list[str]:
    """Every import in `path` that names a private module or member under `package`."""
    names: list[str] = []
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    prefix = package.split(".")
    return [
        name
        for name in names
        if name.split(".")[: len(prefix)] == prefix
        and any(part.startswith("_") for part in name.split(".")[len(prefix) :])
    ]


def test_capacity_reaches_the_floor_only_through_the_generation_package() -> None:
    assert _private_imports(capacity_mod.__file__, "decoy_engine.generation") == []


def test_the_import_guard_sees_every_spelling(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    for line in (
        "from decoy_engine.generation import _bounds",
        "import decoy_engine.generation._bounds",
        "from decoy_engine.generation._bounds import x",
    ):
        probe.write_text(line + "\n", encoding="utf-8")
        assert _private_imports(str(probe), "decoy_engine.generation") != [], line

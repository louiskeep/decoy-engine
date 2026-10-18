"""Phase F FR0: the byte budget is enforced by the library, before anything is allocated.

Direct-library assertions (no bench supervisor): the ledger refuses a stage BEFORE its allocation,
a column with no defensible bound is rejected on every path before a provider runs, a decline
leaves the sink untouched, and an over-ceiling run raises instead of emitting different values.
The external RSS bench is containment evidence only; none of these tests start a subprocess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.generation import _column_streams, _faker_pool, _streamed, synthesize
from decoy_engine.generation._bounds import FAKER_VALUE_CAPS, compute_payload_bounds
from decoy_engine.generation._budget_ledger import (
    GenerationBudgetExceeded,
    GenerationBudgetLedger,
    GenerationUnboundedError,
)
from decoy_engine.generation._streamed import run_generation
from tests.parity import _f0_fixtures as fx
from tests.parity._fr0_support import fingerprints, prepared_config

MIB = 1024 * 1024
UNBOUNDED_TEXT = {
    "name": "wide",
    "type": "faker",
    "faker_type": "text",
    "faker_kwargs": {"max_nb_chars": 10**9},
}


class SpySink:
    """Carries the bounded marker and records every call; real writes are never made."""

    bounded_write_batches = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, table: str, data: pa.Table) -> None:
        self.calls.append("write")

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.calls.append("write_batches")
        for _ in batches:
            pass

    def commit(self) -> None:
        self.calls.append("commit")

    def abort(self) -> None:
        self.calls.append("abort")


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


def _cfg_with_wide(extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cols = [{"name": "k", "type": "sequence"}, UNBOUNDED_TEXT, *(extra or [])]
    return prepared_config(fx.config([fx.table("t", 50, cols)]))


# ---- ledger -----------------------------------------------------------------------------------


def test_ledger_reserves_releases_and_tracks_peak() -> None:
    ledger = GenerationBudgetLedger(100)
    a = ledger.reserve("a", 60)
    with ledger.reserve("b", 40) as b:
        assert ledger.charged_bytes == 100 and ledger.remaining_bytes == 0 and b.nbytes == 40
    assert ledger.charged_bytes == 60
    a.release()
    a.release()  # idempotent
    assert ledger.charged_bytes == 0 and ledger.peak_bytes == 100
    assert [e[0] for e in ledger.events] == ["a", "b"]


def test_ledger_refuses_before_charging() -> None:
    ledger = GenerationBudgetLedger(100)
    ledger.reserve("a", 70)
    with pytest.raises(GenerationBudgetExceeded) as err:
        ledger.reserve("b", 31)
    assert err.value.code == "generation_budget_exceeded" and "'b'" in str(err.value)
    assert ledger.charged_bytes == 70, "a refused reservation must not be charged"


def test_unbounded_ledger_counts_but_never_refuses() -> None:
    ledger = GenerationBudgetLedger(None)
    ledger.reserve("big", 10**15)
    assert not ledger.bounded and ledger.remaining_bytes is None and ledger.peak_bytes == 10**15


# ---- GenerationUnboundedError: every path, before any provider, zero writes -------------------


@pytest.mark.parametrize("path", ["streamed", "resident", "legacy"])
def test_unbounded_column_is_rejected_on_every_path_before_a_provider_runs(
    path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extra = [{"name": "f", "type": "formula", "formula": "i"}] if path == "legacy" else None
    config = _cfg_with_wide(extra)
    spies = ProviderSpies(monkeypatch)
    sink = SpySink() if path == "streamed" else None
    with pytest.raises(GenerationUnboundedError) as err:
        run_generation(config, sink=sink, budget_bytes=256 * MIB)
    assert "t.wide" in str(err.value)
    assert spies.count == 0, "a provider was invoked before the plan was rejected"
    assert sink is None or sink.calls == [], "the sink was touched"
    assert list(tmp_path.iterdir()) == []


def test_unbounded_rejection_through_the_public_entry_with_a_real_sink(tmp_path: Path) -> None:
    from decoy_engine.execution import ParquetTransactionalSink
    from decoy_engine.generation._plan_entry import generate_tables_streamed
    from tests.parity._fr0_support import plan_of

    plan = plan_of(fx.config([fx.table("t", 5, [UNBOUNDED_TEXT])]))
    target = tmp_path / "out"
    with pytest.raises(GenerationUnboundedError):
        generate_tables_streamed(
            plan, sink=ParquetTransactionalSink(target), budget_bytes=256 * MIB
        )
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_formula_has_no_bound_so_a_bounded_legacy_plan_with_one_is_rejected() -> None:
    cfg = prepared_config(
        fx.config([fx.table("t", 5, [{"name": "f", "type": "formula", "formula": "i"}])])
    )
    with pytest.raises(GenerationUnboundedError):
        run_generation(cfg, budget_bytes=256 * MIB)


def test_reference_to_an_unbounded_parent_key_is_unbounded() -> None:
    """An unknowable parent key width means an unknowable parent key pool."""
    cfg = prepared_config(
        fx.config(
            [
                fx.table("p", 5, [{**UNBOUNDED_TEXT, "name": "k"}]),
                fx.table(
                    "c",
                    5,
                    [
                        {
                            "name": "r",
                            "type": "reference",
                            "reference_table": "p",
                            "reference_column": "k",
                        }
                    ],
                ),
            ]
        )
    )
    bounds = compute_payload_bounds(cfg)
    assert bounds[("p", "k")] is None and bounds[("c", "r")] is None
    with pytest.raises(GenerationUnboundedError) as err:
        run_generation(cfg, budget_bytes=256 * MIB)
    assert "c.r" in str(err.value) and "p.k" in str(err.value)


def test_no_budget_is_the_explicit_unbounded_mode_and_still_runs_such_a_column() -> None:
    """kwargs make the width unknowable, not large: the same column runs when no ceiling is asked."""
    short = {**UNBOUNDED_TEXT, "faker_kwargs": {"max_nb_chars": 20}}
    config = prepared_config(fx.config([fx.table("t", 50, [short])]))
    assert compute_payload_bounds(config)[("t", "wide")] is None
    out = run_generation(config).tables
    assert out is not None and out["t"].num_rows == 50
    with pytest.raises(GenerationUnboundedError):
        run_generation(config, budget_bytes=256 * MIB)


def test_bounds_are_exact_or_defensible() -> None:
    cfg = prepared_config(
        fx.config(
            [
                fx.table(
                    "t",
                    1000,
                    [
                        {
                            "name": "s",
                            "type": "sequence",
                            "start": 1,
                            "step": 1,
                            "prefix": "AB",
                            "pad_length": 8,
                        },
                        {"name": "c", "type": "categorical", "categories": ["a", "bcd"]},
                        {"name": "i", "type": "categorical", "categories": [1, 2]},
                        {"name": "f", "type": "faker", "faker_type": "city"},
                        {
                            "name": "p",
                            "type": "faker",
                            "faker_type": "pyint",
                            "faker_kwargs": {"max_value": 10**6},
                        },
                        {
                            "name": "bad",
                            "type": "faker",
                            "faker_type": "city",
                            "faker_kwargs": {"x": 1},
                        },
                        {"name": "wild", "type": "faker", "faker_type": "pystr"},
                    ],
                )
            ]
        )
    )
    b = compute_payload_bounds(cfg)
    assert b[("t", "s")] >= len("AB00001000")  # type: ignore[operator]
    assert b[("t", "c")] == 3 and b[("t", "i")] == 8
    assert b[("t", "f")] == FAKER_VALUE_CAPS["city"] and b[("t", "p")] == 7
    assert b[("t", "bad")] is None and b[("t", "wild")] is None


def test_every_built_in_faker_in_the_cap_table_stays_under_its_cap() -> None:
    """A cap that real provider output exceeds would make the bound a guess."""
    from faker import Faker

    from decoy_engine.internal.faker_setup import get_faker_providers

    fake = Faker()
    providers = get_faker_providers(fake)
    for name, cap in FAKER_VALUE_CAPS.items():
        if name not in providers:
            continue
        for seed in range(300):
            fake.seed_instance(seed)
            assert len(str(providers[name]()).encode("utf-8")) <= cap, (name, seed)


# ---- reserve-before-allocate, per stage -------------------------------------------------------


def _relational() -> dict[str, Any]:
    return prepared_config(
        fx.config(
            [
                fx.table(
                    "child",
                    60,
                    [
                        {"name": "cid", "type": "sequence"},
                        {
                            "name": "p",
                            "type": "reference",
                            "reference_table": "parent",
                            "reference_column": "id",
                        },
                    ],
                ),
                fx.table(
                    "parent",
                    60,
                    [
                        {"name": "id", "type": "sequence", "prefix": "P"},
                        {"name": "city", "type": "faker", "faker_type": "city"},
                    ],
                ),
            ]
        )
    )


class RefuseAt(GenerationBudgetLedger):
    """Refuses the first reservation whose stage starts with `prefix`, recording what had been
    allocated by then through the `snapshot` callback."""

    def __init__(self, prefix: str, snapshot: Any) -> None:
        super().__init__(10**12)
        self.prefix, self.snapshot, self.refused_at = prefix, snapshot, None

    def reserve(self, stage: str, upper_bound_bytes: int) -> Any:
        if stage.startswith(self.prefix):
            self.refused_at = stage
            self.snapshot()
            raise GenerationBudgetExceeded(code="generation_budget_exceeded", message=stage)
        return super().reserve(stage, upper_bound_bytes)


class AllocationSpies:
    """What a stage allocates: provider/pool work, batch generation, key-pool fill, sink writes."""

    def __init__(self, mp: pytest.MonkeyPatch, sink: SpySink | None) -> None:
        self.counts = {"pool_build": 0, "batch": 0, "key_pool": 0}
        self.sink = sink

        def count(name: str, real: Any) -> Any:
            def spy(*a: Any, **k: Any) -> Any:
                self.counts[name] += 1
                return real(*a, **k)

            return spy

        mp.setattr(
            _faker_pool, "build_pool_values", count("pool_build", _faker_pool.build_pool_values)
        )
        for cls in (_column_streams.SequenceStream, _column_streams.PerRowFakerStream):
            mp.setattr(cls, "next_batch", count("batch", cls.next_batch))
        mp.setattr(
            _column_streams.PooledFakerStream,
            "next_batch",
            count("batch", _column_streams.PooledFakerStream.next_batch),
        )
        mp.setattr(_column_streams.KeyPool, "add", count("key_pool", _column_streams.KeyPool.add))

    def snapshot(self) -> dict[str, int]:
        return {**self.counts, "sink_calls": len(self.sink.calls) if self.sink else 0}


@pytest.mark.parametrize(
    ("prefix", "sink"),
    [
        ("faker_pool_build", False),
        ("parent_key_pool", False),
        ("batch_buffers", False),
        ("accumulator", False),
        ("sink_buffer", True),
    ],
)
def test_each_bounded_stage_refuses_before_its_allocation(
    prefix: str, sink: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", 40)  # the city column pools
    spy_sink = SpySink() if sink else None
    spies = AllocationSpies(monkeypatch, spy_sink)
    seen: list[dict[str, int]] = []
    ledger = RefuseAt(prefix, lambda: seen.append(spies.snapshot()))
    with pytest.raises(GenerationBudgetExceeded):
        run_generation(_relational(), sink=spy_sink, ledger=ledger)
    assert ledger.refused_at and ledger.refused_at.startswith(prefix)
    (at_refusal,) = seen
    # Stages reserve in order; nothing the refused stage guards had been produced yet.
    guarded = {
        "faker_pool_build": ("pool_build", "batch", "sink_calls"),
        "parent_key_pool": ("pool_build", "batch", "key_pool", "sink_calls"),
        "batch_buffers": ("batch", "key_pool", "sink_calls"),
        "accumulator": ("batch", "key_pool", "sink_calls"),
        "sink_buffer": ("batch", "key_pool", "sink_calls"),
    }[prefix]
    for key in guarded:
        assert at_refusal[key] == 0, f"{prefix}: {key} ran before the reservation"
    after = spies.snapshot()
    if spy_sink is not None:
        # The only thing a refused stage may cause on the sink is the rollback.
        assert spy_sink.calls == ["abort"]
        after["sink_calls"] = at_refusal["sink_calls"]
    assert after == at_refusal, "work continued after the refusal"


def test_runtime_ledger_is_the_line_of_defense_when_the_admission_estimate_is_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the preflight estimate defeated, a ceiling below the first stage still refuses with
    nothing generated: the reservation, not the estimate, is what protects the ceiling."""
    monkeypatch.setattr(_streamed, "_fits", lambda ledger, need: True)
    spies = AllocationSpies(monkeypatch, None)
    config = _relational()
    with pytest.raises(GenerationBudgetExceeded) as err:
        run_generation(config, ledger=GenerationBudgetLedger(1))
    assert "parent_key_pool" in str(err.value)
    assert spies.snapshot() == {"pool_build": 0, "batch": 0, "key_pool": 0, "sink_calls": 0}
    # And a ceiling that fits every stage completes.
    ok = GenerationBudgetLedger(512 * MIB)
    assert run_generation(config, ledger=ok).tables is not None and ok.peak_bytes > 0


def test_ceiling_sweep_never_allocates_past_a_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """For ceilings from nothing up to the peak, every produced batch happens while the stage
    reservations that cover it are held, and each run either completes or refuses cleanly."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", 40)
    monkeypatch.setattr(_streamed, "_fits", lambda ledger, need: True)
    config = _relational()
    peak = GenerationBudgetLedger(None)
    run_generation(config, ledger=peak)
    refused_stages: set[str] = set()
    for step in range(0, 41):
        ceiling = peak.peak_bytes * step // 40
        ledger = GenerationBudgetLedger(ceiling)
        held_at_batch: list[int] = []
        real = _column_streams.SequenceStream.next_batch

        def spy(
            self: Any,
            offset: int,
            size: int,
            _l: Any = ledger,
            _r: Any = real,
            _h: Any = held_at_batch,
        ) -> Any:
            _h.append(_l.charged_bytes)
            return _r(self, offset, size)

        monkeypatch.setattr(_column_streams.SequenceStream, "next_batch", spy)
        try:
            run_generation(config, ledger=ledger)
        except GenerationBudgetExceeded as exc:
            refused_stages.add(str(exc).split("'")[1].split(":")[0])
        monkeypatch.setattr(_column_streams.SequenceStream, "next_batch", real)
        assert all(0 < h <= ceiling for h in held_at_batch), (ceiling, held_at_batch)
    assert {"parent_key_pool", "batch_buffers"} <= refused_stages


# ---- decline path and Exceeded ----------------------------------------------------------------


def _formula_free_legacy() -> dict[str, Any]:
    """Ineligible (group_key is legacy-only) but fully bounded."""
    return prepared_config(
        fx.config(
            [
                fx.table(
                    "t",
                    40,
                    [
                        {"name": "g", "type": "categorical", "categories": ["x", "y"]},
                        {
                            "name": "gk",
                            "type": "group_key",
                            "group_by": "g",
                            "length": 8,
                            "prefix": "G-",
                        },
                    ],
                )
            ]
        )
    )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("legacy_family", "legacy:t.gk:family_declined:group_key"),
        ("no_sink", "no_sink"),
        ("callable_sink", "sink_not_bounded"),
    ],
)
def test_decline_leaves_the_sink_untouched_and_returns_resident(
    case: str, expected_reason: str
) -> None:
    if case == "legacy_family":
        config, sink = _formula_free_legacy(), SpySink()
    else:
        config = prepared_config(fx.cfg_sequence())
        sink = None if case == "no_sink" else (lambda *a: None)  # type: ignore[assignment]
    # The bounded marker is present only on the legacy case's sink, which must still be untouched.
    result = run_generation(config, sink=sink, budget_bytes=256 * MIB)
    assert result.mode == "resident" and result.reason == expected_reason
    assert result.tables is not None and sum(result.row_counts.values()) > 0
    if isinstance(sink, SpySink):
        assert sink.calls == []


def test_decline_output_is_the_legacy_whole_table_output() -> None:
    config = _formula_free_legacy()
    result = run_generation(config, sink=SpySink(), budget_bytes=256 * MIB)
    legacy = synthesize._generate_tables_from_config(config)
    assert result.tables is not None
    assert fingerprints(result.tables) == fingerprints(legacy)
    assert result.declines == ("t.gk:family_declined:group_key",)


def test_budget_that_fits_resident_but_not_streaming_declines_with_the_sink_untouched() -> None:
    config = prepared_config(
        fx.config([fx.table("t", 4096, [{"name": f"c{i}", "type": "sequence"} for i in range(6)])])
    )
    tables = [t for t in config["tables"]]
    bounds = compute_payload_bounds(config)
    plan = _streamed._preflight(tables, bounds, lambda t: 4096, None)
    assert plan.resident_peak() < plan.streaming_peak()
    band = _streamed._fit_band()
    ceiling = int(plan.streaming_peak() * (1 + band))  # streaming does not fit, resident does
    sink = SpySink()
    result = run_generation(
        config, sink=sink, ledger=GenerationBudgetLedger(ceiling), batch_rows=4096
    )
    assert result.mode == "resident" and result.reason == "budget_does_not_fit_streaming"
    assert sink.calls == []
    plain = run_generation(config, batch_rows=4096).tables
    assert result.tables is not None and plain is not None
    assert fingerprints(result.tables) == fingerprints(plain), (
        "different values must never be emitted"
    )


def test_budget_too_small_even_for_resident_raises_and_emits_nothing() -> None:
    config = prepared_config(fx.config([fx.table("t", 10**9, [{"name": "c", "type": "sequence"}])]))
    # A bounded sink would stream 10**9 rows within the ceiling, so the resident case is a sink
    # that lacks the marker.
    sink = SpySink()
    sink.bounded_write_batches = False
    with pytest.raises(GenerationBudgetExceeded) as err:
        run_generation(config, sink=sink, budget_bytes=256 * MIB)
    assert err.value.code == "generation_budget_exceeded"
    assert sink.calls == []


def test_legacy_whole_table_over_the_ceiling_raises_before_generating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _formula_free_legacy()
    config["tables"][0]["row_count"] = 10**9
    called = []
    monkeypatch.setattr(
        synthesize, "_generate_tables_from_config", lambda *a, **k: called.append(1)
    )
    with pytest.raises(GenerationBudgetExceeded):
        run_generation(config, budget_bytes=256 * MIB)
    assert called == [], "legacy generation ran past its ceiling check"


def test_value_wider_than_its_declared_bound_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The faker cap is enforced per batch: a provider that exceeds it fails the run."""
    config = prepared_config(
        fx.config([fx.table("t", 10, [{"name": "c", "type": "faker", "faker_type": "city"}])])
    )
    monkeypatch.setattr(
        _column_streams.PerRowFakerStream,
        "next_batch",
        lambda self, offset, size: ["x" * 5000] * size,
    )
    with pytest.raises(GenerationBudgetExceeded) as err:
        run_generation(config, budget_bytes=256 * MIB)
    assert err.value.code == "value_exceeds_declared_bound"
    assert "xxxx" not in str(err.value), "an error must never carry a data value"


def test_bounded_and_unbounded_runs_emit_identical_values() -> None:
    config = prepared_config(fx.cfg_live_faker())
    bounded = run_generation(config, budget_bytes=256 * MIB).tables
    plain = run_generation(config).tables
    assert bounded is not None and plain is not None
    assert fingerprints(bounded) == fingerprints(plain)

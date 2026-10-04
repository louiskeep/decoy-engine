"""B7 unit tests that grade the split's decision and merge logic directly.

The acceptance tests drive `run_pipeline`; these call the free functions of
`_pipeline_multi_table` with small fakes, so each gate, each merge rule and each
forwarded argument is graded on its own (the mutation pass over the module reads them).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _pipeline_multi_table as pmt
from decoy_engine.execution import run_pipeline
from tests.unit.execution import _multi_table_support as mt

pytestmark = pytest.mark.filterwarnings("ignore")


def _plan(*columns: tuple[str, str, str, bool, tuple[tuple[str, Any], ...]]) -> Any:
    """A plan stand-in: (table, column, strategy, deterministic, provider_config)."""
    tables: dict[str, list[tuple[str, Any]]] = {}
    for table, column, strategy, deterministic, config in columns:
        seed = SimpleNamespace(
            strategy=strategy, deterministic=deterministic, provider_config=config
        )
        tables.setdefault(table, []).append((column, seed))
    per_table = tuple((t, SimpleNamespace(per_column=tuple(c))) for t, c in tables.items())
    return SimpleNamespace(seed_envelope=SimpleNamespace(per_table=per_table))


class TestUnseededRandomNodes:
    def test_reports_each_fresh_generator_shape_and_only_those(self) -> None:
        plan = _plan(
            ("t", "a", "categorical", False, ()),
            ("t", "b", "categorical", True, ()),
            ("t", "c", "shuffle", False, ()),
            ("t", "d", "shuffle", True, ()),
            ("t", "e", "nested", False, (("strategy", "categorical"),)),
            ("t", "f", "nested", False, (("strategy", "shuffle"),)),
            ("t", "g", "nested", True, (("strategy", "categorical"),)),
            ("t", "h", "nested", False, (("strategy", "hash"),)),
            ("u", "i", "hash", False, ()),
            ("u", "j", "redact", False, ()),
        )
        assert pmt.unseeded_random_nodes(plan) == (
            ("t", "c", "shuffle"),
            ("t", "f", "nested"),
        )
        # Non-deterministic categorical is seeded; it is held whole by the separate
        # positional-deferred veto instead.
        assert pmt.position_keyed_deferred_nodes(plan) == (
            ("t", "a", "categorical"),
            ("t", "e", "nested"),
        )

    def test_a_plan_without_random_columns_reports_nothing(self) -> None:
        assert pmt.unseeded_random_nodes(_plan(("t", "a", "hash", False, ()))) == ()


_KINDS = {"a": "mask", "b": "mask"}


def _gate(**overrides: Any) -> bool:
    args: dict[str, Any] = {
        "config": {},
        "plan": _plan(("a", "x", "hash", False, ())),
        "graph": SimpleNamespace(edges=()),
        "substrate": "pandas",
        "table_kinds": _KINDS,
        "auto_chunk": True,
        "dispatcher_enabled": True,
        "split_enabled": True,
        "vault_writer_present": False,
    }
    args.update(overrides)
    return pmt._job_gates_hold(args.pop("config"), **args)


class TestJobGates:
    def test_every_gate_open_holds(self) -> None:
        assert _gate() is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"auto_chunk": False},
            {"dispatcher_enabled": False},
            {"split_enabled": False},
            {"table_kinds": {"a": "mask"}},
            {"table_kinds": {"a": "mask", "b": "mask", "g": "generate"}},
            {"table_kinds": {"a": "mask", "g": "generate"}},
            {"graph": SimpleNamespace(edges=(object(),))},
            {"config": {"relationships": [{"parent": {}}]}},
            {"substrate": "duckdb"},
            {"config": {"quarantine": {"enabled": True}}},
            {"vault_writer_present": True},
            {"config": {"validators": [{"name": "x"}]}},
            {"plan": _plan(("a", "x", "shuffle", False, ()))},
        ],
        ids=[
            "auto_chunk",
            "dispatcher",
            "split_knob",
            "one_table",
            "generate_table",
            "one_mask_one_generate",
            "fk_edge",
            "relationships_block",
            "substrate",
            "quarantine",
            "vault_writer",
            "validators",
            "unseeded",
        ],
    )
    def test_each_closed_gate_alone_keeps_the_job_whole(self, overrides: dict[str, Any]) -> None:
        assert _gate(**overrides) is False

    def test_quarantine_present_but_disabled_does_not_close_the_gate(self) -> None:
        assert _gate(config={"quarantine": {"enabled": False}}) is True
        assert _gate(config={"quarantine": None}) is True


class TestSplitStamp:
    def test_the_six_reproducibility_keys(self) -> None:
        split = pmt.MultiTableSplit(("a", "c"), ("b",), {"a": "ra", "b": "rb", "c": "rc"})
        assert pmt.split_reproducibility_stamp(
            split, chunk_size_rows=7, auto_chunk_threshold_rows=11
        ) == {
            "mode": "chunked",
            "chunk_size_rows": 7,
            "threshold_rows": 11,
            "source_rows": None,
            "chunk_count": None,
            "reason": "multi_table_split: 2 of 3 mask tables dispatched (a, c)",
        }

    def test_the_split_is_frozen(self) -> None:
        import dataclasses

        split = pmt.MultiTableSplit(("a",), (), {"a": "x"})
        with pytest.raises(dataclasses.FrozenInstanceError):
            split.dispatched = ()  # type: ignore[misc]

    def test_every_name_in_all_resolves(self) -> None:
        assert sorted(pmt.__all__) == [
            "MultiTableSplit",
            "POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED",
            "UNSEEDED_RANDOM_STRATEGIES",
            "decide_multi_table_split",
            "position_keyed_deferred_nodes",
            "run_multi_table_split",
            "split_reproducibility_stamp",
            "unseeded_random_nodes",
        ]
        assert all(hasattr(pmt, name) for name in pmt.__all__)

    def test_the_reasons_mapping_is_read_only_and_copied(self) -> None:
        reasons = {"a": "x"}
        split = pmt.MultiTableSplit(("a",), (), reasons)
        reasons["a"] = "changed"
        assert split.reasons["a"] == "x"
        with pytest.raises(TypeError):
            split.reasons["a"] = "y"  # type: ignore[index]


class TestTableEntry:
    SPLIT = pmt.MultiTableSplit(("a",), ("b",), {"a": "why a", "b": "why b"})
    LANE = {"lane": "dispatcher", "lane_reason": None, "native_threads": 3, "ignored": 1}

    def test_a_dispatched_entry_carries_the_lane_keys_and_the_chunk_count(self) -> None:
        sources = {"a": pa.table({"x": list(range(10))}), "b": pa.table({"x": [1]})}
        assert pmt._table_entry(self.SPLIT, "a", sources, 4, self.LANE) == {
            "table": "a",
            "dispatched": True,
            "source_rows": 10,
            "chunk_count": 3,
            "reason": "why a",
            "lane": "dispatcher",
            "lane_reason": None,
            "native_threads": 3,
        }

    def test_an_exact_multiple_of_the_chunk_size_does_not_add_a_chunk(self) -> None:
        sources = {"a": pa.table({"x": list(range(8))})}
        assert pmt._table_entry(self.SPLIT, "a", sources, 4, self.LANE)["chunk_count"] == 2

    def test_a_group_entry_has_no_chunk_count_and_no_lane_keys(self) -> None:
        sources = {"b": pa.table({"x": [1, 2]})}
        assert pmt._table_entry(self.SPLIT, "b", sources, 4, None) == {
            "table": "b",
            "dispatched": False,
            "source_rows": 2,
            "chunk_count": None,
            "reason": "why b",
        }

    def test_the_lane_keys_follow_the_lane_block_not_the_dispatched_flag(self) -> None:
        sources = {"b": pa.table({"x": [1, 2]})}
        entry = pmt._table_entry(self.SPLIT, "b", sources, 4, self.LANE)
        assert entry["lane"] == "dispatcher" and entry["native_threads"] == 3
        assert "ignored" not in entry

    def test_a_table_without_a_resident_source_reports_null_rows(self) -> None:
        entry = pmt._table_entry(self.SPLIT, "a", {}, 4, self.LANE)
        assert entry["source_rows"] is None and entry["chunk_count"] is None
        assert entry["lane"] == "dispatcher"


# ---------------------------------------------------------------------------
# run_multi_table_split against fakes.
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def run(self, plan: Any, sources: Any, **kwargs: Any) -> Any:
        self.calls.append((sources, kwargs))
        return self.result


def _group_result(outputs: dict[str, pa.Table], **extra: Any) -> Any:
    fields: dict[str, Any] = {
        "outputs": outputs,
        "timings": ("g-timing",),
        "boundary_conversion_ms": 2.5,
        "warnings": ("g-warning",),
        "quality_metrics": {"code_set_corpora": ["g-corpus"], "other": 1},
        "row_errors": ("g-row-error",),
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _unit(table: str, tag: str, conv: float) -> Any:
    return (
        {table: pa.table({"v": [tag]})},
        (f"{tag}-timing",),
        conv,
        (f"{tag}-warning",),
        {
            "code_set_corpora": [f"{tag}-corpus"],
            "chunked_route": {"route": tag},
            "auto_chunk": {"lane": "dispatcher", "lane_reason": None, "native_threads": 5},
        },
    )


def _run_fake(
    monkeypatch: pytest.MonkeyPatch, split: pmt.MultiTableSplit, resident: dict[str, pa.Table]
) -> tuple[Any, _FakeAdapter, list[dict[str, Any]]]:
    adapter = _FakeAdapter(
        _group_result({"g1": pa.table({"v": ["g"]}), "x": pa.table({"v": ["x"]})})
    )
    calls: list[dict[str, Any]] = []
    units = {"d1": _unit("d1", "d1", 1.0), "d2": _unit("d2", "d2", 4.0)}

    def fake_run_auto_chunk(config: Any, source: Any, **kwargs: Any) -> Any:
        calls.append({"config": config, "source": source, **kwargs})
        return units[kwargs["table"]]

    monkeypatch.setattr(pmt._pipeline_auto_chunk, "run_auto_chunk", fake_run_auto_chunk)
    result = pmt.run_multi_table_split(
        split,
        {"cfg": 1},
        resident_sources=resident,
        engine_version="ev",
        registry="registry",  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        chunk_size_rows=2,
        key_provider="key",  # type: ignore[arg-type]
        native_threads=5,
        plan="plan",  # type: ignore[arg-type]
        graph="graph",  # type: ignore[arg-type]
        namespace_registry="ns",  # type: ignore[arg-type]
        unconfigured_column_policy="error",
        generate_output_tables=frozenset({"gen"}),
    )
    return result, adapter, calls


def _resident() -> dict[str, pa.Table]:
    return {
        "g1": pa.table({"v": ["1"] * 3}),
        "d2": pa.table({"v": ["2"] * 5}),
        "x": pa.table({"v": ["3"]}),
        "d1": pa.table({"v": ["4"] * 4}),
    }


def test_the_split_merges_every_unit_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    split = pmt.MultiTableSplit(("d1", "d2"), ("g1",), {"d1": "r1", "g1": "rg", "d2": "r2"})
    (outputs, timings, conv, warnings, metrics, row_errors), adapter, calls = _run_fake(
        monkeypatch, split, _resident()
    )
    # Outputs follow the order of the resident sources, each from its own unit.
    assert list(outputs) == ["g1", "d2", "x", "d1"]
    assert outputs["d1"].column("v").to_pylist() == ["d1"]
    assert outputs["d2"].column("v").to_pylist() == ["d2"]
    assert outputs["g1"].column("v").to_pylist() == ["g"]
    assert outputs["x"].column("v").to_pylist() == ["x"]
    # Dispatched units in config order, then the group.
    assert timings == ("d1-timing", "d2-timing", "g-timing")
    assert warnings == ("d1-warning", "d2-warning", "g-warning")
    assert conv == pytest.approx(7.5)
    assert row_errors == ("g-row-error",)
    assert metrics["code_set_corpora"] == ["d1-corpus", "d2-corpus", "g-corpus"]
    assert metrics["other"] == 1
    assert metrics["chunked_route_by_table"] == {"d1": {"route": "d1"}, "d2": {"route": "d2"}}
    block = metrics["auto_chunk"]
    assert (block["lane"], block["lane_reason"], block["native_threads"]) == ("dispatcher", None, 5)
    assert [
        (t["table"], t["dispatched"], t["source_rows"], t["chunk_count"]) for t in block["tables"]
    ] == [
        ("d1", True, 4, 2),
        ("g1", False, 3, None),
        ("d2", True, 5, 3),
    ]
    assert [c["table"] for c in calls] == ["d1", "d2"]


def test_each_dispatched_table_gets_its_own_source_and_the_job_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    split = pmt.MultiTableSplit(("d1", "d2"), ("g1",), {"d1": "r1", "g1": "rg", "d2": "r2"})
    resident = _resident()
    _, adapter, calls = _run_fake(monkeypatch, split, resident)
    for call in calls:
        assert call["source"] is resident[call["table"]]
        assert call["config"] == {"cfg": 1}
        assert call["engine_version"] == "ev"
        assert call["registry"] == "registry"
        assert call["vault_writer"] is None
        assert call["chunk_size_rows"] == 2
        assert call["key_provider"] == "key"
        assert call["native_threads"] == 5
        assert call["dispatcher_enabled"] is True
        assert call["adapter"] is adapter
    ((group_sources, kwargs),) = adapter.calls
    assert list(group_sources) == ["g1", "x"]
    assert kwargs == {
        "registry": "registry",
        "relationship_graph": "graph",
        "namespace_registry": "ns",
        "unconfigured_column_policy": "error",
        "generate_output_tables": frozenset({"gen"}),
        "key_provider": "key",
    }


def test_all_dispatched_with_no_extra_source_never_calls_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    split = pmt.MultiTableSplit(("d1", "d2"), (), {"d1": "r1", "d2": "r2"})
    resident = {"d1": _resident()["d1"], "d2": _resident()["d2"]}
    (outputs, timings, conv, warnings, metrics, row_errors), adapter, _ = _run_fake(
        monkeypatch, split, resident
    )
    assert adapter.calls == []
    assert list(outputs) == ["d1", "d2"]
    assert timings == ("d1-timing", "d2-timing")
    assert warnings == ("d1-warning", "d2-warning")
    assert conv == pytest.approx(5.0)
    assert row_errors == ()
    assert metrics["code_set_corpora"] == ["d1-corpus", "d2-corpus"]


def test_no_corpora_means_no_corpora_key(monkeypatch: pytest.MonkeyPatch) -> None:
    split = pmt.MultiTableSplit(("d1",), ("g1",), {"d1": "r1", "g1": "rg"})
    adapter = _FakeAdapter(_group_result({"g1": pa.table({"v": ["g"]})}, quality_metrics={}))
    unit = _unit("d1", "d1", 1.0)
    unit[4].pop("code_set_corpora")
    monkeypatch.setattr(pmt._pipeline_auto_chunk, "run_auto_chunk", lambda *a, **k: unit)
    outputs, _, _, _, metrics, _ = pmt.run_multi_table_split(
        split,
        {},
        resident_sources={"d1": pa.table({"v": [1]}), "g1": pa.table({"v": [2]})},
        engine_version="ev",
        registry=None,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        chunk_size_rows=2,
        key_provider=None,
        native_threads=1,
        plan=None,  # type: ignore[arg-type]
        graph=None,  # type: ignore[arg-type]
        namespace_registry=None,  # type: ignore[arg-type]
        unconfigured_column_policy="warn",
        generate_output_tables=frozenset(),
    )
    assert "code_set_corpora" not in metrics
    assert list(outputs) == ["d1", "g1"]


# ---------------------------------------------------------------------------
# End to end: arguments the acceptance matrix does not vary.
# ---------------------------------------------------------------------------


def test_the_key_provider_reaches_both_the_dispatched_and_the_group_tables(
    tmp_path: Path,
) -> None:
    from decoy_engine.keyprovider import SecretKeyProvider

    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
            "small": (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s")),
        },
    )
    provider = SecretKeyProvider(secret=bytes(range(32)), key_version="v1")
    keyed = run_pipeline(cfg, sources=sources, key_provider=provider, **mt.kw())
    plain = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, key_provider=provider, **mt.kw(**mt.off_kw()))
    assert mt.dispatched_tables(keyed) == ["big"]
    for name in ("big", "small"):
        assert (
            keyed.outputs[name].column("h").to_pylist() == off.outputs[name].column("h").to_pylist()
        )
        assert (
            keyed.outputs[name].column("h").to_pylist()
            != plain.outputs[name].column("h").to_pylist()
        )


def test_the_unconfigured_column_policy_reaches_the_group_call(tmp_path: Path) -> None:
    small = pa.table({**mt.string_table(mt.SMALL, "s").to_pydict(), "extra": ["z"] * mt.SMALL})
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
            "small": (mt.std_columns("small_ns"), small),
        },
    )
    cfg["global_settings"]["unconfigured_column_policy"] = "error"
    errors: list[BaseException] = []
    for extra in ({}, mt.off_kw()):
        with pytest.raises(Exception) as raised:
            run_pipeline(cfg, sources=sources, **mt.kw(**extra))
        errors.append(raised.value)
    assert mt.same_error(errors[0], errors[1])
    cfg["global_settings"]["unconfigured_column_policy"] = "warn"
    ok = run_pipeline(cfg, sources=sources, **mt.kw())
    assert mt.dispatched_tables(ok) == ["big"]
    assert ok.outputs["small"].column("extra").to_pylist() == ["z"] * mt.SMALL


def test_a_group_table_row_error_still_raises_with_its_own_records(tmp_path: Path) -> None:
    values = ["2020-01-01"] * (mt.SMALL - 1) + ["not-a-date"]
    dated = (
        [
            {
                "name": "d",
                "strategy": "date_shift",
                "namespace": "d_ns",
                "provider_config": {"min_days": -5, "max_days": 5, "date_format": "%Y-%m-%d"},
            }
        ],
        pa.table({"d": pa.array(values)}),
    )
    cfg, sources = mt.build_job(
        tmp_path,
        {"big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")), "small": dated},
    )
    with pytest.raises(RowErrorsFailedError) as split:
        run_pipeline(cfg, sources=sources, **mt.kw())
    with pytest.raises(RowErrorsFailedError) as off:
        run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert {r.table for r in split.value.records} == {"small"}
    assert split.value.records == off.value.records


def test_the_decision_names_dispatched_group_and_reasons_per_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "z": (mt.std_columns("z_ns"), mt.string_table(mt.BIG, "z")),
            "tiny": (mt.std_columns("t_ns"), mt.string_table(mt.SMALL, "t")),
            "a": (mt.std_columns("a_ns"), mt.string_table(mt.BIG + 3, "a")),
            "tiny2": (mt.std_columns("t2_ns"), mt.string_table(mt.SMALL, "u")),
        },
    )
    decisions: list[Any] = []
    real = pmt.decide_multi_table_split

    def spy(*args: Any, **kwargs: Any) -> Any:
        decisions.append(real(*args, **kwargs))
        return decisions[-1]

    monkeypatch.setattr(pmt, "decide_multi_table_split", spy)
    run_pipeline(cfg, sources=sources, **mt.kw())
    (split,) = decisions
    assert split.dispatched == ("z", "a")
    assert split.full_frame == ("tiny", "tiny2")
    assert list(split.reasons) == ["z", "tiny", "a", "tiny2"]
    assert "threshold" in split.reasons["tiny"] and "threshold" in split.reasons["tiny2"]
    assert "chunk-safe" in split.reasons["z"]

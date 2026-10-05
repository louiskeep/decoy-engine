"""B7 acceptance tests 1 and 2: independence, job gates and the no-dispatch golden.

Plan: docs/plans/2026-10-01-multi-table-dispatch.md (revision 3), "What independent means"
and Design 2. Every no-split case has a table that would otherwise dispatch, asserts the
split executor never runs (spy on `run_multi_table_split`), and, where the output is
repeatable, asserts equality with the split-off run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import RowErrorsFailedError, ValidatorFailedError
from decoy_engine.execution import ExecutionError, run_pipeline
from decoy_engine.generation.pool import PoolCache
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_support as mt

pytestmark = pytest.mark.filterwarnings("ignore")


def _two_tables(tmp_path: Path, **extra: Any) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    return mt.build_job(
        tmp_path,
        {
            "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
            "tiny": (mt.std_columns("tiny_ns"), mt.string_table(mt.SMALL, "t")),
        },
        **extra,
    )


def _two_big(tmp_path: Path, **extra: Any) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    return mt.build_job(
        tmp_path,
        {
            "a": (mt.std_columns("a_ns"), mt.string_table(mt.BIG, "a")),
            "b": (mt.std_columns("b_ns"), mt.string_table(mt.BIG, "b")),
        },
        **extra,
    )


def _assert_tables_equal(a: Any, b: Any) -> None:
    assert list(a.outputs) == list(b.outputs)
    for name in a.outputs:
        assert a.outputs[name].equals(b.outputs[name], check_metadata=True), name


def _assert_golden(
    cfg: dict[str, Any],
    sources: dict[str, pa.Table],
    monkeypatch: pytest.MonkeyPatch,
    *,
    expect: str = "ok",
    **extra: Any,
) -> Any:
    """No split: the executor is never entered and every observable equals the split-off run."""
    calls = mt.spy_split(monkeypatch)
    got = mt.outcome(lambda: run_pipeline(cfg, sources=sources, **mt.kw(**extra)))
    assert calls == []
    off = mt.outcome(
        lambda: run_pipeline(cfg, sources=sources, **mt.kw(**{**mt.off_kw(), **extra}))
    )
    mt.assert_same_outcome(got, off, expect=expect)
    return got[1]


# ---------------------------------------------------------------------------
# Test 1: independence.
# ---------------------------------------------------------------------------


def _fk_three_tables(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table({"id": pa.array([f"C{i}" for i in range(mt.BIG)])})
    child = pa.table({"customer_id": pa.array([f"C{i % 10}" for i in range(mt.BIG)])})
    other = mt.string_table(mt.BIG, "o")
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "customers": ([support.hash_col("id", "id_ns")], parent),
            "orders": ([support.hash_col("customer_id", "id_ns")], child),
            "other": (mt.std_columns("other_ns"), other),
        },
        extra={
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "id_ns",
                }
            ]
        },
    )
    return cfg, sources


@pytest.mark.parametrize("mode", ["auto", "full_frame"])
def test_fk_edge_is_a_golden_no_split(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = _fk_three_tables(tmp_path)
    _assert_golden(cfg, sources, monkeypatch, execution_mode=mode)


def test_relationships_block_without_a_profiled_edge_never_enters_the_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The FK values do not overlap, so profiling finds no edge, but the config declares one.
    parent = pa.table({"id": pa.array([f"C{i}" for i in range(mt.BIG)])})
    child = pa.table({"customer_id": pa.array([f"Z{i}" for i in range(mt.BIG)])})
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "customers": ([support.hash_col("id", "id_ns")], parent),
            "orders": ([support.hash_col("customer_id", "id_ns")], child),
        },
        extra={
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "id_ns",
                }
            ]
        },
    )
    _assert_golden(cfg, sources, monkeypatch)


def test_shared_namespace_masks_the_same_value_the_same_in_both_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = pa.table({"h": pa.array([f"v{i}" for i in range(mt.BIG)])})
    small = pa.table({"h": pa.array([f"v{i}" for i in range(mt.SMALL)])})
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": ([support.hash_col("h", "shared_ns")], shared),
            "tiny": ([support.hash_col("h", "shared_ns")], small),
        },
    )
    calls = mt.spy_split(monkeypatch)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert len(calls) == 1
    assert mt.dispatched_tables(got) == ["big"]
    big = got.outputs["big"].column("h").to_pylist()
    tiny = got.outputs["tiny"].column("h").to_pylist()
    assert tiny == big[: mt.SMALL]
    assert big == off.outputs["big"].column("h").to_pylist()
    assert tiny == off.outputs["tiny"].column("h").to_pylist()


# ---------------------------------------------------------------------------
# Test 2: job gates and the no-dispatch golden.
# ---------------------------------------------------------------------------


def test_one_mask_table_is_a_golden_no_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(tmp_path, {"only": (mt.std_columns("o_ns"), mt.string_table())})
    _assert_golden(cfg, sources, monkeypatch)


def test_generate_table_present_is_a_golden_no_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = _two_big(tmp_path)
    gen = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [
                {
                    "name": "gen",
                    "row_count": 5,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1}],
                }
            ],
            "targets": {"gen": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()
    cfg["tables"].append(gen["tables"][0])
    cfg["targets"]["gen"] = gen["targets"]["gen"]
    got = _assert_golden(cfg, sources, monkeypatch)
    assert got.outputs["gen"].num_rows == 5
    assert mt.dispatched_tables(got) == []


@pytest.mark.parametrize(
    "knob",
    [
        {"auto_chunk": False},
        {"chunked_dispatcher_enabled": False},
        {"multi_table_dispatch_enabled": False},
    ],
    ids=["auto_chunk_off", "dispatcher_off", "split_off"],
)
def test_kill_switches_keep_the_full_frame_call(
    knob: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if "multi_table_dispatch_enabled" in knob and not mt.split_supported():
        pytest.skip("the knob does not exist before B7")
    cfg, sources = _two_big(tmp_path)
    calls = mt.spy_split(monkeypatch)
    got = mt.outcome(lambda: run_pipeline(cfg, sources=sources, **mt.kw(**knob)))
    assert calls == []
    off = mt.outcome(lambda: run_pipeline(cfg, sources=sources, **mt.kw(**{**mt.off_kw(), **knob})))
    mt.assert_same_outcome(got, off)
    assert mt.dispatched_tables(got[1]) == []


def test_a_forced_substrate_reaches_the_same_outcome_with_the_split_on_and_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substrate is resolved before routing, so a non-pandas substrate fails the same way
    with the split on and off, and an env-resolved pandas substrate splits as usual."""
    cfg, sources = _two_big(tmp_path)
    calls = mt.spy_split(monkeypatch)
    got = mt.outcome(lambda: run_pipeline(cfg, sources=sources, **mt.kw(substrate="polars")))
    off = mt.outcome(
        lambda: run_pipeline(
            cfg, sources=sources, **mt.kw(**{**mt.off_kw(), "substrate": "polars"})
        )
    )
    mt.assert_same_outcome(got, off, expect="err")
    assert got[1].code == "invalid_substrate"
    assert calls == []
    monkeypatch.setenv("DECOY_SUBSTRATE", "pandas")
    env_run = run_pipeline(cfg, sources=sources, **mt.kw(substrate=None))
    assert mt.dispatched_tables(env_run) == ["a", "b"]


def test_no_table_passing_the_table_gate_is_a_golden_no_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "a": (mt.std_columns("a_ns"), mt.string_table(mt.SMALL, "a")),
            "b": (mt.std_columns("b_ns"), mt.string_table(mt.SMALL, "b")),
        },
    )
    _assert_golden(cfg, sources, monkeypatch)


def test_quarantine_enabled_is_a_golden_no_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = _two_tables(
        tmp_path,
        extra={
            "quarantine": {"enabled": True, "output_path": str(tmp_path / "quarantine.parquet")}
        },
    )
    _assert_golden(cfg, sources, monkeypatch)


def test_validators_passing_is_a_golden_no_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = _two_tables(
        tmp_path,
        extra={
            "validators": [
                {"name": "regex_match", "columns": {"big": ["h"]}, "params": {"pattern": ".+"}},
            ]
        },
    )
    got = _assert_golden(cfg, sources, monkeypatch)
    assert "validation" in got.quality_metrics


def test_validators_failing_with_a_row_error_raise_the_same_validator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    date_src = pa.table({"d": pa.array(["2020-01-01"] * (mt.BIG - 1) + ["not-a-date"])})
    cols = [
        {
            "name": "d",
            "strategy": "date_shift",
            "namespace": "d_ns",
            "provider_config": {"min_days": -5, "max_days": 5, "date_format": "%Y-%m-%d"},
        }
    ]
    cfg, sources = mt.build_job(
        tmp_path,
        {"big": (cols, date_src), "tiny": (mt.std_columns("t_ns"), mt.string_table(mt.SMALL))},
        extra={
            "validators": [
                {
                    "name": "regex_match",
                    "columns": {"tiny": ["h"]},
                    "params": {"pattern": "^never-matches$"},
                }
            ]
        },
    )
    calls = mt.spy_split(monkeypatch)
    errors: list[ValidatorFailedError] = []
    for extra in ({}, mt.off_kw()):
        with pytest.raises(ValidatorFailedError) as raised:
            run_pipeline(cfg, sources=sources, **mt.kw(**extra))
        errors.append(raised.value)
    assert calls == []
    on_report, off_report = errors[0].report, errors[1].report
    assert on_report.passed is False
    assert on_report.findings == off_report.findings and on_report.findings
    assert on_report.validators_run == off_report.validators_run
    assert str(errors[0]) == str(errors[1])


@pytest.mark.parametrize("value", [0, None, "yes"])
def test_bad_split_knob_values_fail_before_profiling(
    value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not mt.split_supported():
        pytest.fail("multi_table_dispatch_enabled is not a run_pipeline knob yet")
    import decoy_engine.profile as profile_mod

    profiled: list[int] = []
    monkeypatch.setattr(profile_mod, "profile_source", lambda *a, **k: profiled.append(1))
    cfg, sources = _two_big(tmp_path)
    with pytest.raises(ExecutionError) as raised:
        run_pipeline(cfg, sources=sources, **mt.kw(multi_table_dispatch_enabled=value))
    assert raised.value.code == "invalid_execution_knob"
    assert "multi_table_dispatch_enabled" in str(raised.value)
    assert profiled == []


# --- vault writer ----------------------------------------------------------


def _vault_writer(cfg: dict[str, Any]) -> Any:
    from decoy_engine.vault import vault_writer_for_config

    return vault_writer_for_config(cfg)


def _vault_cols(namespace: str) -> list[dict[str, Any]]:
    return [{**support.hash_col("h", namespace), "vault": True}, support.redact_col("r")]


def test_vault_writer_job_stays_whole_and_ends_with_the_same_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (_vault_cols("big_ns"), mt.string_table(mt.BIG, "b")),
            "tiny": (_vault_cols("tiny_ns"), mt.string_table(mt.SMALL, "t")),
        },
    )
    calls = mt.spy_split(monkeypatch)
    w_on, w_off = _vault_writer(cfg), _vault_writer(cfg)
    got = mt.outcome(lambda: run_pipeline(cfg, sources=sources, vault_writer=w_on, **mt.kw()))
    off = mt.outcome(
        lambda: run_pipeline(cfg, sources=sources, vault_writer=w_off, **mt.kw(**mt.off_kw()))
    )
    assert calls == []
    mt.assert_same_outcome(got, off)
    assert w_on._entries == w_off._entries
    assert len(w_on._entries) == mt.BIG + mt.SMALL


def test_vault_file_round_trip_equals_the_split_off_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.plan._seed import _normalize_job_seed
    from decoy_engine.vault import load_vault

    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": ([{**support.hash_col("h", "big_ns"), "vault": True}], mt.string_table(mt.BIG)),
            "tiny": (
                [{**support.hash_col("h", "tiny_ns"), "vault": True}],
                mt.string_table(mt.SMALL, "t"),
            ),
        },
    )
    calls = mt.spy_split(monkeypatch)
    maps = {}
    for label, extra in (("on", {}), ("off", mt.off_kw())):
        writer = _vault_writer(cfg)
        run_pipeline(cfg, sources=sources, vault_writer=writer, **mt.kw(**extra))
        path = tmp_path / f"{label}.vault"
        writer.write(path)
        maps[label], ambiguous = load_vault(path, _normalize_job_seed(cfg))
        assert ambiguous == 0
    assert calls == []
    assert maps["on"] == maps["off"]


def test_vault_writer_with_a_later_failing_table_leaves_the_writer_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = {
        "name": "code",
        "strategy": "code_set",
        "provider_config": {"code_set": "no_such_corpus"},
    }
    code_src = pa.table({"code": pa.array(["A00"] * mt.SMALL)})
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (_vault_cols("big_ns"), mt.string_table(mt.BIG, "b")),
            "tiny": ([missing], code_src),
        },
    )
    calls = mt.spy_split(monkeypatch)
    errors: list[BaseException] = []
    writers = []
    for extra in ({}, mt.off_kw()):
        writer = _vault_writer(cfg)
        writers.append(writer)
        with pytest.raises(Exception) as raised:
            run_pipeline(cfg, sources=sources, vault_writer=writer, **mt.kw(**extra))
        errors.append(raised.value)
    assert calls == []
    assert mt.same_error(errors[0], errors[1])
    assert all(len(w._entries) == 0 for w in writers)


def test_vault_writer_keyed_differently_is_rejected_before_any_table_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.keyprovider import SecretKeyProvider
    from decoy_engine.vault import VaultWriter

    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (_vault_cols("big_ns"), mt.string_table(mt.BIG, "b")),
            "tiny": (_vault_cols("tiny_ns"), mt.string_table(mt.SMALL, "t")),
        },
    )
    calls = mt.spy_split(monkeypatch)
    adapter_calls = mt.spy_adapter_run(monkeypatch)
    errors: list[BaseException] = []
    for extra in ({}, mt.off_kw()):
        with pytest.raises(Exception) as raised:
            run_pipeline(
                cfg,
                sources=sources,
                vault_writer=VaultWriter((42).to_bytes(8, "big")),
                key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
                **mt.kw(**extra),
            )
        errors.append(raised.value)
    assert calls == [] and adapter_calls == []
    assert mt.same_error(errors[0], errors[1])


# --- fresh-generator randomness and the positional-deferred categorical veto -----


def _random_columns(kind: str) -> tuple[list[dict[str, Any]], pa.Table]:
    nested = kind == "nested"
    values = pa.table(
        {
            "v": pa.array(
                [
                    f'{{"k": "{["red", "green", "blue"][i % 3]}"}}'
                    if nested
                    else ["red", "green", "blue"][i % 3]
                    for i in range(mt.SMALL * 40)
                ]
            ),
            "h": pa.array([f"u{i}" for i in range(mt.SMALL * 40)]),
        }
    )
    base = support.hash_col("h", "r_ns")
    if kind == "categorical":
        col = {
            "name": "v",
            "strategy": "categorical",
            "deterministic": False,
            "namespace": "v_ns",
            "provider_config": {"categories": ["x", "y", "z"]},
        }
    elif kind == "shuffle":
        col = {"name": "v", "strategy": "shuffle", "deterministic": False}
    else:
        col = {
            "name": "v",
            "strategy": "nested",
            "deterministic": False,
            "namespace": "v_ns",
            "provider_config": {
                "target": "$.k",
                "strategy": "categorical",
                "strategy_config": {"categories": ["x", "y", "z"]},
            },
        }
    return [col, base], values


@pytest.mark.parametrize("kind", ["categorical", "shuffle", "nested"])
def test_split_vetoed_random_columns_keep_the_full_frame_call(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    random_cols, random_table = _random_columns(kind)
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "tiny": (random_cols, random_table.slice(0, mt.SMALL)),
            "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
        },
    )
    calls = mt.spy_split(monkeypatch)
    adapter_calls = mt.spy_adapter_run(monkeypatch)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    assert calls == []
    assert len(adapter_calls) == 1
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert len(adapter_calls) == 2
    _assert_same_adapter_call(adapter_calls[0], adapter_calls[1])
    assert mt.strip_elapsed(got.quality_metrics) == mt.strip_elapsed(off.quality_metrics)
    assert got.warnings == off.warnings
    assert mt.timing_keys(got) == mt.timing_keys(off)
    assert list(got.outputs) == list(off.outputs)
    assert got.outputs["big"].equals(off.outputs["big"], check_metadata=True)
    assert (
        got.outputs["tiny"].column("h").to_pylist() == off.outputs["tiny"].column("h").to_pylist()
    )
    got_v, off_v = got.outputs["tiny"].column("v"), off.outputs["tiny"].column("v")
    assert got_v.type == off_v.type
    assert got_v.null_count == off_v.null_count


def _assert_same_adapter_call(a: tuple[Any, Any], b: tuple[Any, Any]) -> None:
    """Every positional and keyword argument of the two `adapter.run` calls is equal."""
    (a_args, a_kwargs), (b_args, b_kwargs) = a, b
    assert len(a_args) == len(b_args) == 2
    assert a_args[0] == b_args[0]  # the compiled plan
    assert list(a_args[1]) == list(b_args[1])
    for name in a_args[1]:
        assert a_args[1][name].equals(b_args[1][name], check_metadata=True), name
    assert a_kwargs.keys() == b_kwargs.keys()
    for key, value in a_kwargs.items():
        if key == "pool_cache":
            # One cache per job, so two jobs never share an instance.
            assert isinstance(value, PoolCache), key
            assert value is not b_kwargs[key], key
            continue
        assert value == b_kwargs[key], key


def test_veto_sets_report_exactly_their_columns(tmp_path: Path) -> None:
    from decoy_engine.execution import _pipeline_multi_table as pmt
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import profile_source

    cols = [
        {"name": "c1", "strategy": "categorical", "deterministic": False, "namespace": "n1"},
        {"name": "c2", "strategy": "categorical", "deterministic": True, "namespace": "n2"},
        {"name": "s1", "strategy": "shuffle", "deterministic": False},
        {"name": "s2", "strategy": "shuffle", "deterministic": True, "namespace": "n3"},
        support.hash_col("h", "n4"),
    ]
    for c in cols[:4]:
        if c["strategy"] == "categorical":
            c["provider_config"] = {"categories": ["a", "b"]}
    src = pa.table(
        {
            "c1": pa.array(["a", "b"] * 5),
            "c2": pa.array(["a", "b"] * 5),
            "s1": pa.array(["a", "b"] * 5),
            "s2": pa.array(["a", "b"] * 5),
            "h": pa.array([f"h{i}" for i in range(10)]),
        }
    )
    cfg, _ = mt.build_job(tmp_path, {"t": (cols, src)})
    plan = compile_plan(cfg, profile_source(cfg, seed=42), decoy_engine_version="x")
    assert sorted(pmt.unseeded_random_nodes(plan)) == [("t", "s1", "shuffle")]

    # The seeded column is held whole by its own veto, not by the shuffle set above.
    assert sorted(pmt.position_keyed_deferred_nodes(plan)) == [("t", "c1", "categorical")]


def test_unseeded_strategy_set_matches_the_strategies_that_draw_from_a_fresh_rng() -> None:
    """A new unseeded path fails here until `UNSEEDED_RANDOM_STRATEGIES` knows it.

    The scan finds every strategy module that builds an unseeded generator
    (`default_rng()` with no argument, `random.Random()` with none, the global `random`
    module), and the pinned set below is what B7's job gate 9 covers."""
    import pathlib
    import re

    import decoy_engine.execution._strategies as strategies_pkg
    from decoy_engine.execution import _pipeline_multi_table as pmt

    root = pathlib.Path(strategies_pkg.__file__).parent
    unseeded = re.compile(
        r"default_rng\(\s*\)|random\.Random\(\s*\)|(?<![\w.])random\.(?:random|choice|shuffle|randint|sample)\(|uuid\.uuid4"
    )
    offenders = sorted(
        p.stem.lstrip("_") for p in root.glob("*.py") if unseeded.search(p.read_text())
    )
    assert frozenset({"shuffle"}) == pmt.UNSEEDED_RANDOM_STRATEGIES

    # Seeded, so the scan above cannot see it; it has its own pinned veto set.
    assert frozenset({"categorical"}) == pmt.POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED
    assert offenders == ["shuffle"], (
        "a strategy module draws from an unseeded generator; add it to job gate 9"
    )


def test_two_calls_of_every_repeatable_fixture_agree_and_unseeded_ones_differ(
    tmp_path: Path,
) -> None:
    """Registry sentry (plan test 2): for each strategy fixture, two independent calls
    with the same config give equal output unless `unseeded_random_nodes` reports the
    column, and each reported column differs between two calls on 1,000 rows."""
    from decoy_engine.execution import _pipeline_multi_table as pmt
    from decoy_engine.execution._strategies import SCALAR_HANDLERS
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import profile_source
    from tests.unit.execution import _auto_chunk_strategies as admitted
    from tests.unit.execution import _multi_table_sentry_fixtures as extra

    fixtures: dict[str, tuple[list[dict[str, Any]], dict[str, pa.Array]]] = {
        **{k: v for k, v in admitted.STRATEGY_FIXTURES.items()},
        **extra.EXTRA_FIXTURES,
    }
    covered = {key.split(":")[0] for key in fixtures}
    assert covered == set(SCALAR_HANDLERS), (
        sorted(set(SCALAR_HANDLERS) ^ covered),
        "the fixtures and the live registry differ; add or remove one in "
        "_multi_table_sentry_fixtures",
    )
    for key, (columns, data) in sorted(fixtures.items()):
        n = 1000
        wide = {name: _widen(arr, n) for name, arr in data.items()}
        table = pa.table(wide)
        workdir = tmp_path / key.replace(":", "_")
        workdir.mkdir()
        cfg, _ = mt.build_job(workdir, {"t": (columns, table)})
        plan = compile_plan(cfg, profile_source(cfg, seed=42), decoy_engine_version="x")
        reported = {col for _t, col, _s in pmt.unseeded_random_nodes(plan)}
        deferred = {col for _t, col, _s in pmt.position_keyed_deferred_nodes(plan)}
        assert not reported & deferred, "a column cannot be both fresh-generator and seeded"
        a = run_pipeline(cfg, sources={"t": table}, **mt.kw(auto_chunk=False))
        b = run_pipeline(cfg, sources={"t": table}, **mt.kw(auto_chunk=False))
        for name in a.outputs["t"].column_names:
            same = (
                a.outputs["t"].column(name).to_pylist() == b.outputs["t"].column(name).to_pylist()
            )
            if name in reported:
                assert not same, f"{key}:{name} is reported unseeded but repeats"
            else:
                assert same, f"{key}:{name} differs between calls but is not reported unseeded"


def _widen(arr: pa.Array, n: int) -> pa.Array:
    values = arr.to_pylist()
    return pa.array([values[i % len(values)] for i in range(n)], arr.type)


def test_row_errors_still_aggregate_when_the_job_does_not_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quarantine-free job with unseeded randomness is a golden no-split even with row errors."""
    bad = pa.table({"d": pa.array(["2020-01-01"] * (mt.BIG - 1) + ["not-a-date"])})
    cols = [
        {
            "name": "d",
            "strategy": "date_shift",
            "namespace": "d_ns",
            "provider_config": {"min_days": -5, "max_days": 5, "date_format": "%Y-%m-%d"},
        }
    ]
    random_cols, random_table = _random_columns("shuffle")
    cfg, sources = mt.build_job(
        tmp_path,
        {"big": (cols, bad), "tiny": (random_cols, random_table.slice(0, mt.SMALL))},
    )
    calls = mt.spy_split(monkeypatch)
    with pytest.raises(RowErrorsFailedError):
        run_pipeline(cfg, sources=sources, **mt.kw())
    assert calls == []

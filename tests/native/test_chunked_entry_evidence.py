"""`run_mask_chunked`: route evidence, pool cache, adapter, threads, drift error, surface.

Covers acceptance tests 8, 8a, 8b, 8c, 9, 10 and 11 of the dispatcher production
contract.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

import decoy_engine
from decoy_engine import run_mask_chunked
from decoy_engine.errors import DecoyError
from decoy_engine.execution import _errors as _execution_errors
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.native import _crypto_ext
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._dispatch import (
    NativeChunkSchemaDriftError,
    NativeRouteEvidence,
    run_native_or_oracle_chunked,
)
from decoy_engine.generation.pool import PoolBuilder, PoolCache
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    faker_col,
    force_oracle,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    split,
    string_source,
    truncate,
)

REPO = Path(__file__).resolve().parents[2]
_CHUNK = 4


def _source(n: int = 11) -> pa.Table:
    return pa.table(
        {
            "r": pa.array([f"r{i}" for i in range(n)], pa.string()),
            "t": pa.array([None if i % 4 == 0 else f"abcdef{i}" for i in range(n)], pa.string()),
            "p": pa.array(range(n), pa.int64()),
            "h": pa.array(
                [None if i % 5 == 3 else f"user{i % 4}@x.com" for i in range(n)], pa.string()
            ),
            "f": pa.array([None if i % 6 == 1 else f"first{i % 5}" for i in range(n)], pa.string()),
            "c": pa.array(["2020-03-15"] * n, pa.string()),
        }
    )


def _run(
    config: dict[str, Any], names: list[str], **kw: Any
) -> tuple[list[pa.Table], list[Any], NativeRouteEvidence]:
    sink: list[Any] = []
    evidence: list[NativeRouteEvidence] = []
    chunks = split(_source().select(names), kw.pop("size", _CHUNK))
    kw.setdefault("key_provider", key_provider())
    out = list(
        run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            chunk_result_sink=sink,
            route_evidence_sink=evidence,
            **kw,
        )
    )
    return out, sink, evidence[0]


def _columns(agg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["column"]: c for c in agg["columns"]}


def _assert_json_safe(sink: list[Any], agg: dict[str, Any]) -> None:
    for res in sink:
        payload = res.quality_metrics["chunked_route"]
        assert json.loads(json.dumps(payload, allow_nan=False)) == payload
    assert json.loads(json.dumps(agg, allow_nan=False)) == agg


# ---------------------------------------------------------------------------
# 8a. The legacy evidence object keeps its meaning.
# ---------------------------------------------------------------------------


def _legacy(config: dict[str, Any], chunks: list[pa.Table]) -> NativeRouteEvidence:
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=sink,
        )
    )
    return sink[0]


@pytest.mark.parametrize(
    "columns, names",
    [
        ([redact("r"), truncate("t"), passthrough("p")], ["r", "t", "p"]),
        ([redact("r"), force_oracle("c")], ["r", "c"]),
    ],
    ids=["native_route", "oracle_route"],
)
def test_legacy_route_evidence_is_unchanged(
    columns: list[dict[str, Any]], names: list[str]
) -> None:
    config = make_config(columns)
    _, _, new = _run(config, names)
    old = _legacy(config, split(_source().select(names), _CHUNK))
    assert new.native_admitted == old.native_admitted
    assert new.reroute_reason == old.reroute_reason
    assert new.node_routes == old.node_routes
    assert new.kernel_calls == old.kernel_calls
    assert {n.route for n in new.node_routes} <= {"native_kernel", "native_pool", "oracle"}


def test_valid_zero_chunk_call_reports_empty_input() -> None:
    evidence: list[NativeRouteEvidence] = []
    out = list(
        run_mask_chunked(
            make_config([redact("r")]),
            [],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=evidence,
        )
    )
    assert out == []
    assert evidence[0].native_admitted is False
    assert evidence[0].reroute_reason == "empty_input"
    assert evidence[0].node_routes == ()


# ---------------------------------------------------------------------------
# 8. Planned and executed backends.
# ---------------------------------------------------------------------------


def test_native_route_reports_planned_and_executed_backends_without_companion() -> None:
    config = make_config([redact("r"), truncate("t"), passthrough("p")])
    _, sink, evidence = _run(config, ["r", "t", "p"])
    agg = aggregate_chunked_route_evidence(sink)
    assert evidence.native_admitted is True
    assert (
        agg["table"] == TABLE and agg["native_admitted"] is True and agg["reroute_reason"] is None
    )
    cols = _columns(agg)
    assert set(cols) == {"r", "t", "p"}
    for name, strategy in (("r", "redact"), ("t", "truncate"), ("p", "passthrough")):
        assert cols[name]["strategy"] == strategy
        assert cols[name]["planned_backend"] == "arrow_python"
        assert cols[name]["executed_backend"] == "arrow_python"
        assert cols[name]["calls"] == len(sink) == 3
        assert cols[name]["elapsed_ms"] >= 0
    _assert_json_safe(sink, agg)


@NEEDS_COMPANION
def test_native_route_reports_companion_and_pool_backends() -> None:
    config = make_config([hash_col("h"), faker_col("f"), redact("r")])
    _, sink, evidence = _run(config, ["h", "f", "r"])
    agg = aggregate_chunked_route_evidence(sink)
    assert evidence.native_admitted is True
    cols = _columns(agg)
    assert (cols["h"]["planned_backend"], cols["h"]["executed_backend"]) == ("rust_companion",) * 2
    assert (cols["f"]["planned_backend"], cols["f"]["executed_backend"]) == (
        "rust_pool_select",
    ) * 2
    assert cols["r"]["executed_backend"] == "arrow_python"
    assert all(c["calls"] == len(sink) for c in cols.values())
    _assert_json_safe(sink, agg)


class _PlainAdapter:
    """Not a pandas adapter by type; delegates so the oracle route can run."""

    def __init__(self) -> None:
        self.calls = 0
        self._inner = PandasExecutionAdapter()

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._inner.run(*args, **kwargs)


def _veto_case(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[str], dict[str, Any], str, dict[str, str]]:
    """Returns (config, source columns, run kwargs, reason fragment, planned backends)."""
    arrow = {"r": "arrow_python", "t": "arrow_python", "p": "arrow_python"}
    if case == "missing_companion":
        monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
        return (
            make_config([hash_col("h"), redact("r")]),
            ["h", "r"],
            {},
            "crypto_extension_unavailable",
            {"h": "rust_companion", "r": "arrow_python"},
        )
    if case == "incompatible_companion":
        monkeypatch.setattr(_crypto_ext, "_EXPECTED_ABI_VERSION", "not-a-real-abi-tag")
        return (
            make_config([hash_col("h"), redact("r")]),
            ["h", "r"],
            {},
            "crypto_extension_unavailable",
            {"h": "rust_companion", "r": "arrow_python"},
        )
    if case == "schema_veto":
        # The config names a column the source does not carry.
        return (
            make_config([redact("r"), truncate("t"), passthrough("q")]),
            ["r", "t"],
            {},
            "missing_configured_columns:['q']",
            {"r": "arrow_python", "t": "arrow_python", "q": "arrow_python"},
        )
    if case == "when_veto":
        config = make_config([redact("r"), truncate("t"), passthrough("p")])
        # Outside the closed `when` grammar, so the native route declines it.
        config["tables"][0]["columns"][0]["when"] = "p + 0 > 2"
        return config, ["r", "t", "p"], {}, "when_predicate_outside_native_subset:r", arrow
    if case == "adapter_veto":
        return (
            make_config([redact("r"), truncate("t"), passthrough("p")]),
            ["r", "t", "p"],
            {"adapter": _PlainAdapter()},
            "adapter_requested",
            arrow,
        )
    return (  # mixed table: natively capable columns beside a vetoed one
        make_config([redact("r"), passthrough("p"), force_oracle("c")]),
        ["r", "p", "c"],
        {},
        "fallback_policy_not_native:c:python_only",
        {"r": "arrow_python", "p": "arrow_python", "c": "pandas_oracle"},
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("missing_companion", marks=[]),
        pytest.param("incompatible_companion", marks=NEEDS_COMPANION),
        "schema_veto",
        "when_veto",
        "adapter_veto",
        "mixed_table",
    ],
)
def test_vetoed_tables_report_planned_native_and_executed_oracle(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, names, kwargs, reason, planned = _veto_case(case, monkeypatch)
    _, sink, evidence = _run(config, names, **kwargs)
    agg = aggregate_chunked_route_evidence(sink)
    assert evidence.native_admitted is False
    assert reason in (evidence.reroute_reason or "")
    assert agg["native_admitted"] is False and reason in agg["reroute_reason"]
    cols = _columns(agg)
    assert set(cols) == set(planned)
    for name, backend in planned.items():
        assert cols[name]["planned_backend"] == backend, name
        assert cols[name]["executed_backend"] == "pandas_oracle", name
        assert cols[name]["calls"] == len(sink) == 3, name
    _assert_json_safe(sink, agg)


def test_unconfigured_admitted_reports_planned_and_executed_native_backends() -> None:
    config = make_config([redact("r"), truncate("t")])
    _, sink, evidence = _run(config, ["r", "t", "p"])
    agg = aggregate_chunked_route_evidence(sink)
    assert evidence.native_admitted is True and evidence.reroute_reason is None
    assert agg["native_admitted"] is True and agg["reroute_reason"] is None
    cols = _columns(agg)
    assert set(cols) == {"r", "t"}
    for name in ("r", "t"):
        assert cols[name]["planned_backend"] == "arrow_python"
        assert cols[name]["executed_backend"] == "arrow_python"
        assert cols[name]["calls"] == len(sink) == 3
    _assert_json_safe(sink, agg)


def test_fk_table_runs_on_the_oracle_and_lists_its_configured_columns() -> None:
    from tests.unit.execution.test_chunked_fk_gate_kills import _hash_config

    chunks = [pa.table({"customer_id": pa.array([1, 2, 3], pa.int64())})] * 2
    sink: list[Any] = []
    evidence: list[NativeRouteEvidence] = []
    out = list(
        run_mask_chunked(
            _hash_config(),
            chunks,
            table="orders",
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            chunk_result_sink=sink,
            route_evidence_sink=evidence,
        )
    )
    assert len(out) == 2 and len(sink) == 2
    assert evidence[0].reroute_reason == "fk_relationship_not_native_route"
    cols = _columns(aggregate_chunked_route_evidence(sink))
    assert set(cols) == {"customer_id"}
    assert cols["customer_id"]["executed_backend"] == "pandas_oracle"
    assert cols["customer_id"]["calls"] == 2


def test_unconfigured_columns_are_not_listed_in_the_evidence() -> None:
    config = make_config([redact("r")])
    _, sink, _ = _run(config, ["r", "p"])
    agg = aggregate_chunked_route_evidence(sink)
    assert set(_columns(agg)) == {"r"}


# ---------------------------------------------------------------------------
# 8b. One shared PoolCache.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_given_pool_cache_is_reused_across_routes_and_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[int] = []
    real_build = PoolBuilder.build

    def _counting(self: Any, *a: Any, **k: Any) -> Any:
        builds.append(1)
        return real_build(self, *a, **k)

    monkeypatch.setattr(PoolBuilder, "build", _counting)
    cache = PoolCache()
    native_cfg = make_config([faker_col("f"), redact("r")])
    oracle_cfg = make_config([faker_col("f"), redact("r"), force_oracle("c")])

    _, _, ev1 = _run(native_cfg, ["f", "r"], pool_cache=cache)
    assert ev1.native_admitted is True and len(builds) == 1
    _, _, ev2 = _run(oracle_cfg, ["f", "r", "c"], pool_cache=cache)
    assert ev2.native_admitted is False and len(builds) == 1
    _, _, ev3 = _run(native_cfg, ["f", "r"], pool_cache=cache)
    assert ev3.native_admitted is True and len(builds) == 1


# ---------------------------------------------------------------------------
# 8c. A non-pandas adapter forces the oracle route and is used by it.
# ---------------------------------------------------------------------------


def test_non_pandas_adapter_forces_oracle_route_and_is_used() -> None:
    adapter = _PlainAdapter()
    config = make_config([redact("r"), passthrough("p")])
    _, sink, evidence = _run(config, ["r", "p"], adapter=adapter)
    assert evidence.reroute_reason == "adapter_requested" and evidence.native_admitted is False
    assert adapter.calls == len(sink) == 3


@pytest.mark.parametrize("adapter", [None, PandasExecutionAdapter()], ids=["none", "pandas"])
def test_default_and_pandas_adapters_do_not_veto_the_native_route(adapter: Any) -> None:
    config = make_config([redact("r"), passthrough("p")])
    _, _, evidence = _run(config, ["r", "p"], adapter=adapter)
    assert evidence.native_admitted is True


# ---------------------------------------------------------------------------
# 9. native_threads.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, True, "2"], ids=["zero", "negative", "bool", "str"])
def test_invalid_native_threads_is_rejected_before_any_work(bad: Any) -> None:
    with pytest.raises(_execution_errors.ExecutionError) as info:
        run_mask_chunked(
            make_config([redact("r")]),
            split(_source().select(["r"]), _CHUNK),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            native_threads=bad,
        )
    assert info.value.code == "invalid_native_threads"


def test_output_does_not_depend_on_native_threads() -> None:
    config = make_config([redact("r"), truncate("t"), passthrough("p")])
    one, *_ = _run(config, ["r", "t", "p"], native_threads=1)
    four, *_ = _run(config, ["r", "t", "p"], native_threads=4)
    assert pa.concat_tables(one).equals(pa.concat_tables(four))


@NEEDS_COMPANION
def test_native_threads_reaches_the_hash_kernel_and_output_is_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution.native import _kernels_keyed
    from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel

    seen: list[Any] = []
    real = load_compiled_crypto_kernel()

    class _Recording:
        def derive_batch(self, values: Any, *, native_threads: Any = None, **kw: Any) -> Any:
            seen.append(native_threads)
            return real.derive_batch(values, native_threads=native_threads, **kw)

    monkeypatch.setattr(_kernels_keyed, "load_compiled_crypto_kernel", lambda: _Recording())
    config = make_config([hash_col("h"), passthrough("p")])
    one, *_ = _run(config, ["h", "p"], native_threads=1)
    four, *_ = _run(config, ["h", "p"], native_threads=4)
    assert pa.concat_tables(one).equals(pa.concat_tables(four))
    assert set(seen) == {1, 4}


# ---------------------------------------------------------------------------
# 10. Schema drift raises a coded, mappable error.
# ---------------------------------------------------------------------------


def _drift_chunks(kind: str) -> list[pa.Table]:
    good = string_source(4)
    if kind == "type_changed":
        later = good.set_column(1, "p", pa.array(["1", "2", "3", "4"], pa.string()))
    else:
        later = good.drop_columns(["p"])
    return [good, later]


@pytest.mark.parametrize(
    "kind, detail", [("type_changed", "type_changed:p"), ("missing", "missing:['p']")]
)
def test_schema_drift_is_an_execution_error_and_a_decoy_error(kind: str, detail: str) -> None:
    config = make_config([redact("s"), passthrough("p")])

    def go() -> Any:
        return list(
            run_mask_chunked(
                config,
                _drift_chunks(kind),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )

    caught: list[BaseException] = []
    try:
        go()
    except _execution_errors.ExecutionError as exc:
        caught.append(exc)
    assert len(caught) == 1, "an `except ExecutionError` clause must catch the drift error"
    try:
        go()
    except DecoyError as exc:
        caught.append(exc)
    assert len(caught) == 2, "an `except DecoyError` clause must catch it too"

    err = caught[0]
    assert isinstance(err, NativeChunkSchemaDriftError)
    assert err.code == "native_chunk_schema_drift"
    assert "chunk 1" in err.message and TABLE in err.message
    assert str(err) == err.message
    assert not str(err).startswith("[")
    assert err.table == TABLE and err.chunk_index == 1 and err.detail.startswith(detail)


# ---------------------------------------------------------------------------
# 11. Public surface and release bookkeeping.
# ---------------------------------------------------------------------------


def test_run_mask_chunked_is_public() -> None:
    from decoy_engine import execution
    from decoy_engine import run_mask_chunked as top_level

    assert top_level is execution.run_mask_chunked
    assert "run_mask_chunked" in decoy_engine.__all__
    assert "run_mask_chunked" in execution.__all__


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a : text.index(end, a + len(start))]


def test_release_bookkeeping_names_the_new_entry_point() -> None:
    contract = (REPO / "docs/compatibility-contract.md").read_text(encoding="utf-8")
    assert "run_mask_chunked" in _section(contract, "### 3.4", "### 3.5")
    assert "run_mask_chunked" in _section(
        contract, "## 9. Pre-flight checklist", "## Pre-GA corpus"
    )
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "run_mask_chunked" in _section(changelog, "## [0.7.0]", "\n## [0.")
    assert re.search(r"`run_mask_chunked\(", (REPO / "README.md").read_text(encoding="utf-8"))
    assert "_chunked_entry.py" in (REPO / "CODEMAP.md").read_text(encoding="utf-8")

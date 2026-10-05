"""`run_mask_chunked`: parameters, validation parity, vault, chunk results, row offset.

Covers acceptance tests 1, 4, 5, 6 and 7 of the dispatcher production contract.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _chunked_dgrn
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.native._dispatch import NativeRouteEvidence
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.vault import VaultWriter, collect_vault_entries
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    FORCE_ORACLE_VALUE,
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
    vault_key,
)
from tests.unit.execution.test_chunked_fk_gate_kills import _hash_config as _fk_config


def _forced_columns(config: dict[str, Any]) -> list[str]:
    """Columns of `config` whose strategy the chunked dispatcher still vetoes (`group_key`)."""
    return [
        c["name"]
        for t in config.get("tables", ())
        for c in t.get("columns", ())
        if c.get("strategy") == "group_key"
    ]


def _entry(config: dict[str, Any], chunks: Any, **kw: Any) -> Any:
    """`run_mask_chunked` that owns a route-evidence sink and, for a config carrying a
    forcing column, proves the table stayed on the oracle for the exact reason."""
    kw.setdefault("key_provider", key_provider())
    sink = kw.setdefault("route_evidence_sink", [])
    out = run_mask_chunked(
        config, chunks, table=kw.pop("table", TABLE), engine_version=ENGINE_VERSION, **kw
    )
    for column in _forced_columns(config):
        assert len(sink) == 1, sink
        assert sink[0].native_admitted is False, sink[0]
        assert f"group_key_not_native_chunked_route:{column}" in (sink[0].reroute_reason or "")
    return out


def _oracle(config: dict[str, Any], chunks: Any, **kw: Any) -> Any:
    kw.setdefault("key_provider", key_provider())
    return run_mask_pipeline_chunked(
        config, chunks, table=kw.pop("table", TABLE), engine_version=ENGINE_VERSION, **kw
    )


class _RecordingAdapter(PandasExecutionAdapter):
    """The real pandas adapter, remembering every raw result it produced."""

    def __init__(self) -> None:
        super().__init__()
        self.raw: list[Any] = []

    def run(self, *args: Any, **kwargs: Any) -> Any:
        result = super().run(*args, **kwargs)
        self.raw.append(result)
        return result


def _oracle_route_config() -> dict[str, Any]:
    return make_config([redact("s"), passthrough("p"), force_oracle("c")])


def _oracle_route_source(n: int = 9) -> pa.Table:
    return string_source(n).append_column("c", pa.array([FORCE_ORACLE_VALUE] * n, pa.string()))


# ---------------------------------------------------------------------------
# 1. Every oracle parameter is accepted and forwarded.
# ---------------------------------------------------------------------------


def test_every_oracle_parameter_reaches_the_shared_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _chunked_oracle

    pre: list[dict[str, Any]] = []
    masked: list[dict[str, Any]] = []
    real_pre = _chunked_oracle._oracle_preflight
    real_masked = _chunked_oracle._oracle_masked

    def _spy_pre(*a: Any, **k: Any) -> Any:
        pre.append(k)
        return real_pre(*a, **k)

    def _spy_masked(*a: Any, **k: Any) -> Any:
        masked.append(k)
        return real_masked(*a, **k)

    monkeypatch.setattr(_chunked_oracle, "_oracle_preflight", _spy_pre)
    monkeypatch.setattr(_chunked_oracle, "_oracle_masked", _spy_masked)

    registry = copy.copy(get_default_registry())
    adapter = _RecordingAdapter()
    vault = VaultWriter(vault_key())
    sink: list[Any] = []
    kp = key_provider()
    source = _oracle_route_source()
    out = list(
        _entry(
            _oracle_route_config(),
            split(source, 4),
            registry=registry,
            adapter=adapter,
            vault_writer=vault,
            chunk_result_sink=sink,
            key_provider=kp,
            base_row_offset=7,
            native_threads=2,
            route_evidence_sink=[],
        )
    )
    assert len(out) == 3
    assert len(pre) == 1 and len(masked) == 1
    assert pre[0]["registry"] is registry
    assert pre[0]["adapter"] is adapter
    assert pre[0]["vault_writer"] is vault
    assert pre[0]["key_provider"] is kp
    assert pre[0]["base_row_offset"] == 7
    assert masked[0]["chunk_result_sink"] is sink
    assert masked[0]["vault_writer"] is vault
    assert masked[0]["base_row_offset"] == 7
    assert callable(masked[0]["on_chunk"])
    assert len(adapter.raw) == 3 and len(sink) == 3


def test_public_oracle_runs_through_the_same_shared_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution import _chunked_oracle

    seen: list[str] = []
    real_pre = _chunked_oracle._oracle_preflight
    real_masked = _chunked_oracle._oracle_masked
    monkeypatch.setattr(
        _chunked_oracle,
        "_oracle_preflight",
        lambda *a, **k: (seen.append("pre"), real_pre(*a, **k))[1],
    )
    monkeypatch.setattr(
        _chunked_oracle,
        "_oracle_masked",
        lambda *a, **k: (seen.append(f"masked:{k.get('on_chunk')}"), real_masked(*a, **k))[1],
    )
    list(_oracle(_oracle_route_config(), split(_oracle_route_source(), 4)))
    assert seen == ["pre", "masked:None"]


@NEEDS_COMPANION
def test_registry_reaches_native_pool_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.native import _chunked_entry

    seen: list[Any] = []
    real = _chunked_entry._resolve_faker_pools

    def _spy(*a: Any, **k: Any) -> Any:
        seen.append(k.get("registry"))
        return real(*a, **k)

    monkeypatch.setattr(_chunked_entry, "_resolve_faker_pools", _spy)
    registry = copy.copy(get_default_registry())
    source = pa.table({"f": pa.array(["a", "b", None, "c"], pa.string())})
    evidence: list[NativeRouteEvidence] = []
    list(
        _entry(
            make_config([faker_col("f")]),
            split(source, 2),
            registry=registry,
            route_evidence_sink=evidence,
        )
    )
    assert evidence[0].native_admitted is True
    assert seen == [registry]


# ---------------------------------------------------------------------------
# 4. Validation parity: same error, before any chunk beyond the first.
# ---------------------------------------------------------------------------


def _poisoned(first: pa.Table) -> Any:
    def gen() -> Any:
        yield first
        raise AssertionError("a chunk after the first was consumed before validation finished")

    return gen()


def _failure(call: Callable[[], Any]) -> tuple[type[BaseException], Any]:
    with pytest.raises(Exception) as info:
        call()
    return type(info.value), getattr(info.value, "code", None)


def _with_extra(base: list[dict[str, Any]], extra: dict[str, Any]) -> dict[str, Any]:
    return make_config([*base, extra])


def _generate_table_config() -> dict[str, Any]:
    cfg = make_config([redact("s"), passthrough("p")])
    cfg["tables"][0]["generate_columns"] = [{"name": "g", "type": "sequence"}]
    return cfg


_ADMITTED = [redact("s"), passthrough("p")]
_STR = string_source(6)
_REJECTIONS: dict[str, Callable[[], tuple[dict[str, Any], pa.Table, str]]] = {
    "generate_table": lambda: (_generate_table_config(), _STR, TABLE),
    "strategy_not_chunk_safe": lambda: (
        _with_extra(_ADMITTED, {"name": "x", "strategy": "shuffle"}),
        _STR.append_column("x", pa.array(["a"] * 6)),
        TABLE,
    ),
    "conditions_unmet": lambda: (
        _with_extra(
            _ADMITTED,
            {"name": "x", "strategy": "faker", "provider": "person_first_name", "namespace": "n"},
        ),
        _STR.append_column("x", pa.array(["a"] * 6)),
        TABLE,
    ),
    "table_unknown": lambda: (make_config(_ADMITTED), _STR, "missing_table"),
}
for _strategy in ("windowed_date", "text_mask", "code_set", "bucket_perturb", "group_key"):

    def _when_case(strategy: str = _strategy) -> tuple[dict[str, Any], pa.Table, str]:
        cfg = _with_extra(_ADMITTED, {"name": "x", "strategy": strategy, "namespace": "n"})
        cfg["tables"][0]["columns"][-1]["when"] = "p == 1"
        return cfg, _STR.append_column("x", pa.array(["a"] * 6)), TABLE

    _REJECTIONS[f"{_strategy}_when"] = _when_case


def _fk_case(**extra: Any) -> Callable[[], tuple[dict[str, Any], pa.Table, str]]:
    def build() -> tuple[dict[str, Any], pa.Table, str]:
        cfg = _fk_config(**extra)
        return cfg, pa.table({"customer_id": pa.array([1, 2, 3], pa.int64())}), "orders"

    return build


_REJECTIONS["fk_parent_when"] = _fk_case(parent_extra={"when": "region == 'US'"})
_REJECTIONS["fk_child_when"] = _fk_case(child_extra={"when": "region == 'US'"})
_REJECTIONS["fk_endpoint_not_scalar"] = _fk_case(parent_extra={"provider": "name.full_name"})


@pytest.mark.parametrize("case", sorted(_REJECTIONS))
def test_compatibility_rejection_is_identical_on_both_entry_points(case: str) -> None:
    config, first, table = _REJECTIONS[case]()
    with pytest.raises(Exception) as direct:
        check_chunked_compatibility(config, table=table, registry=get_default_registry())
    want = (type(direct.value), direct.value.code)
    assert want[1] is not None

    oracle = _failure(lambda: _oracle(config, _poisoned(first), table=table))
    entry = _failure(lambda: _entry(config, _poisoned(first), table=table))
    assert oracle == want
    assert entry == want


def _ga_keyed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr("decoy_engine.keyprovider.is_pre_ga", lambda: False)
    return make_config([redact("s"), hash_col("h")])


def _bad_corpus(tmp_path: Any) -> dict[str, Any]:
    return make_config(
        [
            {
                "name": "code",
                "strategy": "code_set",
                "provider_config": {
                    "code_set": "custom",
                    "corpus_source": f"customer:{tmp_path / 'does_not_exist.parquet'}",
                },
            }
        ]
    )


def _bucket_without_namespace() -> dict[str, Any]:
    return make_config(
        [
            {
                "name": "d",
                "strategy": "bucket_perturb",
                "provider_config": {"date_format": "%Y-%m-%d", "bucket": "month"},
            }
        ]
    )


@pytest.mark.parametrize("zero_chunks", [True, False], ids=["zero_chunks", "first_chunk_only"])
@pytest.mark.parametrize("case", ["keyed_no_secret", "bad_corpus", "bucket_namespace", "vault_key"])
def test_eager_oracle_checks_fail_the_same_way_with_no_side_effects(
    case: str, zero_chunks: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    first = pa.table(
        {
            "s": pa.array(["a", "b"], pa.string()),
            "h": pa.array(["a", "b"], pa.string()),
            "code": pa.array(["E11.9", "E11.8"], pa.string()),
            "d": pa.array(["2024-01-15", "2024-02-15"], pa.string()),
        }
    )
    kp: Any = key_provider()
    vault = VaultWriter(vault_key())
    if case == "keyed_no_secret":
        config, kp = _ga_keyed(monkeypatch), None
    elif case == "bad_corpus":
        config = _bad_corpus(tmp_path)
    elif case == "bucket_namespace":
        config = _bucket_without_namespace()
    else:
        config = make_config([redact("s"), passthrough("p")])
        vault = VaultWriter(b"\x09" * 32)
    source = first.select(
        [c for c in first.column_names if c in {c["name"] for c in config["tables"][0]["columns"]}]
    )
    chunks = (lambda: iter(())) if zero_chunks else (lambda: _poisoned(source))
    sink: list[Any] = []
    evidence: list[NativeRouteEvidence] = []

    oracle = _failure(
        lambda: _oracle(config, chunks(), key_provider=kp, vault_writer=vault, chunk_result_sink=[])
    )
    entry = _failure(
        lambda: _entry(
            config,
            chunks(),
            key_provider=kp,
            vault_writer=vault,
            chunk_result_sink=sink,
            route_evidence_sink=evidence,
        )
    )
    assert entry == oracle
    assert entry[1] is not None or entry[0].__name__ == "KeyedStrategyRequiresSecret"
    assert sink == [] and evidence == [] and vault._entries == set()


def test_non_empty_schema_gate_fails_the_same_way_before_later_chunks() -> None:
    config = make_config(
        [
            {
                "name": "d",
                "strategy": "bucket_perturb",
                "namespace": "ns_b",
                "provider_config": {"date_format": "%Y-%m-%d", "bucket": "month"},
            }
        ]
    )
    first = pa.table({"d": pa.array([20240115, 20240215], pa.int64())})
    oracle = _failure(lambda: _oracle(config, _poisoned(first)))
    entry = _failure(lambda: _entry(config, _poisoned(first)))
    assert entry == oracle
    assert entry[1] is not None


# ---------------------------------------------------------------------------
# 5. Vault: native route collects the same entries; a mis-keyed writer is rejected.
# ---------------------------------------------------------------------------


def _vault_columns(with_hash: bool) -> list[dict[str, Any]]:
    cols = [{**truncate("t"), "namespace": "ns_t", "vault": True}, passthrough("p")]
    if with_hash:
        cols.append({**hash_col("h"), "vault": True})
    return cols


def _vault_source() -> pa.Table:
    return pa.table(
        {
            "t": pa.array(
                ["abcdef", "ghijkl", None, "abcdef", "mnopqr", "stuvwx", "yzabcd"], pa.string()
            ),
            "p": pa.array(range(7), pa.int64()),
            "h": pa.array(["u1", "u2", "u3", None, "u1", "u5", "u6"], pa.string()),
        }
    )


@pytest.mark.parametrize("with_hash", [False, pytest.param(True, marks=NEEDS_COMPANION)])
def test_native_route_vault_entries_match_the_oracle_route(with_hash: bool) -> None:
    columns = _vault_columns(with_hash)
    config = make_config(columns)
    names = [c["name"] for c in columns]
    source = _vault_source().select(names)
    chunks = split(source, 3)

    native_vault, oracle_vault = VaultWriter(vault_key()), VaultWriter(vault_key())
    evidence: list[NativeRouteEvidence] = []
    native_out = list(
        _entry(config, chunks, vault_writer=native_vault, route_evidence_sink=evidence)
    )
    oracle_out = list(_oracle(config, chunks, vault_writer=oracle_vault))
    assert evidence[0].native_admitted is True
    assert native_vault._entries == oracle_vault._entries
    assert native_vault._entries, "the vault must have collected something"
    expected: set[tuple[str, str, str]] = set()
    for src, out in zip(chunks, native_out, strict=True):
        expected.update(collect_vault_entries(config, {TABLE: src}, {TABLE: out}))
    assert native_vault._entries == expected
    assert [c.num_rows for c in native_out] == [c.num_rows for c in oracle_out]


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_vault_writer_keyed_differently_is_rejected_on_both_routes(route: str) -> None:
    columns = _vault_columns(False)
    source = _vault_source().select(["t", "p"])
    if route == "oracle":
        columns.append(force_oracle("c"))
        source = source.append_column(
            "c", pa.array([FORCE_ORACLE_VALUE] * source.num_rows, pa.string())
        )
    wrong = VaultWriter(b"\x07" * 32)
    code = _failure(lambda: _entry(make_config(columns), split(source, 3), vault_writer=wrong))
    assert code[1] == "vault_key_mismatch"
    assert wrong._entries == set()


# ---------------------------------------------------------------------------
# 6. Chunk results.
# ---------------------------------------------------------------------------


def _native_config() -> dict[str, Any]:
    return make_config([redact("r"), truncate("t1"), truncate("t2", 2), passthrough("p")])


def _native_source(n: int = 11) -> pa.Table:
    return pa.table(
        {
            "r": pa.array([f"r{i}" for i in range(n)], pa.string()),
            "t1": pa.array([None if i % 4 == 0 else f"abcdef{i}" for i in range(n)], pa.string()),
            "t2": pa.array([f"zyxwvu{i}" for i in range(n)], pa.string()),
            "p": pa.array(range(n), pa.int64()),
        }
    )


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_one_result_per_chunk_is_appended_before_each_yield(route: str) -> None:
    if route == "native":
        config, source = _native_config(), _native_source()
    else:
        config, source = _oracle_route_config(), _oracle_route_source(11)
    sink: list[Any] = []
    it = iter(_entry(config, split(source, 4), chunk_result_sink=sink))
    seen: list[int] = []
    for _ in range(3):
        next(it)
        seen.append(len(sink))
    assert seen == [1, 2, 3]
    assert list(it) == [] and len(sink) == 3
    for res in sink:
        assert res.outputs.keys() == {TABLE}
        assert res.quality_metrics["chunked_route"]["table"] == TABLE


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_result_output_is_the_yielded_table_schema_and_metadata_included(route: str) -> None:
    if route == "native":
        config, source = _native_config(), _native_source()
    else:
        config, source = _oracle_route_config(), _oracle_route_source(11)
    sink: list[Any] = []
    out = list(_entry(config, split(source, 4), chunk_result_sink=sink))
    for table, res in zip(out, sink, strict=True):
        assert res.outputs[TABLE].equals(table, check_metadata=True)
        assert res.outputs[TABLE].schema.equals(table.schema, check_metadata=True)


def test_enriched_oracle_route_result_keeps_the_adapters_figures() -> None:
    adapter = _RecordingAdapter()
    sink: list[Any] = []
    list(
        _entry(
            _oracle_route_config(),
            split(_oracle_route_source(11), 4),
            adapter=adapter,
            chunk_result_sink=sink,
        )
    )
    assert len(sink) == len(adapter.raw) == 3
    for got, raw in zip(sink, adapter.raw, strict=True):
        assert got.timings == raw.timings and got.timings
        assert got.boundary_conversion_ms == raw.boundary_conversion_ms
        assert got.warnings == raw.warnings
        assert got.row_errors == raw.row_errors
        assert {
            k: v for k, v in got.quality_metrics.items() if k != "chunked_route"
        } == raw.quality_metrics


def test_native_route_per_column_timings_and_aggregate() -> None:
    from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence

    sink: list[Any] = []
    evidence: list[NativeRouteEvidence] = []
    chunks = split(_native_source(), 4)
    list(_entry(_native_config(), chunks, chunk_result_sink=sink, route_evidence_sink=evidence))
    assert evidence[0].native_admitted is True
    assert len(sink) == 3
    for res in sink:
        assert res.warnings == () and res.row_errors == ()
        assert res.boundary_conversion_ms == 0.0
        keyed = [(t.strategy_type, t.column) for t in res.timings]
        assert sorted(keyed) == [
            ("passthrough", "p"),
            ("redact", "r"),
            ("truncate", "t1"),
            ("truncate", "t2"),
        ]
        assert all(t.elapsed_ms >= 0 and t.peak_memory_delta_kb == 0 for t in res.timings)
    agg = aggregate_chunked_route_evidence(sink)
    for col in agg["columns"]:
        per_chunk = [
            next(
                c
                for c in r.quality_metrics["chunked_route"]["columns"]
                if c["column"] == col["column"]
            )
            for r in sink
        ]
        assert col["calls"] == 3
        assert col["elapsed_ms"] == pytest.approx(sum(c["elapsed_ms"] for c in per_chunk))
    # The per-column samples come from the one timer the path already runs.
    by_strategy: dict[str, float] = {}
    for res in sink:
        for t in res.timings:
            by_strategy[t.strategy_type] = by_strategy.get(t.strategy_type, 0.0) + t.elapsed_ms
    for strategy, total_ms in by_strategy.items():
        assert evidence[0].kernel_elapsed_s[strategy] * 1000.0 == pytest.approx(
            total_ms, rel=1e-6, abs=1e-6
        )


def test_native_hot_path_never_samples_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.instrumentation import timing

    def _boom(*_a: Any, **_k: Any) -> int:
        raise AssertionError("RSS sampled on the native hot path")

    monkeypatch.setattr(timing, "_rss_kb", _boom)
    monkeypatch.setattr(timing, "rss_kb", _boom)
    sink: list[Any] = []
    list(_entry(_native_config(), split(_native_source(), 4), chunk_result_sink=sink))
    assert len(sink) == 3


def test_native_route_builds_no_result_objects_without_a_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution.native import _chunked_entry

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an ExecutionResult was built although no sink was given")

    monkeypatch.setattr(_chunked_entry, "ExecutionResult", _boom)
    out = list(_entry(_native_config(), split(_native_source(), 4)))
    assert sum(c.num_rows for c in out) == 11


class _UnconvertibleAdapter(PandasExecutionAdapter):
    """Returns a result whose string-rule column cannot be cast to string."""

    def run(self, *args: Any, **kwargs: Any) -> Any:
        result = super().run(*args, **kwargs)
        out = result.outputs[TABLE]
        bad = pa.array([[1]] * out.num_rows, pa.list_(pa.int64()))
        out = out.set_column(out.schema.get_field_index("s"), "s", bad)
        return dataclasses.replace(result, outputs={TABLE: out})


def _row_error_setup() -> tuple[dict[str, Any], list[pa.Table]]:
    config = make_config(
        [redact("s"), {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}}]
    )
    good = pa.table(
        {"s": pa.array(["a", "b"], pa.string()), "age": pa.array(["23", "47"], pa.string())}
    )
    bad = pa.table(
        {"s": pa.array(["c", "d"], pa.string()), "age": pa.array(["12", "oops"], pa.string())}
    )
    return config, [good, bad]


@pytest.mark.parametrize("entry_point", ["oracle", "entry"])
def test_row_error_chunk_is_appended_unnormalized_then_fails_closed(entry_point: str) -> None:
    config, chunks = _row_error_setup()
    sink: list[Any] = []
    run = _oracle if entry_point == "oracle" else _entry
    with pytest.raises(RowErrorsFailedError):
        list(run(config, chunks, chunk_result_sink=sink))
    assert len(sink) == 2  # the clean chunk, then the failing one
    failing = sink[-1]
    assert failing.row_errors
    assert b"pandas" in (failing.outputs[TABLE].schema.metadata or {})
    assert "chunked_route" not in failing.quality_metrics


def test_unconvertible_column_maps_to_chunked_schema_mismatch() -> None:
    source = _oracle_route_source(4)
    with pytest.raises(Exception) as info:
        list(_entry(_oracle_route_config(), split(source, 2), adapter=_UnconvertibleAdapter()))
    assert getattr(info.value, "code", None) == "chunked_schema_mismatch"
    message = getattr(info.value, "message", "")
    assert TABLE in message and "'s'" in message and "chunk 0" in message
    assert "string" in message and "list" in message


def test_row_error_chunk_with_an_unconvertible_column_raises_row_errors_not_mismatch() -> None:
    config = make_config(
        [redact("s"), {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}}]
    )
    bad = pa.table(
        {"s": pa.array(["c", "d"], pa.string()), "age": pa.array(["12", "oops"], pa.string())}
    )
    sink: list[Any] = []
    with pytest.raises(RowErrorsFailedError):
        list(_entry(config, [bad], adapter=_UnconvertibleAdapter(), chunk_result_sink=sink))
    assert len(sink) == 1 and sink[0].row_errors


# ---------------------------------------------------------------------------
# 7. Row offset.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 2**70, True, "5"], ids=["negative", "too_big", "bool", "str"])
@pytest.mark.parametrize("route", ["native", "oracle"])
def test_out_of_domain_base_row_offset_raises_on_both_routes(bad: Any, route: str) -> None:
    config, source = _native_config(), _native_source()
    if route == "oracle":
        config, source = _oracle_route_config(), _oracle_route_source(11)
    code = _failure(lambda: _entry(config, split(source, 4), base_row_offset=bad))
    assert code[1] == "chunked_row_offset_out_of_domain"


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_row_offset_advances_by_each_chunks_rows(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, source = _native_config(), _native_source(11)
    if route == "oracle":
        config, source = _oracle_route_config(), _oracle_route_source(11)
    advanced: list[tuple[int, int]] = []
    checked: list[tuple[int, int]] = []
    real_adv = _chunked_dgrn.advance_row_offset
    real_chk = _chunked_dgrn.validate_chunk_row_offset_range
    monkeypatch.setattr(
        _chunked_dgrn,
        "advance_row_offset",
        lambda off, chunk: (advanced.append((off, chunk.num_rows)), real_adv(off, chunk))[1],
    )
    monkeypatch.setattr(
        _chunked_dgrn,
        "validate_chunk_row_offset_range",
        lambda off, n: (checked.append((off, n)), real_chk(off, n))[1],
    )
    list(_entry(config, split(source, 4), base_row_offset=5))
    assert advanced == [(5, 4), (9, 4), (13, 3)]
    assert checked == advanced

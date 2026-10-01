"""`run_mask_chunked` against the oracle: strategy x Arrow source type x provider type.

Round-1 gate findings: the native route decided admission from the profile's
coarse dtype labels and skipped the oracle adapter's per-chunk ingest guards.
Every cell here runs the same chunks through `run_mask_chunked` and through the
oracle `run_mask_pipeline_chunked` and requires the same outcome: the same
values, or the same error code with the same number of chunks yielded, result
objects appended and vault entries written before it.

Also covers the one source-drift contract (type change after chunk 1 raises on
both routes, a `null`-typed all-null chunk is conformed), the native_threads
upper bound, exact-type adapter detection and mixed-table evidence.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._dispatch import (
    NativeChunkSchemaDriftError,
    NativeRouteEvidence,
)
from decoy_engine.vault import VaultWriter
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    categorical,
    faker_col,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    truncate,
    vault_key,
)


@dataclass
class Outcome:
    code: str | None
    chunks_yielded: int
    sink_len: int
    vault_entries: frozenset[Any]
    values: list[Any]


def _drive(
    fn: Any,
    config: dict[str, Any],
    chunks: list[pa.Table],
    evidence: list[NativeRouteEvidence] | None = None,
) -> Outcome:
    sink: list[Any] = []
    vault = VaultWriter(vault_key())
    out: list[pa.Table] = []
    code: str | None = None
    try:
        for t in fn(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            vault_writer=vault,
            chunk_result_sink=sink,
            **({} if evidence is None else {"route_evidence_sink": evidence}),
        ):
            out.append(t)
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
    values = [v for t in out for v in t.column("c").to_pylist()] if out else []
    return Outcome(code, len(out), len(sink), frozenset(vault._entries), values)


def _sources() -> dict[str, list[pa.Table]]:
    def three(make: Any) -> list[pa.Table]:
        return [pa.table({"c": make(i)}) for i in range(3)]

    def dictionary(i: int) -> pa.Array:
        vals = [f"v{i}{j}" if j != 2 else None for j in range(4)]
        return pa.array(vals, pa.string()).dictionary_encode()

    def date32(i: int) -> pa.Array:
        return pa.array(
            [dt.date(2020, 1, 1 + i * 4 + j) if j != 1 else None for j in range(4)], pa.date32()
        )

    def decimal(i: int) -> pa.Array:
        return pa.array(
            [Decimal(f"{i}{j}.25") if j != 3 else None for j in range(4)], pa.decimal128(10, 2)
        )

    def int_late_null(i: int) -> pa.Array:
        return pa.array([i * 4 + j if not (i == 1 and j == 1) else None for j in range(4)])

    def int_clean(i: int) -> pa.Array:
        return pa.array([i * 4 + j for j in range(4)], pa.int64())

    def string(i: int) -> pa.Array:
        return pa.array([f"value{i}{j}" if j != 1 else None for j in range(4)], pa.string())

    return {
        "dictionary": three(dictionary),
        "date32": three(date32),
        "decimal128": three(decimal),
        "int_null_in_chunk_2": three(int_late_null),
        "int_clean": three(int_clean),
        "string": three(string),
    }


def _faker(provider: str) -> dict[str, Any]:
    return {**faker_col("c"), "provider": provider}


_STRATEGIES: dict[str, dict[str, Any]] = {
    "hash": {**hash_col("c"), "vault": True},
    "truncate": {**truncate("c"), "namespace": "ns_c", "vault": True},
    "redact": redact("c"),
    "passthrough": passthrough("c"),
    "faker_first_name": _faker("person_first_name"),
    "faker_dob": _faker("person_dob"),
}
_SOURCES = _sources()
# Cells whose route is fixed by the admission rules, so the parity check also
# proves the native kernels (not a silent oracle fallback) produced the values.
_MUST_RUN_NATIVE = ("hash", "truncate", "redact", "faker_first_name")
_HASH_REROUTED = ("dictionary", "date32", "decimal128")


@NEEDS_COMPANION
@pytest.mark.parametrize("source_name", list(_SOURCES))
@pytest.mark.parametrize("strategy", list(_STRATEGIES))
def test_entry_matches_oracle_for_every_strategy_and_source_type(
    strategy: str, source_name: str
) -> None:
    config = make_config([_STRATEGIES[strategy]])
    chunks = _SOURCES[source_name]
    expected = _drive(run_mask_pipeline_chunked, config, chunks)
    evidence: list[NativeRouteEvidence] = []
    actual = _drive(run_mask_chunked, config, chunks, evidence)
    assert actual == expected
    assert len(evidence) == 1
    if source_name == "string" and strategy in _MUST_RUN_NATIVE:
        assert evidence[0].native_admitted is True, evidence[0].reroute_reason
    if (strategy == "faker_dob") or (strategy == "hash" and source_name in _HASH_REROUTED):
        assert evidence[0].native_admitted is False
        assert evidence[0].reroute_reason is not None


@NEEDS_COMPANION
@pytest.mark.parametrize("strategy", ["truncate", "hash"])
def test_null_bearing_int_in_a_later_chunk_fails_like_the_oracle(strategy: str) -> None:
    config = make_config([_STRATEGIES[strategy]])
    chunks = _SOURCES["int_null_in_chunk_2"]
    actual = _drive(run_mask_chunked, config, chunks)
    assert actual.code == "null_bearing_int_unsupported"
    assert actual.chunks_yielded == 1
    assert actual.sink_len == 1


@NEEDS_COMPANION
def test_non_string_faker_provider_routes_to_the_oracle_with_a_coded_reason() -> None:
    config = make_config([_STRATEGIES["faker_dob"]])
    evidence: list[NativeRouteEvidence] = []
    out = list(
        run_mask_chunked(
            config,
            list(_SOURCES["string"]),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=evidence,
        )
    )
    assert len(out) == 3
    assert evidence[0].native_admitted is False
    assert evidence[0].reroute_reason is not None
    assert evidence[0].reroute_reason.startswith("faker_provider_not_native:c:person_dob")


@NEEDS_COMPANION
@pytest.mark.parametrize("source_name", ["dictionary", "date32", "decimal128"])
def test_hash_over_a_type_the_kernel_does_not_admit_routes_to_the_oracle(source_name: str) -> None:
    config = make_config([_STRATEGIES["hash"]])
    evidence: list[NativeRouteEvidence] = []
    list(
        run_mask_chunked(
            config,
            list(_SOURCES[source_name]),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=evidence,
        )
    )
    assert evidence[0].native_admitted is False
    assert (evidence[0].reroute_reason or "").startswith("hash_input_type_not_native:c:")


# Source drift: one contract on both routes.


def _drift_config(route: str) -> dict[str, Any]:
    cols = [redact("r"), passthrough("p")]
    if route == "oracle":
        cols.append(categorical("k"))
    return make_config(cols)


def _drift_chunks(first: pa.Array, later: pa.Array, extra: pa.Array | None = None) -> list:
    def chunk(p: pa.Array) -> pa.Table:
        n = len(p)
        return pa.table(
            {
                "r": pa.array([f"r{i}" for i in range(n)], pa.string()),
                "p": p,
                "k": pa.array(["a", "b", "c", "a"][:n], pa.string()),
            }
        )

    chunks = [chunk(first), chunk(later)]
    if extra is not None:
        chunks.append(chunk(extra))
    return chunks


def _run_drift(route: str, chunks: list[pa.Table], evidence: list | None = None) -> list[pa.Table]:
    config = _drift_config(route)
    if route != "oracle":
        chunks = [c.drop_columns(["k"]) for c in chunks]
    return list(
        run_mask_chunked(
            config,
            iter(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=evidence,
        )
    )


_NAIVE = [dt.datetime(2020, 1, 1, 0, 0, i) for i in range(4)]


@pytest.mark.parametrize("route", ["native", "oracle"])
@pytest.mark.parametrize(
    ("first", "later"),
    [
        (pa.array([True, False, True, False]), pa.array(["1", "0", "1", "0"])),
        (
            pa.array(_NAIVE, pa.timestamp("us")),
            pa.array(_NAIVE, pa.timestamp("us", tz="UTC")),
        ),
        (pa.array([1, 2, 3, 4], pa.int64()), pa.array([1.0, 2.0, 3.0, 4.0], pa.float64())),
    ],
    ids=["bool_then_string", "naive_then_tz_aware", "int_then_float"],
)
def test_source_type_change_after_chunk_one_raises_on_both_routes(
    route: str, first: pa.Array, later: pa.Array
) -> None:
    evidence: list[NativeRouteEvidence] = []
    with pytest.raises(NativeChunkSchemaDriftError) as info:
        _run_drift(route, _drift_chunks(first, later), evidence)
    assert info.value.code == "native_chunk_schema_drift"
    assert evidence[0].native_admitted is (route == "native")


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_drift_raises_before_the_drifting_chunk_is_yielded(route: str) -> None:
    config = _drift_config(route)
    chunks = _drift_chunks(pa.array([True, False, True, False]), pa.array(["1", "0", "1", "0"]))
    if route != "oracle":
        chunks = [c.drop_columns(["k"]) for c in chunks]
    sink: list[ExecutionResult] = []
    got = 0
    with pytest.raises(NativeChunkSchemaDriftError):
        for _ in run_mask_chunked(
            config,
            iter(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            chunk_result_sink=sink,
        ):
            got += 1
    assert got == 1
    assert len(sink) == 1


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_all_null_null_typed_chunk_is_conformed_to_the_first_type(route: str) -> None:
    first = pa.array([1, 2, 3, 4], pa.int64())
    chunks = _drift_chunks(first, pa.nulls(4), first)
    out = _run_drift(route, chunks)
    assert [t.schema.field("p").type for t in out] == [pa.int64()] * 3
    assert out[1].column("p").to_pylist() == [None] * 4
    assert out[2].column("p").to_pylist() == [1, 2, 3, 4]


# native_threads bound (the compiled kernels refuse above 1024).


def _threads_call(native_threads: Any) -> Any:
    return run_mask_chunked(
        make_config([redact("c")]),
        [pa.table({"c": pa.array(["a", "b"], pa.string())})],
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        native_threads=native_threads,
    )


@pytest.mark.parametrize("bad", [1025, 2**70, 0, -1])
def test_native_threads_outside_one_to_1024_raises_eagerly(bad: int) -> None:
    with pytest.raises(ExecutionError) as info:
        _threads_call(bad)  # not iterated: the check is eager
    assert info.value.code == "invalid_native_threads"


@pytest.mark.parametrize("ok", [1, 1024])
def test_native_threads_bounds_are_inclusive(ok: int) -> None:
    out = list(_threads_call(ok))
    assert out[0].column("c").to_pylist() == ["REDACTED", "REDACTED"]


# Exact-type adapter detection.


def test_a_pandas_adapter_subclass_is_not_bypassed() -> None:
    calls: list[int] = []

    class Counting(PandasExecutionAdapter):
        def run(self, *args: Any, **kwargs: Any) -> ExecutionResult:
            calls.append(1)
            return super().run(*args, **kwargs)

    evidence: list[NativeRouteEvidence] = []
    list(
        run_mask_chunked(
            make_config([redact("c")]),
            [pa.table({"c": pa.array(["a", "b"], pa.string())})],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            adapter=Counting(),
            route_evidence_sink=evidence,
        )
    )
    assert calls == [1]
    assert evidence[0].reroute_reason == "adapter_requested"


# Evidence aggregation must not mix tables.


def _result_for(table: str) -> ExecutionResult:
    return ExecutionResult(
        outputs={},
        timings=(),
        boundary_conversion_ms=0.0,
        warnings=(),
        quality_metrics={
            "chunked_route": {
                "table": table,
                "native_admitted": True,
                "reroute_reason": None,
                "columns": [],
            }
        },
        row_errors=(),
    )


def test_aggregate_rejects_results_from_different_tables() -> None:
    with pytest.raises(ExecutionError) as info:
        aggregate_chunked_route_evidence([_result_for("a"), _result_for("b")])
    assert info.value.code == "chunked_route_evidence_mixed_tables"


# Leading null-typed chunk (rev6 guarantee 1, acceptance test 13).


def _leading_null_chunks() -> list[pa.Table]:
    return [
        pa.table({"c": pa.nulls(4)}),
        pa.table({"c": pa.array(["a1", "b2", None, "d4"], pa.string())}),
    ]


def _drive_with_state(config: dict[str, Any], chunks: list[pa.Table]) -> tuple[Outcome, Any]:
    sink: list[Any] = []
    vault = VaultWriter(vault_key())
    out: list[pa.Table] = []
    caught: Exception | None = None
    try:
        for t in run_mask_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            vault_writer=vault,
            chunk_result_sink=sink,
        ):
            out.append(t)
    except Exception as exc:
        caught = exc
    code = None if caught is None else getattr(caught, "code", type(caught).__name__)
    values = [v for t in out for v in t.column("c").to_pylist()]
    return Outcome(code, len(out), len(sink), frozenset(vault._entries), values), caught


@NEEDS_COMPANION
@pytest.mark.parametrize("strategy", ["passthrough", "hash"])
def test_leading_null_typed_chunk_then_typed_chunk_is_a_coded_refusal(strategy: str) -> None:
    config = make_config([_STRATEGIES[strategy]])
    chunks = _leading_null_chunks()
    outcome, err = _drive_with_state(config, chunks)
    assert outcome.code == "chunked_leading_null_type"
    assert isinstance(err, ExecutionError)
    assert TABLE in err.message and "'c'" in err.message and "string" in err.message
    assert "fixed schema" in err.message
    # Refused before the second chunk reaches the sink, the vault or the caller.
    first_only, _ = _drive_with_state(config, chunks[:1])
    assert outcome.chunks_yielded == 1
    assert outcome.sink_len == 1
    assert outcome.vault_entries == first_only.vault_entries


@NEEDS_COMPANION
@pytest.mark.parametrize("strategy", ["passthrough", "hash"])
def test_column_null_typed_in_every_chunk_is_yielded_as_null(strategy: str) -> None:
    config = make_config([_STRATEGIES[strategy]])
    chunks = [pa.table({"c": pa.nulls(4)}), pa.table({"c": pa.nulls(3)})]
    out = list(
        run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    assert [t.column("c").to_pylist() for t in out] == [[None] * 4, [None] * 3]
    if strategy == "passthrough":
        assert all(pa.types.is_null(t.schema.field("c").type) for t in out)


@NEEDS_COMPANION
@pytest.mark.parametrize("strategy", ["truncate", "hash"])
def test_int_column_with_a_later_null_typed_chunk_is_masked_like_the_oracle(
    strategy: str,
) -> None:
    config = make_config([_STRATEGIES[strategy]])
    chunks = [
        pa.table({"c": pa.array([10, 20, 30, 40], pa.int64())}),
        pa.table({"c": pa.nulls(4)}),
        pa.table({"c": pa.array([50, 60, 70, 80], pa.int64())}),
    ]
    expected = _drive(run_mask_pipeline_chunked, config, chunks)
    actual = _drive(run_mask_chunked, config, chunks)
    assert expected.code is None
    assert actual == expected


@NEEDS_COMPANION
def test_run_native_or_oracle_chunked_refuses_a_leading_null_typed_chunk_too() -> None:
    from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked

    config = make_config([_STRATEGIES["passthrough"]])
    got = 0
    with pytest.raises(ExecutionError) as info:
        for _ in run_native_or_oracle_chunked(
            config,
            _leading_null_chunks(),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        ):
            got += 1
    assert info.value.code == "chunked_leading_null_type"
    assert got == 1

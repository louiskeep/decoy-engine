"""B8 acceptance tests 1, 2, 5, 7, 8 and 9: native admission of unconfigured passthrough columns.

Under the resolved `warn` policy a table whose only obstacle to the native route was
unconfigured source columns runs natively, with output, warnings and side channels equal
to the oracle route's. Under `error` the routing is unchanged. Tests that compare outputs
also assert the route each side took (`native_admitted`, `reroute_reason`), so a case
cannot pass with both sides on the oracle. Written before the implementation; do not
delete one, add a skip or xfail outside `NEEDS_COMPANION`, or loosen a comparison without
a new plan gate.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution.native import _chunked_entry
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._dispatch import (
    NativeChunkSchemaDriftError,
    plan_native_route,
    run_native_or_oracle_chunked,
)
from decoy_engine.execution.native._real_type_admission import real_type_rejection
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.vault import VaultWriter
from tests.native._b8_support import (
    FORCE,
    assert_same_as_oracle,
    identical,
    policy_settings,
    run_one,
    run_pair,
    strings,
    with_force,
)
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
from tests.native._rev9_support import companion_missing
from tests.native._rev9_type_catalogue import CATALOGUE

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _chunk(n: int, i: int = 0, **cols: pa.Array) -> pa.Table:
    """Strings for r, t, h, f, u (and p ints) of `n` rows; `cols` replace or add columns."""
    base: dict[str, pa.Array] = {
        "r": strings(n, "r", i),
        "t": strings(n, "abcdef", i),
        "h": strings(n, "h", i),
        "f": strings(n, "f", i),
        "p": pa.array(range(n), pa.int64()),
        "u": strings(n, "u", i),
    }
    base.update(cols)
    return pa.table(base)


def _stream(sizes: tuple[int, ...], names: list[str], **by_name: Any) -> list[pa.Table]:
    return [_chunk(n, i).select(names) for i, n in enumerate(sizes)]


_STRATEGY_COLUMNS = {
    "redact": redact("r"),
    "truncate": truncate("t"),
    "hash": hash_col("h"),
    "passthrough": passthrough("p"),
    "faker": faker_col("f"),
}
_STRATEGY_SOURCE = {"redact": "r", "truncate": "t", "hash": "h", "passthrough": "p", "faker": "f"}
_NEEDS = {"hash", "faker"}


def _params(names: set[str]) -> list[Any]:
    return [
        pytest.param(n, marks=[NEEDS_COMPANION] if n in names else []) for n in _STRATEGY_COLUMNS
    ]


def _admissible_columns() -> list[dict[str, Any]]:
    return [redact("r"), truncate("t"), hash_col("h"), passthrough("p"), faker_col("f")]


# ---------------------------------------------------------------------------
# Test 1: routing by policy
# ---------------------------------------------------------------------------


def _all_chunks() -> list[pa.Table]:
    return [_chunk(4, 0), _chunk(4, 1), _chunk(3, 2)]


@NEEDS_COMPANION
def test_warn_policy_routes_native_with_no_reroute_reason() -> None:
    run = run_one(
        make_config(_admissible_columns(), global_settings=policy_settings("warn")), _all_chunks()
    )
    assert run.ev[0].native_admitted is True
    assert run.ev[0].reroute_reason is None
    assert all(r.column_names[-1] == "u" for r in run.out)


@NEEDS_COMPANION
def test_error_policy_keeps_the_oracle_route_and_raises_as_today() -> None:
    config = make_config(_admissible_columns(), global_settings=policy_settings("error"))
    chunks = _all_chunks()
    with pytest.raises(ExecutionError) as public:
        list(
            run_mask_pipeline_chunked(
                config,
                list(chunks),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    sink: list[Any] = []
    ev: list[Any] = []
    vault = VaultWriter(vault_key())
    gen = run_mask_chunked(
        config,
        list(chunks),
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
        route_evidence_sink=ev,
        vault_writer=vault,
    )
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason == "uncovered_columns:['u'];missing_configured_columns:[]"
    with pytest.raises(ExecutionError) as entry:
        next(gen)
    assert entry.value.code == public.value.code == "undeclared_output_columns"
    assert str(entry.value) == str(public.value)
    assert sink == [] and vault._entries == set()


@pytest.mark.parametrize("pre_ga", [True, False])
def test_unset_policy_follows_the_release_phase(
    pre_ga: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("decoy_engine.execution._output_projection.is_pre_ga", lambda: pre_ga)
    config = make_config([redact("r"), truncate("t")])
    chunks = [_chunk(4, 0).select(["r", "t", "u"]), _chunk(3, 1).select(["r", "t", "u"])]
    if pre_ga:
        run = run_one(config, chunks)
        assert run.ev[0].native_admitted is True and run.ev[0].reroute_reason is None
        return
    ev: list[Any] = []
    gen = run_mask_chunked(
        config,
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        route_evidence_sink=ev,
    )
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason == "uncovered_columns:['u'];missing_configured_columns:[]"
    with pytest.raises(ExecutionError) as info:
        next(gen)
    assert info.value.code == "undeclared_output_columns"


@pytest.mark.parametrize("policy", ["warn", "error"])
@pytest.mark.parametrize("unconfigured", [False, True], ids=["no_extra", "with_extra"])
def test_configured_column_missing_from_the_source_keeps_todays_reason(
    policy: str, unconfigured: bool
) -> None:
    config = make_config(
        [redact("r"), truncate("t"), passthrough("q")], global_settings=policy_settings(policy)
    )
    names = ["r", "t"] + (["u"] if unconfigured else [])
    chunks = [_chunk(4, 0).select(names), _chunk(3, 1).select(names)]
    ev: list[Any] = []
    try:
        list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                route_evidence_sink=ev,
            )
        )
    except ExecutionError:
        pass  # under `error` the oracle refuses after the route is decided
    extra = "['u']" if unconfigured else "[]"
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason == f"uncovered_columns:{extra};missing_configured_columns:['q']"


def _schema_and_profile(config: dict[str, Any], chunk: pa.Table) -> tuple[Any, pa.Schema]:
    return first_chunk_profile(chunk, table=TABLE, engine_version=ENGINE_VERSION), chunk.schema


def test_plan_native_route_takes_the_policy_keyword_and_defaults_to_the_veto() -> None:
    config = make_config([redact("r"), truncate("t")])
    chunk = _chunk(4, 0).select(["r", "t", "u"])
    profile, schema = _schema_and_profile(config, chunk)

    def plan(**kw: Any) -> Any:
        return plan_native_route(
            config, profile, table=TABLE, engine_version=ENGINE_VERSION, first_schema=schema, **kw
        )

    assert plan().evidence.native_admitted is False
    assert plan(unconfigured_policy=None).evidence.native_admitted is False
    assert plan(unconfigured_policy="error").evidence.native_admitted is False
    admitted = plan(unconfigured_policy="warn")
    assert admitted.evidence.native_admitted is True
    assert admitted.unconfigured_passthrough == ("u",)
    assert plan().evidence.reroute_reason == "uncovered_columns:['u'];missing_configured_columns:[]"


def test_plan_native_route_warn_still_vetoes_a_missing_configured_column() -> None:
    config = make_config([redact("r"), passthrough("q")])
    chunk = _chunk(4, 0).select(["r", "u"])
    profile, schema = _schema_and_profile(config, chunk)
    got = plan_native_route(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        first_schema=schema,
        unconfigured_policy="warn",
    )
    assert got.evidence.native_admitted is False
    assert got.evidence.reroute_reason == "uncovered_columns:['u'];missing_configured_columns:['q']"


def test_legacy_entry_keeps_the_veto() -> None:
    config = make_config([redact("r")])
    ev: list[Any] = []
    list(
        run_native_or_oracle_chunked(
            config,
            [_chunk(4, 0).select(["r", "u"])],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=ev,
        )
    )
    assert ev[0].native_admitted is False
    assert "uncovered_columns" in ev[0].reroute_reason


# ---------------------------------------------------------------------------
# Test 2: output identity with the oracle route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", _params(_NEEDS))
def test_each_admitted_strategy_beside_an_unconfigured_column(strategy: str) -> None:
    source = _STRATEGY_SOURCE[strategy]
    chunks = _stream((4, 4, 3), [source, "u"])
    native, forced = run_pair([_STRATEGY_COLUMNS[strategy]], chunks, vault=False)
    assert_same_as_oracle(native, forced)
    assert all(len(r.warnings) == 1 for r in native.sink)


@pytest.mark.parametrize("order", ["u_first", "u_between", "u_last", "u_everywhere"])
def test_unconfigured_columns_first_between_and_after(order: str) -> None:
    layouts = {
        "u_first": ["u", "r", "t"],
        "u_between": ["r", "u", "t"],
        "u_last": ["r", "t", "u"],
        "u_everywhere": ["u", "r", "p", "t", "f"],
    }
    names = layouts[order]
    chunks = _stream((4, 4, 3), names)
    native, forced = run_pair([redact("r"), truncate("t")], chunks)
    assert_same_as_oracle(native, forced)
    for out in native.out:
        assert out.column_names == names


def test_table_whose_only_configured_column_is_passthrough() -> None:
    chunks = _stream((4, 4, 3), ["p", "u"])
    native, forced = run_pair([passthrough("p")], chunks)
    assert_same_as_oracle(native, forced)


@pytest.mark.parametrize("sizes", [(4, 4, 3), (5, 5, 1), (2, 2, 2, 1)])
def test_uneven_last_chunk(sizes: tuple[int, ...]) -> None:
    native, forced = run_pair([redact("r"), truncate("t")], _stream(sizes, ["r", "t", "u"]))
    assert_same_as_oracle(native, forced)
    assert [o.num_rows for o in native.out] == list(sizes)


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_zero_row_chunk(position: str) -> None:
    sizes = {"first": (0, 4, 3), "middle": (4, 0, 3), "last": (4, 3, 0)}[position]
    native, forced = run_pair([redact("r"), truncate("t")], _stream(sizes, ["r", "t", "u"]))
    assert_same_as_oracle(native, forced)
    assert [o.num_rows for o in native.out] == list(sizes)


def test_unconfigured_column_all_null_in_chunk_zero_but_string_typed() -> None:
    chunks = [
        _chunk(4, 0, u=pa.array([None] * 4, pa.string())).select(["r", "t", "u"]),
        _chunk(4, 1).select(["r", "t", "u"]),
    ]
    native, forced = run_pair([redact("r"), truncate("t")], chunks)
    assert_same_as_oracle(native, forced)
    assert {o.schema.field("u").type for o in native.out} == {pa.string()}


def test_later_all_null_null_typed_chunk_of_a_typed_unconfigured_column() -> None:
    chunks = [
        _chunk(4, 0).select(["r", "t", "u"]),
        _chunk(4, 1, u=pa.nulls(4)).select(["r", "t", "u"]),
        _chunk(3, 2).select(["r", "t", "u"]),
    ]
    native, forced = run_pair([redact("r"), truncate("t")], chunks)
    assert_same_as_oracle(native, forced)
    assert {o.schema.field("u").type for o in native.out} == {pa.string()}


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_native_threads(threads: int) -> None:
    chunks = _stream((4, 4, 3), ["r", "h", "f", "u"])
    native, forced = run_pair(
        [redact("r"), hash_col("h"), faker_col("f")], chunks, native_threads=threads
    )
    assert_same_as_oracle(native, forced)


def test_companion_absent_redact_truncate_passthrough_still_admit_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = _stream((4, 4, 3), ["r", "t", "p", "u"])
    with companion_missing(monkeypatch):
        native, forced = run_pair([redact("r"), truncate("t"), passthrough("p")], chunks)
    assert_same_as_oracle(native, forced)


def test_vault_entries_match_the_oracle_route() -> None:
    columns = [{**truncate("t"), "namespace": "ns_t", "vault": True}, redact("r")]
    chunks = _stream((4, 4, 3), ["r", "t", "u"])
    native, forced = run_pair(columns, chunks, vault=True)
    assert_same_as_oracle(native, forced)
    assert native.vault, "the vault must have collected something"


def test_warnings_are_one_undeclared_output_columns_per_chunk_naming_the_columns() -> None:
    chunks = [
        _chunk(4, 0).select(["r", "p", "u", "t"]),
        _chunk(3, 1).select(["r", "p", "u", "t"]),
    ]
    native, forced = run_pair([redact("r")], chunks)
    assert_same_as_oracle(native, forced)
    for result in native.sink:
        (warning,) = result.warnings
        assert isinstance(warning, QualityWarning)
        assert warning.code == "undeclared_output_columns"
        assert warning.provider == "output_projection" and warning.column is None
        assert warning.detail == {"table": TABLE, "undeclared_columns": ["p", "t", "u"]}


def test_native_route_computes_the_warning_with_no_sink_given() -> None:
    called: list[tuple[Any, ...]] = []
    real = _chunked_entry.enforce_output_projection

    def spy(*args: Any, **kw: Any) -> Any:
        called.append(args)
        return real(*args, **kw)

    config = make_config([redact("r")], global_settings=policy_settings("warn"))
    chunks = [_chunk(4, 0).select(["r", "u"]), _chunk(3, 1).select(["r", "u"])]
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(_chunked_entry, "enforce_output_projection", spy)
        out = list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    finally:
        mp.undo()
    assert len(out) == 2 and len(called) == 2


# ---------------------------------------------------------------------------
# Test 5: configured union and run-end-encoded passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["dense_union", "sparse_union", "ree_string_view"])
def test_configured_union_and_ree_passthrough_runs_natively(name: str) -> None:
    if name not in CATALOGUE:
        pytest.skip(f"this pyarrow has no {name}")
    arr = CATALOGUE[name](False)
    chunks = [pa.table({"s": pa.array(list("abcde")), "x": arr}) for _ in range(2)]
    native, forced = run_pair([redact("s"), passthrough("x")], chunks)
    assert_same_as_oracle(native, forced)
    for out, src in zip(native.out, chunks, strict=True):
        assert out.column("x").equals(src.column("x"))


def _hash_source_types() -> list[Any]:
    from tests.native.test_chunked_entry_parity_matrix import _sources

    return [pytest.param(chunks[0].schema, id=name) for name, chunks in _sources().items()]


@pytest.mark.parametrize("schema", _hash_source_types())
def test_real_type_rejection_reasons_are_unchanged_for_every_hash_source_type(
    schema: pa.Schema,
) -> None:
    """The resident table is built from the hash fields only; each reason must equal the one
    the whole-schema resident table gave."""
    from decoy_engine.execution.native._dispatch import _static_route_decision
    from decoy_engine.execution.native._requirements import hash_config_rejection

    config = make_config([hash_col("c")])
    chunk = pa.Table.from_batches([], schema=schema)
    profile = first_chunk_profile(chunk, table=TABLE, engine_version=ENGINE_VERSION)
    decision = _static_route_decision(config, profile, table=TABLE, engine_version=ENGINE_VERSION)
    before = hash_config_rejection(
        "c", TABLE, profile, resident_sources={TABLE: schema.empty_table()}
    )
    after = real_type_rejection(config, decision.node_routes, schema, table=TABLE, profile=profile)
    assert after == before


# ---------------------------------------------------------------------------
# Test 7: cross-check against the oracle's own definition
# ---------------------------------------------------------------------------


def test_cross_check_downgrades_when_the_two_unconfigured_sets_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config([redact("r"), truncate("t")], global_settings=policy_settings("warn"))
    chunks = [_chunk(4, 0).select(["r", "t", "u"]), _chunk(3, 1).select(["r", "t", "u"])]
    monkeypatch.setattr(
        _chunked_entry, "known_output_columns", lambda plan, table: frozenset({"r"})
    )
    run = run_one(config, chunks)
    assert run.ev[0].native_admitted is False
    assert run.ev[0].reroute_reason == "unconfigured_set_mismatch:['t', 'u']:['u']"
    public = list(
        run_mask_pipeline_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    assert [o.to_pydict() for o in run.out] == [o.to_pydict() for o in public]


# ---------------------------------------------------------------------------
# Test 8: leading null, drift and guards on native-admitted tables
# ---------------------------------------------------------------------------


def _outcome(config: dict[str, Any], chunks: list[pa.Table]) -> tuple[str | None, int, int, bool]:
    sink: list[Any] = []
    ev: list[Any] = []
    yielded = 0
    code: str | None = None
    try:
        for _ in run_mask_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            chunk_result_sink=sink,
            route_evidence_sink=ev,
        ):
            yielded += 1
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
    return code, yielded, len(sink), ev[0].native_admitted if ev else False


def _both_outcomes(
    columns: list[dict[str, Any]], chunks: list[pa.Table]
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    gs = policy_settings("warn")
    native = _outcome(make_config(columns, global_settings=gs), chunks)
    forced = _outcome(
        make_config([*columns, categorical(FORCE)], global_settings=gs),
        [with_force(c) for c in chunks],
    )
    assert native[3] is True and forced[3] is False
    return native[:3], forced[:3]


def test_leading_null_type_of_an_unconfigured_column_raises_on_both_routes() -> None:
    chunks = [
        _chunk(4, 0, u=pa.nulls(4)).select(["r", "t", "u"]),
        _chunk(3, 1).select(["r", "t", "u"]),
    ]
    native, forced = _both_outcomes([redact("r"), truncate("t")], chunks)
    assert native == forced == ("chunked_leading_null_type", 1, 1)


@pytest.mark.parametrize("kind", ["added", "dropped", "retyped"])
def test_drift_in_an_unconfigured_column_raises_on_both_routes(kind: str) -> None:
    names = ["r", "t", "u"]
    base = [_chunk(4, 0).select(names), _chunk(4, 1).select(names)]
    if kind == "added":
        last = _chunk(3, 2).select(names).append_column("extra", pa.array([1, 2, 3]))
    elif kind == "dropped":
        last = _chunk(3, 2).select(["r", "t"])
    else:
        last = _chunk(3, 2, u=pa.array([1, 2, 3], pa.int64())).select(names)
    native, forced = _both_outcomes([redact("r"), truncate("t")], [*base, last])
    assert native == forced == ("native_chunk_schema_drift", 2, 2)
    assert issubclass(NativeChunkSchemaDriftError, ExecutionError)


def test_null_bearing_int_is_carried_when_unconfigured_and_refused_when_truncated() -> None:
    ints = [pa.array([1, None, 3, 4], pa.int64()), pa.array([5, 6, None, 8], pa.int64())]
    chunks = [_chunk(4, i, ncol=ints[i]).select(["r", "ncol"]) for i in range(2)]
    # unconfigured: carried, no refusal, on both routes
    native, forced = run_pair([redact("r")], chunks)
    assert_same_as_oracle(native, forced)
    for out, src in zip(native.out, chunks, strict=True):
        assert out.column("ncol").equals(src.column("ncol"))
    # configured truncate: refused as B1 pins, on both routes
    config = make_config([redact("r"), truncate("ncol")], global_settings=policy_settings("warn"))
    with pytest.raises(Exception) as native_exc:
        run_one(config, chunks)
    with pytest.raises(Exception) as oracle_exc:
        run_one(
            make_config(
                [redact("r"), truncate("ncol"), categorical(FORCE)],
                global_settings=policy_settings("warn"),
            ),
            [with_force(c) for c in chunks],
        )
    assert type(native_exc.value) is type(oracle_exc.value)
    assert getattr(native_exc.value, "code", None) == getattr(oracle_exc.value, "code", None)


@pytest.mark.parametrize("name", ["list_view", "ree_string"])
def test_later_all_null_chunk_of_a_type_with_no_cast_from_null(name: str) -> None:
    if name not in CATALOGUE:
        pytest.skip(f"this pyarrow has no {name}")
    arr = CATALOGUE[name](False)
    chunks = [
        pa.table({"s": pa.array(list("abcde")), "x": arr}),
        pa.table({"s": pa.array(list("abcde")), "x": pa.nulls(5)}),
    ]
    gs = policy_settings("warn")
    results: list[tuple[str, int]] = []
    for config, src in (
        (make_config([redact("s")], global_settings=gs), chunks),
        (
            make_config([redact("s"), categorical(FORCE)], global_settings=gs),
            [with_force(c) for c in chunks],
        ),
    ):
        sink: list[Any] = []
        with pytest.raises(pa.ArrowNotImplementedError) as info:
            list(
                run_mask_chunked(
                    config,
                    src,
                    table=TABLE,
                    engine_version=ENGINE_VERSION,
                    key_provider=key_provider(),
                    chunk_result_sink=sink,
                )
            )
        results.append((str(info.value), len(sink)))
    assert results[0] == results[1]
    assert results[0][1] == 1


# ---------------------------------------------------------------------------
# Test 9: evidence
# ---------------------------------------------------------------------------


def test_admitted_table_evidence() -> None:
    chunks = _stream((4, 4, 3), ["r", "t", "p", "u"])
    run = run_one(
        make_config(
            [redact("r"), truncate("t"), passthrough("p")], global_settings=policy_settings("warn")
        ),
        chunks,
    )
    assert run.ev[0].native_admitted is True and run.ev[0].reroute_reason is None
    for result in run.sink:
        payload = result.quality_metrics["chunked_route"]
        assert json.loads(json.dumps(payload, allow_nan=False)) == payload
        assert payload["native_admitted"] is True and payload["reroute_reason"] is None
        assert payload["pandas_read_passthrough"] == []
        assert {c["column"] for c in payload["columns"]} == {"r", "t", "p"}
        assert all(c["calls"] == 1 for c in payload["columns"])
    agg = aggregate_chunked_route_evidence(run.sink)
    assert json.loads(json.dumps(agg, allow_nan=False)) == agg
    assert agg["native_admitted"] is True and agg["reroute_reason"] is None
    assert agg["pandas_read_passthrough"] == []
    assert {c["column"]: c["calls"] for c in agg["columns"]} == {"r": 3, "t": 3, "p": 3}


def test_unconfigured_admission_does_not_change_the_yielded_columns() -> None:
    chunks = _stream((4, 4, 3), ["r", "u", "p"])
    run = run_one(make_config([redact("r")], global_settings=policy_settings("warn")), chunks)
    for out, src in zip(run.out, chunks, strict=True):
        assert identical(out.select(["u", "p"]), src.select(["u", "p"]))
        assert out.column_names == ["r", "u", "p"]


def test_native_route_refuses_through_the_projection_call_if_admitted_under_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission keeps `error` tables on the oracle route. If a later change ever admitted
    one, the per-chunk `enforce_output_projection` call is the backstop and must raise."""
    real = _chunked_entry.plan_native_route

    def admit_anyway(*args: Any, **kwargs: Any) -> Any:
        kwargs["unconfigured_policy"] = "warn"
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunked_entry, "plan_native_route", admit_anyway)
    config = make_config([redact("r")], global_settings=policy_settings("error"))
    chunks = [_chunk(4, 0).select(["r", "u"]), _chunk(3, 1).select(["r", "u"])]
    sink: list[Any] = []
    ev: list[Any] = []
    gen = run_mask_chunked(
        config,
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
        route_evidence_sink=ev,
    )
    assert ev[0].native_admitted is True
    with pytest.raises(ExecutionError) as info:
        next(gen)
    assert info.value.code == "undeclared_output_columns"
    assert sink == []

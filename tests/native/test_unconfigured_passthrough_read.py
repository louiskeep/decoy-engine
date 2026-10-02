"""B8 acceptance test 6: the read set holds actual references only.

(a) A passthrough column whose name equals a strategy, a provider or a namespace is carried
and the table stays native. (b) Genuine readers keep their route, reason and coded errors.
(c) Columns a composite writes are never carried, end to end through the carry.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution._chunked_carry import plan_carry, read_set
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.plan import compile_plan
from tests.native._b8_support import assert_same_as_oracle, run_pair
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    faker_col,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
)
from tests.native._rev9_support import (
    BY_NAME,
    run_public,
    same_column,
)

CODE = "chunked_passthrough_value_unrepresentable"
_T64 = BY_NAME["time64ns_unaligned"]
_S = pa.array(["a", "b", "c"])
_WARN = {"unconfigured_column_policy": "warn"}


def _chunks(name: str, *, extra: dict[str, pa.Array] | None = None) -> list[pa.Table]:
    base = {
        "s": _S,
        "f": pa.array(["f1", "f2", "f3"]),
        "h": pa.array(["h1", "h2", "h3"]),
        **(extra or {}),
    }
    return [
        pa.table({**base, name: _T64.good}),
        pa.table({**base, name: _T64.bad}),
        pa.table({**base, name: _T64.good}),
    ]


# ---------------------------------------------------------------------------
# (a) collisions with a strategy, provider or namespace stay native and exact
# ---------------------------------------------------------------------------

_COLLISIONS = {
    "strategy_name": ("redact", [redact("s")]),
    "provider_name": ("person_first_name", [redact("s"), faker_col("f")]),
    "namespace_value": ("ns_h", [redact("s"), hash_col("h")]),
}


@NEEDS_COMPANION
@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
@pytest.mark.parametrize("case", sorted(_COLLISIONS))
def test_name_collision_stays_native_and_exact(case: str, configured: bool) -> None:
    name, columns = _COLLISIONS[case]
    columns = [*columns, *([passthrough(name)] if configured else [])]
    names = (
        ["s"]
        + (["f"] if case == "provider_name" else [])
        + (["h"] if case == "namespace_value" else [])
    )
    chunks = [t.select([*names, name]) for t in _chunks(name)]
    native, forced = run_pair(columns, chunks)
    assert_same_as_oracle(native, forced)
    for out, src in zip(native.out, chunks, strict=True):
        assert same_column(out.column(name), src.column(name))
    assert [r.quality_metrics["chunked_route"]["pandas_read_passthrough"] for r in native.sink] == [
        []
    ] * 3


# ---------------------------------------------------------------------------
# (b) genuine readers are unchanged
# ---------------------------------------------------------------------------


def _when(name: str, expr: str) -> dict[str, Any]:
    return {**redact(name), "when": expr}


def _reader_cases() -> dict[str, tuple[list[dict[str, Any]], str, str]]:
    """name -> (columns, the passthrough column read, expected reroute reason prefix)."""
    return {
        "when_bare": ([_when("s", "x.notnull()")], "x", "when_predicate_not_native:s"),
        "when_backtick": ([_when("s", "`x`.notnull()")], "x", "when_predicate_not_native:s"),
        "when_nfkc": ([_when("s", "\uff58.notnull()")], "x", "when_predicate_not_native:s"),
    }


def _read_chunks(name: str) -> list[pa.Table]:
    return [
        pa.table({"s": _S, name: _T64.good}),
        pa.table({"s": _S, name: _T64.bad}),
        pa.table({"s": _S, name: _T64.good}),
    ]


@pytest.mark.parametrize("forced", [False, True], ids=["plain", "companion_missing"])
@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
@pytest.mark.parametrize("case", sorted(_reader_cases()))
def test_genuine_reader_keeps_its_route_listing_and_coded_error(
    case: str, configured: bool, forced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns, name, reason = _reader_cases()[case]
    columns = [*columns, *([passthrough(name)] if configured else [])]
    chunks = _read_chunks(name)
    sink: list[Any] = []
    ev: list[Any] = []
    config = make_config(columns, global_settings=_WARN)
    src = chunks
    if forced:
        config = make_config([*columns, hash_col("h")], global_settings=_WARN)
        src = [t.append_column("h", pa.array(["p", "q", "r"])) for t in chunks]
        monkeypatch.setitem(__import__("sys").modules, "decoy_engine_native", None)
    gen = run_mask_chunked(
        config,
        src,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
        route_evidence_sink=ev,
    )
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason is not None
    assert ev[0].reroute_reason.startswith(reason) or forced
    assert not (ev[0].reroute_reason or "").startswith("pandas_read_passthrough:")
    first = next(gen)
    assert first.num_rows == 3
    with pytest.raises(ExecutionError) as info:
        next(gen)
    assert info.value.code == CODE
    assert "chunk 1" in info.value.message and repr(name) in info.value.message
    assert len(sink) == 1
    assert sink[0].quality_metrics["chunked_route"]["pandas_read_passthrough"] == [name]


_DICT = BY_NAME["dict_null_referenced"]


class _DateShape:
    good = pa.array([18262, 18263, 18264], pa.date32())
    bad = pa.array([2**31 - 1, 18263, 18264], pa.date32())


_DATE = _DateShape()


def _sibling_case(kind: str) -> tuple[list[dict[str, Any]], str, Any, list[pa.Table]]:
    """(columns, the read passthrough column, its shape, one valid source chunk)."""
    dates = pa.array(["1960-03-01", "1961-03-02", "1962-03-03"])
    if kind == "group_key":
        cols = [{"name": "k", "strategy": "group_key", "provider_config": {"group_by": "g"}}]
        return cols, "g", _DICT, [pa.table({"k": pa.array(["a", "b", "c"]), "g": _DICT.good})]
    if kind == "date_shift":
        cols = [
            {
                "name": "dob",
                "strategy": "date_shift",
                "namespace": "dob_ns",
                "provider_config": {"min_days": -30, "max_days": 30, "group_by": "g"},
            }
        ]
        return cols, "g", _DICT, [pa.table({"dob": dates, "g": _DICT.good})]
    cols = [
        {
            "name": "end",
            "strategy": "windowed_date",
            "provider_config": {
                "anchor": "start",
                "min_days": 0,
                "max_days": 30,
                "distribution": "uniform",
            },
        }
    ]
    return cols, "start", _DATE, [pa.table({"end": dates, "start": _DATE.good})]


@pytest.mark.parametrize("forced", [False, True], ids=["plain", "companion_missing"])
@pytest.mark.parametrize("kind", ["group_key", "date_shift", "windowed_date"])
def test_sibling_reading_strategy_keeps_its_listing_and_coded_error(
    kind: str, forced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sibling reference must name a configured column (the plan compiler checks it), so
    # only the configured passthrough form exists here.
    columns, ref, shape, good = _sibling_case(kind)
    columns = [passthrough(ref), *columns]
    config = make_config(columns, global_settings=_WARN)
    index = good[0].schema.get_field_index(ref)
    chunks = [good[0], good[0].set_column(index, ref, shape.bad)]
    expected = run_public(config, [good[0], good[0]])
    if forced:
        config = make_config([*columns, hash_col("h")], global_settings=_WARN)
        chunks = [t.append_column("h", pa.array(["p", "q", "r"])) for t in chunks]
        monkeypatch.setitem(__import__("sys").modules, "decoy_engine_native", None)
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
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason is not None
    assert not ev[0].reroute_reason.startswith("pandas_read_passthrough:")
    first = next(gen)
    assert first.drop_columns(["h"] if forced else []).to_pydict() == expected[0].to_pydict()
    with pytest.raises(ExecutionError) as info:
        next(gen)
    assert info.value.code == CODE
    assert repr(ref) in info.value.message and "chunk 1" in info.value.message
    assert len(sink) == 1
    assert sink[0].quality_metrics["chunked_route"]["pandas_read_passthrough"] == [ref]


# ---------------------------------------------------------------------------
# (c) composite outputs are never carried
# ---------------------------------------------------------------------------

_BUNDLE = [
    {"column": "a", "provider": "person_first_name"},
    {"column": "b", "provider": "person_last_name"},
    {"column": "c", "provider": "person_phone"},
]


def _composite(
    name: str, provider: str, coherent: tuple[str, ...], strategy: str, **extra: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "strategy": strategy,
        "provider": provider,
        "deterministic": True,
        "namespace": "ns",
        "coherent_with": list(coherent),
    }
    if strategy == "faker":
        out["pool_size"] = 64
    if extra:
        out["provider_config"] = extra
    return out


def _custom_columns(strategy: str, configured_c: bool) -> list[dict[str, Any]]:
    cfg = {"bundle": _BUNDLE}
    return [
        _composite("a", "composite_custom", ("b",), strategy, **cfg),
        _composite("b", "composite_custom", ("a",), strategy, **cfg),
        *([passthrough("c")] if configured_c else []),
    ]


def _lone(provider: str, own: str, strategy: str, when: str | None) -> list[dict[str, Any]]:
    col = _composite(own, provider, (), strategy)
    if when:
        col["when"] = when
    return [col]


_FIXED_LONE = {
    "composite_name_email": ("first_name", ["last_name", "email"]),
    "composite_city_state_zip": ("city", ["state", "zip"]),
}

_CASES = {
    "custom_c_unconfigured": (
        lambda s: _custom_columns(s, False),
        ["c"],
        {"a": "x", "b": "y", "c": "SECRET"},
    ),
    "custom_c_passthrough": (
        lambda s: _custom_columns(s, True),
        ["c"],
        {"a": "x", "b": "y", "c": "SECRET"},
    ),
    "lone_name_email": (
        lambda s: _lone("composite_name_email", "first_name", s, None),
        ["last_name", "email"],
        {"first_name": "o", "last_name": "SECRET-L", "email": "SECRET-E"},
    ),
    "lone_name_email_when": (
        lambda s: _lone("composite_name_email", "first_name", s, "first_name == 'never'"),
        ["last_name", "email"],
        {"first_name": "o", "last_name": "SECRET-L", "email": "SECRET-E"},
    ),
}


def _profile_and_run(config: dict[str, Any], table: pa.Table) -> Any:
    from tests.integration.test_composite_mg4_e2e import _profile, _run

    df = table.to_pandas()
    return _profile(df, "t"), df, _run


@pytest.mark.parametrize("case", sorted(_CASES))
def test_composite_outputs_are_never_carried(case: str) -> None:
    build, written, values = _CASES[case]
    columns = build("<composite>")
    base = {"global_settings": {"seed": 7, "unconfigured_column_policy": "warn"}}
    compile_config = {**base, "tables": [{"name": "t", "columns": columns}]}
    n = 3
    src = pa.table({k: pa.array([f"{v}{i}" for i in range(n)]) for k, v in values.items()})
    df = src.to_pandas()
    from tests.integration.test_composite_mg4_e2e import _profile, _run

    profile = _profile(df, "t")
    # (i) the config compiles
    compile_plan(compile_config, profile, decoy_engine_version="0.1.0")
    # (iii) the carry puts every composite-written column in the read set, not the carried set
    carry = plan_carry(compile_config, table="t", first_schema=src.schema, adapter=None)
    assert set(written) <= set(carry.read)
    assert not (set(written) & carry.carried)
    assert read_set(columns, written) == frozenset(written)
    # (iv) end to end through the carry, the generated value survives reattachment
    full = _run(profile, compile_config, df, "t").outputs["t"]
    adapter_in = carry.adapter_input(src)
    produced = _run(profile, compile_config, adapter_in.to_pandas(), "t").outputs["t"]
    out = carry.reattach(produced, src)
    for name in written:
        assert out.column(name).to_pylist() == full.column(name).to_pylist(), name
        assert all(
            a != b
            for a, b in zip(out.column(name).to_pylist(), src.column(name).to_pylist(), strict=True)
        ), name


@pytest.mark.parametrize("strategy", ["<composite>", "faker"])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_composite_route_and_reason_on_run_mask_chunked_are_unchanged(
    case: str, strategy: str
) -> None:
    build, _written, values = _CASES[case]
    columns = build(strategy)
    n = 3
    src = pa.table({k: pa.array([f"{v}{i}" for i in range(n)]) for k, v in values.items()})
    config = make_config(columns, global_settings=_WARN)
    for runner in ("entry", "public"):
        sink: list[Any] = []
        with pytest.raises(Exception) as info:
            if runner == "entry":
                list(
                    run_mask_chunked(
                        config,
                        [src],
                        table=TABLE,
                        engine_version=ENGINE_VERSION,
                        key_provider=key_provider(),
                        chunk_result_sink=sink,
                    )
                )
            else:
                list(
                    run_mask_pipeline_chunked(
                        config,
                        [src],
                        table=TABLE,
                        engine_version=ENGINE_VERSION,
                        key_provider=key_provider(),
                        chunk_result_sink=sink,
                    )
                )
        expected = (
            "strategy_not_chunk_safe"
            if strategy == "<composite>"
            else "composite_requires_bundle_path"
        )
        assert getattr(info.value, "code", None) == expected
        assert sink == []


def test_bytes_literal_in_a_predicate_reads_every_passthrough_column() -> None:
    """A string token that is not a plain `str` literal cannot be read safely."""
    entry = {**redact("s"), "when": "s == b'x'"}
    assert read_set([entry], ["x", "y"]) == {"x", "y"}

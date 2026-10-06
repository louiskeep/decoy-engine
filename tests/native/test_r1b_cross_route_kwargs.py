"""R1b baseline: what each native route asks its kernel to do.

Two nets over the same compiled plan. The Hypothesis test proves the unified `run_operator`
and the chunked `_mask_chunk_native` hand the kernel equal arguments for every admitted config
of all nine operators. The literal snapshot proves the arguments themselves, so both routes
drifting the same way (a changed default) still fails.

The categorical operator is checked in its deterministic variant only: the positional variant
cannot bind on the unified route and has its own parity suite.

`raw_hex_kernel` is dropped from the comparison: the unified route lets the group_key kernel
load its own, the chunked route passes the preflight one.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.native._r1b_support import (
    MASK_KEY,
    NATIVE_THREADS,
    TARGET,
    compile_column,
    pools_for,
    recording_kernels,
    run_chunked,
    run_unified,
    table_for,
)

_FMT = "%Y-%m-%d"
_ALT_FMT = "%d/%m/%Y"

_VALUES = st.lists(st.one_of(st.none(), st.text(max_size=8)), min_size=1, max_size=6)
_NAMESPACE = st.sampled_from(["ns_one", "ns_two", "a/b"])


def _column(strategy: str, **extra: Any) -> dict[str, Any]:
    return {"name": TARGET, "strategy": strategy, **extra}


def _with_config(strategy: str, config: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return (
        _column(strategy, provider_config=config, **extra) if config else _column(strategy, **extra)
    )


@st.composite
def _redact(draw: st.DrawFn) -> dict[str, Any]:
    config = draw(st.one_of(st.just({}), st.text(max_size=6).map(lambda s: {"redact_with": s})))
    return _with_config("redact", config)


@st.composite
def _truncate(draw: st.DrawFn) -> dict[str, Any]:
    config: dict[str, Any] = {"length": draw(st.integers(1, 8))}
    keep = draw(st.sampled_from([None, "head", "tail"]))
    from_end = draw(st.sampled_from([None, True, False]))
    if keep is not None:
        config["keep"] = keep
    if from_end is not None:
        config["from_end"] = from_end
    if draw(st.booleans()):
        config["mask_char"] = draw(st.sampled_from(["#", "*"]))
    return _with_config("truncate", config)


@st.composite
def _hash(draw: st.DrawFn) -> dict[str, Any]:
    config = draw(st.one_of(st.just({}), st.integers(1, 32).map(lambda n: {"truncate": n})))
    return _with_config("hash", config, namespace=draw(_NAMESPACE))


@st.composite
def _faker(draw: st.DrawFn) -> dict[str, Any]:
    return _column(
        "faker",
        provider="person_first_name",
        deterministic=True,
        namespace=draw(_NAMESPACE),
        pool_size=draw(st.sampled_from([20, 40])),
    )


@st.composite
def _categorical(draw: st.DrawFn) -> dict[str, Any]:
    categories = draw(
        st.lists(st.text(min_size=1, max_size=4), min_size=1, max_size=4, unique=True)
    )
    config: dict[str, Any] = {"categories": categories}
    if draw(st.booleans()):
        config["weights"] = draw(
            st.lists(st.integers(1, 9), min_size=len(categories), max_size=len(categories))
        )
    return _with_config("categorical", config, namespace=draw(_NAMESPACE), deterministic=True)


@st.composite
def _bucket_perturb(draw: st.DrawFn) -> dict[str, Any]:
    config: dict[str, Any] = {"date_format": draw(st.sampled_from([_FMT, _ALT_FMT]))}
    bucket = draw(st.sampled_from([None, "week", "month", "quarter"]))
    if bucket is not None:
        config["bucket"] = bucket
    return _with_config("bucket_perturb", config, namespace=draw(_NAMESPACE))


@st.composite
def _group_key(draw: st.DrawFn) -> dict[str, Any]:
    config: dict[str, Any] = {"group_by": "g"}
    length = draw(st.one_of(st.none(), st.integers(4, 32).map(lambda n: n * 2)))
    if length is not None:
        config["length"] = length
    prefix = draw(st.one_of(st.just("__absent__"), st.none(), st.text(max_size=4)))
    if prefix != "__absent__":
        config["prefix"] = prefix
    return _with_config("group_key", config)


@st.composite
def _date_shift(draw: st.DrawFn) -> dict[str, Any]:
    config: dict[str, Any] = {"date_format": draw(st.sampled_from([_FMT, _ALT_FMT]))}
    for key in ("min_days", "max_days"):
        value = draw(st.one_of(st.none(), st.integers(-400, 400)))
        if value is not None:
            config[key] = value
    return _with_config("date_shift", config, namespace=draw(_NAMESPACE))


COLUMNS: dict[str, st.SearchStrategy[dict[str, Any]]] = {
    "passthrough": st.just(_column("passthrough")),
    "redact": _redact(),
    "truncate": _truncate(),
    "hash": _hash(),
    "faker": _faker(),
    "categorical": _categorical(),
    "bucket_perturb": _bucket_perturb(),
    "group_key": _group_key(),
    "date_shift": _date_shift(),
}


def _routes(column: dict[str, Any], values: list[Any]) -> tuple[list[Any], list[Any]]:
    """The recorded kernel calls of the unified route, then of the chunked route."""
    compiled = compile_column(column, table_for(column["strategy"], values))
    pools = pools_for(compiled)
    pool = pools.get(TARGET)
    with recording_kernels(pool) as unified:
        run_unified(compiled, pool)
    with recording_kernels(pool) as chunked:
        run_chunked(compiled, pools)
    return unified, chunked


def _comparable(calls: list[Any]) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    return [
        (c.kernel, c.args, {k: v for k, v in c.kwargs.items() if k != "raw_hex_kernel"})
        for c in calls
    ]


@pytest.mark.parametrize("operator", sorted(COLUMNS))
@settings(
    max_examples=60,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(data=st.data())
def test_both_routes_pass_the_kernel_equal_arguments(operator: str, data: st.DataObject) -> None:
    column = data.draw(COLUMNS[operator])
    values = data.draw(_VALUES)
    unified, chunked = _routes(column, values)
    assert len(unified) == 1, unified
    assert len(chunked) == 1, chunked
    assert _comparable(unified) == _comparable(chunked), column


# Expected kwargs, written out so a default changed identically on both routes still fails.
_FMT_KW = {"date_format": _FMT}
_KEYED = {"mask_key": MASK_KEY, "native_threads": NATIVE_THREADS}

SNAPSHOT: list[tuple[str, dict[str, Any], str, dict[str, Any]]] = [
    ("passthrough", _column("passthrough"), "native_passthrough", {}),
    (
        "redact_default",
        _column("redact"),
        "native_redact",
        {"redact_with": "REDACTED"},
    ),
    (
        "redact_given",
        _column("redact", provider_config={"redact_with": "***"}),
        "native_redact",
        {"redact_with": "***"},
    ),
    (
        "redact_empty",
        _column("redact", provider_config={"redact_with": ""}),
        "native_redact",
        {"redact_with": ""},
    ),
    (
        "truncate_keep_head",
        _column("truncate", provider_config={"length": 3, "keep": "head"}),
        "native_truncate",
        {"length": 3, "keep": "head", "mask_char": None},
    ),
    (
        "truncate_from_end",
        _column("truncate", provider_config={"length": 4, "from_end": True, "mask_char": "#"}),
        "native_truncate",
        {"length": 4, "keep": "tail", "mask_char": "#"},
    ),
    (
        "truncate_keep_wins",
        _column("truncate", provider_config={"length": 2, "keep": "tail", "from_end": False}),
        "native_truncate",
        {"length": 2, "keep": "tail", "mask_char": None},
    ),
    (
        "hash_plain",
        _column("hash", namespace="ns_h"),
        "native_keyed_hash",
        {**_KEYED, "namespace": "ns_h", "truncate": None},
    ),
    (
        "hash_truncated",
        _column("hash", namespace="ns_h", provider_config={"truncate": 8}),
        "native_keyed_hash",
        {**_KEYED, "namespace": "ns_h", "truncate": 8},
    ),
    (
        "hash_other_namespace",
        _column("hash", namespace="a/b", provider_config={"truncate": 1}),
        "native_keyed_hash",
        {**_KEYED, "namespace": "a/b", "truncate": 1},
    ),
    (
        "faker_20",
        _column(
            "faker",
            provider="person_first_name",
            deterministic=True,
            namespace="ns_f",
            pool_size=20,
        ),
        "sample_faker_array",
        {**_KEYED, "pool": "<pool>", "namespace": "ns_f", "index_kernel": "<index_kernel>"},
    ),
    (
        "faker_40",
        _column(
            "faker",
            provider="person_first_name",
            deterministic=True,
            namespace="ns_g",
            pool_size=40,
        ),
        "sample_faker_array",
        {**_KEYED, "pool": "<pool>", "namespace": "ns_g", "index_kernel": "<index_kernel>"},
    ),
    (
        "faker_other_provider",
        _column(
            "faker", provider="person_last_name", deterministic=True, namespace="ns_h", pool_size=30
        ),
        "sample_faker_array",
        {**_KEYED, "pool": "<pool>", "namespace": "ns_h", "index_kernel": "<index_kernel>"},
    ),
    (
        "categorical_uniform",
        _column(
            "categorical",
            namespace="ns_c",
            deterministic=True,
            provider_config={"categories": ["a", "b"]},
        ),
        "native_categorical",
        {
            **_KEYED,
            "categories": ("a", "b"),
            "cdf": None,
            "namespace": "ns_c",
            "index_kernel": "<index_kernel>",
        },
    ),
    (
        "categorical_weighted",
        _column(
            "categorical",
            namespace="ns_c",
            deterministic=True,
            provider_config={"categories": ["a", "b", "c"], "weights": [1, 1, 2]},
        ),
        "native_categorical",
        {
            **_KEYED,
            "categories": ("a", "b", "c"),
            "cdf": (250000, 500000, 1000000),
            "namespace": "ns_c",
            "index_kernel": "<index_kernel>",
        },
    ),
    (
        "categorical_two_weights",
        _column(
            "categorical",
            namespace="a/b",
            deterministic=True,
            provider_config={"categories": ["x", "y"], "weights": [3, 1]},
        ),
        "native_categorical",
        {
            **_KEYED,
            "categories": ("x", "y"),
            "cdf": (750000, 1000000),
            "namespace": "a/b",
            "index_kernel": "<index_kernel>",
        },
    ),
    (
        "bucket_default",
        _column("bucket_perturb", namespace="ns_b", provider_config=_FMT_KW),
        "native_bucket_perturb",
        {
            **_KEYED,
            "bucket": "month",
            "date_format": _FMT,
            "namespace": "ns_b",
            "index_kernel": "<index_kernel>",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "bucket_week",
        _column("bucket_perturb", namespace="ns_b", provider_config={**_FMT_KW, "bucket": "week"}),
        "native_bucket_perturb",
        {
            **_KEYED,
            "bucket": "week",
            "date_format": _FMT,
            "namespace": "ns_b",
            "index_kernel": "<index_kernel>",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "bucket_quarter_alt_format",
        _column(
            "bucket_perturb",
            namespace="a/b",
            provider_config={"date_format": _ALT_FMT, "bucket": "quarter"},
        ),
        "native_bucket_perturb",
        {
            **_KEYED,
            "bucket": "quarter",
            "date_format": _ALT_FMT,
            "namespace": "a/b",
            "index_kernel": "<index_kernel>",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "group_key_default",
        _column("group_key", provider_config={"group_by": "g"}),
        "native_group_key",
        {
            **_KEYED,
            "length": 16,
            "prefix": "",
            "namespace": "group_key/c",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "group_key_prefixed",
        _column("group_key", provider_config={"group_by": "g", "length": 8, "prefix": "K-"}),
        "native_group_key",
        {
            **_KEYED,
            "length": 8,
            "prefix": "K-",
            "namespace": "group_key/c",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "group_key_none_prefix",
        _column("group_key", provider_config={"group_by": "g", "length": 64, "prefix": None}),
        "native_group_key",
        {
            **_KEYED,
            "length": 64,
            "prefix": "None",
            "namespace": "group_key/c",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "date_shift_default_bounds",
        _column("date_shift", namespace="ns_d", provider_config=_FMT_KW),
        "native_date_shift",
        {
            **_KEYED,
            "min_days": -365,
            "max_days": 365,
            "date_format": _FMT,
            "namespace": "ns_d",
            "index_kernel": "<index_kernel>",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "date_shift_given_bounds",
        _column(
            "date_shift",
            namespace="ns_d",
            provider_config={**_FMT_KW, "min_days": 2, "max_days": 9},
        ),
        "native_date_shift",
        {
            **_KEYED,
            "min_days": 2,
            "max_days": 9,
            "date_format": _FMT,
            "namespace": "ns_d",
            "index_kernel": "<index_kernel>",
            "derive_calls": "<derive_calls>",
        },
    ),
    (
        "date_shift_swapped_bounds",
        _column(
            "date_shift",
            namespace="a/b",
            provider_config={"date_format": _ALT_FMT, "min_days": 30, "max_days": -30},
        ),
        "native_date_shift",
        {
            **_KEYED,
            "min_days": 30,
            "max_days": -30,
            "date_format": _ALT_FMT,
            "namespace": "a/b",
            "index_kernel": "<index_kernel>",
            "derive_calls": "<derive_calls>",
        },
    ),
]


@pytest.mark.parametrize(
    "column, kernel, expected", [pytest.param(c, k, e, id=i) for i, c, k, e in SNAPSHOT]
)
def test_each_route_passes_the_literal_kernel_arguments(
    column: dict[str, Any], kernel: str, expected: dict[str, Any]
) -> None:
    unified, chunked = _routes(column, ["x", None, "y"])
    for route, calls in (("unified", unified), ("chunked", chunked)):
        assert len(calls) == 1, (route, calls)
        assert calls[0].kernel == kernel, route
        assert calls[0].args == (), route
        kwargs = {k: v for k, v in calls[0].kwargs.items() if k != "raw_hex_kernel"}
        assert kwargs == expected, route

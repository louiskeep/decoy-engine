"""Phase D / D1c: sentinel-preserve on the pandas oracle route (no companion needed).

The oracle is the reference the native and out-of-core routes are held to, so the contract is
pinned here first: unset config is the old behavior byte for byte, a listed sentinel comes out
unchanged, every other row masks as it did without the config, and a sentinel never reaches
derivation, the range arithmetic or the whole-column strftime.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.determinism._derive import DeriveContext
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._bucket_perturb import BucketPerturbStrategyHandler
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.transforms import bucket_perturb as bucket_module
from tests.parity._d1c_data import (
    DMY,
    IN_RANGE,
    ISO,
    JOB_SEED,
    NORMAL,
    OPERATORS,
    OUT_OF_RANGE,
    operator_config,
    preserved_total,
    run_oracle,
    seed_for,
    strings,
)

_BOTH = pytest.mark.parametrize("strategy", sorted(OPERATORS))


def _mixed() -> pa.Array:
    return strings(NORMAL[0], IN_RANGE, None, "not-a-date", OUT_OF_RANGE, NORMAL[1], IN_RANGE, "")


@_BOTH
def test_unset_empty_and_matching_nothing_are_the_baseline(strategy: str) -> None:
    base = run_oracle(strategy, operator_config(strategy), _mixed())
    for extra in ({"preserve_values": []}, {"preserve_values": None}):
        got = run_oracle(strategy, operator_config(strategy, **extra), _mixed())
        assert (got.values, got.type, got.errors, got.metrics) == (
            base.values,
            base.type,
            base.errors,
            base.metrics,
        )
    nothing = run_oracle(
        strategy, operator_config(strategy, preserve_values=["1999-09-09"]), _mixed()
    )
    # Matching nothing is byte-identical to unset and reports no preserved row.
    assert (nothing.values, nothing.type, nothing.errors) == (base.values, base.type, base.errors)
    assert preserved_total(nothing.metrics) == 0


@_BOTH
def test_in_range_sentinel_is_perturbed_without_config_and_kept_with_it(strategy: str) -> None:
    base = run_oracle(strategy, operator_config(strategy), _mixed())
    kept = run_oracle(strategy, operator_config(strategy, preserve_values=[IN_RANGE]), _mixed())
    assert base.values[1] != IN_RANGE and base.values[6] != IN_RANGE
    assert kept.values[1] == IN_RANGE and kept.values[6] == IN_RANGE
    for i in (0, 2, 3, 4, 5, 7):
        assert kept.values[i] == base.values[i], i
    assert preserved_total(kept.metrics) == 2


@_BOTH
def test_out_of_range_sentinel_is_preserved_explicitly(strategy: str) -> None:
    """9999-12-31 survives without config only by accident (it parses to NaT); with config the
    preserved-row count proves the match was deliberate."""
    base = run_oracle(strategy, operator_config(strategy), _mixed())
    assert base.values[4] == OUT_OF_RANGE
    assert preserved_total(base.metrics) == 0
    kept = run_oracle(strategy, operator_config(strategy, preserve_values=[OUT_OF_RANGE]), _mixed())
    assert kept.values[4] == OUT_OF_RANGE
    assert preserved_total(kept.metrics) == 1
    assert kept.values[:4] == base.values[:4]


def test_date_shift_sentinel_is_not_a_format_error() -> None:
    base = run_oracle("date_shift", operator_config("date_shift"), _mixed())
    kept = run_oracle(
        "date_shift",
        operator_config("date_shift", preserve_values=[OUT_OF_RANGE, IN_RANGE]),
        _mixed(),
    )
    assert sorted(e.row_index for e in base.errors) == [3, 4, 7]
    # The malformed and empty cells still report; the two sentinel kinds do not.
    assert sorted(e.row_index for e in kept.errors) == [3, 7]


@_BOTH
def test_format_variant_sentinel_matches_by_date(strategy: str) -> None:
    column = strings("01/01/2099", "05/03/2024", "31/12/9999")
    config = operator_config(strategy, date_format=DMY, preserve_values=[IN_RANGE, OUT_OF_RANGE])
    got = run_oracle(strategy, config, column)
    assert got.values[0] == "01/01/2099" and got.values[2] == "31/12/9999"
    base = run_oracle(strategy, operator_config(strategy, date_format=DMY), column)
    assert got.values[1] == base.values[1]
    assert preserved_total(got.metrics) == 2


@_BOTH
def test_a_sentinel_with_time_of_day_matches_by_calendar_date(strategy: str) -> None:
    stamp = "%Y-%m-%d %H:%M:%S"
    column = strings("2099-01-01 13:45:09", "2024-03-05 00:00:00")
    config = operator_config(strategy, date_format=stamp, preserve_values=[IN_RANGE])
    got = run_oracle(strategy, config, column)
    base = run_oracle(strategy, operator_config(strategy, date_format=stamp), column)
    assert got.values[0] == "2099-01-01 13:45:09"
    assert got.values[1] == base.values[1]


@_BOTH
def test_typed_date32_sentinel_renders_as_text_and_the_column_stays_a_string(
    strategy: str,
) -> None:
    column = pa.array([dt.date(9999, 12, 31), dt.date(2099, 1, 1), None], pa.date32())
    config = operator_config(strategy, preserve_values=[OUT_OF_RANGE, IN_RANGE])
    got = run_oracle(strategy, config, column)
    assert got.values == [OUT_OF_RANGE, IN_RANGE, None]
    assert got.type == pa.string()
    assert preserved_total(got.metrics) == 2


@_BOTH
def test_typed_timestamp_sentinel_renders_by_calendar_date_in_the_field_format(
    strategy: str,
) -> None:
    column = pa.array([dt.datetime(2099, 1, 1, 13, 5), None], pa.timestamp("us"))
    got = run_oracle(strategy, operator_config(strategy, preserve_values=[IN_RANGE]), column)
    assert got.values == [IN_RANGE, None]
    assert got.type == pa.string()


@_BOTH
def test_dates_next_to_the_pandas_bounds_are_preserved_not_computed(strategy: str) -> None:
    low, high = "1677-09-22", "2262-04-10"
    column = strings(low, NORMAL[0], high)
    got = run_oracle(strategy, operator_config(strategy, preserve_values=[low, high]), column)
    assert got.values[0] == low and got.values[2] == high
    assert got.values[1] != NORMAL[0]
    assert preserved_total(got.metrics) == 2


@_BOTH
def test_a_non_sentinel_bound_row_behaves_as_it_did(strategy: str) -> None:
    """The carve-out must not change what happens to a row it does not preserve."""
    column = strings("2262-04-10", NORMAL[0])

    def outcome(config: dict[str, Any]) -> Any:
        try:
            return run_oracle(strategy, config, column).values
        except Exception as exc:
            return type(exc)

    assert outcome(operator_config(strategy)) == outcome(
        operator_config(strategy, preserve_values=[OUT_OF_RANGE])
    )


@_BOTH
def test_empty_input_and_all_sentinel_input(strategy: str) -> None:
    config = operator_config(strategy, preserve_values=[IN_RANGE])
    empty = run_oracle(strategy, config, strings())
    assert empty.values == [] and empty.errors == ()
    allkept = run_oracle(strategy, config, strings(IN_RANGE, IN_RANGE))
    assert allkept.values == [IN_RANGE, IN_RANGE]
    assert allkept.type == pa.string()
    assert preserved_total(allkept.metrics) == 2


def test_date_shift_row_error_positions_are_the_original_positions() -> None:
    column = strings(IN_RANGE, "bad-1", NORMAL[0], OUT_OF_RANGE, None, "bad-2", IN_RANGE)
    config = operator_config("date_shift", preserve_values=[IN_RANGE, OUT_OF_RANGE])
    got = run_oracle("date_shift", config, column)
    assert [e.row_index for e in got.errors] == [1, 5]
    assert got.values[1] == "bad-1" and got.values[5] == "bad-2"


@_BOTH
def test_deterministic_across_runs_and_independent_of_neighbouring_sentinels(
    strategy: str,
) -> None:
    config = operator_config(strategy, preserve_values=[IN_RANGE])
    a = run_oracle(strategy, config, strings(NORMAL[0], IN_RANGE, NORMAL[1]))
    b = run_oracle(strategy, config, strings(NORMAL[1], NORMAL[0]))
    assert a.values == run_oracle(strategy, config, strings(NORMAL[0], IN_RANGE, NORMAL[1])).values
    assert [a.values[0], a.values[2]] == [b.values[1], b.values[0]]


# Preserved rows must never reach derivation, the range arithmetic or the whole-column format.


def _frame_and_ctx() -> tuple[pd.DataFrame, StrategyContext]:
    from decoy_engine.generation.pool import PoolCache
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._graph import RelationshipGraph
    from decoy_engine.relationships._namespace import NamespaceRegistry

    ctx = StrategyContext(
        registry=get_default_registry(),
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=JOB_SEED,
        current_table="t",
    )
    return pd.DataFrame({"d": pd.Series(list(_mixed().to_pylist()), dtype=object)}), ctx


def test_bucket_perturb_never_derives_or_computes_a_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    real = bucket_module._perturb_date

    def spy(date: dt.date, bucket: str, job_seed: bytes, namespace: str, value_str: str) -> Any:
        seen.append(value_str)
        return real(date, bucket, job_seed, namespace, value_str)

    monkeypatch.setattr(bucket_module, "_perturb_date", spy)
    frame, ctx = _frame_and_ctx()
    seed = seed_for(
        "bucket_perturb",
        operator_config("bucket_perturb", preserve_values=[IN_RANGE, OUT_OF_RANGE]),
    )
    BucketPerturbStrategyHandler().run(frame, "d", seed, ctx)
    assert seen and IN_RANGE not in seen and OUT_OF_RANGE not in seen


def test_date_shift_never_derives_or_formats_a_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    derived: list[bytes] = []
    real = DeriveContext.derive_sources

    def spy(self: DeriveContext, namespace: str, sources: list[bytes]) -> Any:
        derived.extend(sources)
        return real(self, namespace, sources)

    monkeypatch.setattr(DeriveContext, "derive_sources", spy)
    frame, ctx = _frame_and_ctx()
    seed = seed_for(
        "date_shift", operator_config("date_shift", preserve_values=[IN_RANGE, OUT_OF_RANGE])
    )
    DateShiftStrategyHandler().run(frame, "d", seed, ctx)
    assert derived
    assert _canonicalize_source(IN_RANGE) not in derived
    assert _canonicalize_source(OUT_OF_RANGE) not in derived


def test_date_shift_does_not_format_the_whole_column_with_sentinels_in_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boundary sentinel in a column the shift would overflow must not crash the strftime."""
    column = strings("2262-04-11", NORMAL[0])
    kept = run_oracle(
        "date_shift", operator_config("date_shift", preserve_values=["2262-04-11"]), column
    )
    assert kept.values[0] == "2262-04-11"
    assert kept.values[1] != NORMAL[0]


# Config the oracle handlers reject on their own, so a route without a compile pass still fails.


@pytest.mark.parametrize(
    "extra",
    [
        {"preserve_values": ["not-a-date"]},
        {"preserve_values": ["2020-02-30"]},
        {"preserve_values": [""]},
        {"preserve_values": ["NaT"]},
        {"preserve_values": [20200101]},
        {"preserve_values": IN_RANGE},
        {"preserve_values": [IN_RANGE], "date_format": "%Y-%m-%d %H:%M:%S.%f"},
        {"preserve_values": [IN_RANGE], "date_format": "%Y-%m-%d %Z"},
        {"preserve_values": [IN_RANGE], "date_format": "ISO8601"},
        {"preserve_values": [IN_RANGE], "date_format": "mixed"},
    ],
    ids=lambda e: next(iter(e.values())) if isinstance(next(iter(e.values())), str) else "cfg",
)
@_BOTH
def test_an_invalid_config_is_a_coded_error_on_zero_rows_too(
    strategy: str, extra: dict[str, Any]
) -> None:
    config = operator_config(strategy, **extra)
    with pytest.raises(StrategyError):
        run_oracle(strategy, config, strings(NORMAL[0]))


@_BOTH
def test_preserve_values_without_a_date_format_is_a_coded_error(strategy: str) -> None:
    config = {**OPERATORS[strategy], "preserve_values": [IN_RANGE]}
    with pytest.raises(StrategyError):
        run_oracle(strategy, config, strings(NORMAL[0]))


def test_date_shift_group_by_with_preserve_values_fails_closed() -> None:
    frame, ctx = _frame_and_ctx()
    config = operator_config("date_shift", preserve_values=[IN_RANGE], group_by="g")
    with pytest.raises(StrategyError) as caught:
        DateShiftStrategyHandler().run(frame, "d", seed_for("date_shift", config), ctx)
    assert caught.value.code == "date_preserve_values_with_group_by_unsupported"


def test_the_default_format_constants_are_the_documented_ones() -> None:
    assert (ISO, DMY) == ("%Y-%m-%d", "%d/%m/%Y")

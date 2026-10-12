"""Phase D / D1c: sentinel-preserve on the native routes (compiled companion required).

Native chunked is held to the forced-oracle twin byte for byte, native full-frame to the legacy
pandas pipeline, and out-of-core to the Python path it replaces. Expected values never come
from the output under test: they are literals or an unconfigured run of the same operator.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution.native._chunked_evidence import aggregate_chunked_route_evidence
from decoy_engine.keyprovider import SecretKeyProvider
from tests.native._b8_support import FORCE, assert_same_as_oracle, identical, run_one, run_pair
from tests.native._chunked_bucket_perturb_support import bp_col, spy_index_kernel
from tests.native._chunked_date_shift_support import ds_col
from tests.native._chunked_entry_support import NEEDS_COMPANION, make_config, passthrough, split
from tests.parity._d1c_data import IN_RANGE, NORMAL, OUT_OF_RANGE, table_digest
from tests.physical import _shadow_helpers
from tests.physical._shadow_helpers import (
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

pytestmark = NEEDS_COMPANION

_VALUES: list[str | None] = [
    NORMAL[0],
    IN_RANGE,
    None,
    NORMAL[1],
    OUT_OF_RANGE,
    "not-a-date",
    IN_RANGE,
    NORMAL[2],
]
# date_shift fails closed on a malformed value on the chunked route; keep the chunked data clean.
_CLEAN: list[str | None] = [v for v in _VALUES if v != "not-a-date"]
_KEEP = [IN_RANGE, OUT_OF_RANGE]


def _col(strategy: str, **pc: Any) -> dict[str, Any]:
    if strategy == "bucket_perturb":
        col = bp_col()
    else:
        col = ds_col()
    col["provider_config"] = {**col["provider_config"], **pc}
    return col


def _digest(run: Any) -> str:
    return table_digest(pa.concat_tables(run.out, promote_options="default"))


def _source(values: list[str | None]) -> pa.Table:
    return pa.table(
        {
            "d": pa.array(values, pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


_BOTH = pytest.mark.parametrize("strategy", ["bucket_perturb", "date_shift"])


# ---- native chunked --------------------------------------------------------------------------


@_BOTH
@pytest.mark.parametrize("size", [1, 3, 50_000])
def test_native_chunked_equals_oracle_chunked_and_keeps_the_sentinels(
    strategy: str, size: int
) -> None:
    chunks = split(_source(_CLEAN), size)
    cols = [_col(strategy, preserve_values=_KEEP), passthrough("p")]
    native, forced = run_pair(cols, chunks)
    assert_same_as_oracle(native, forced)
    out = pa.concat_tables(native.out, promote_options="default").column("d").to_pylist()
    for got, src in zip(out, _CLEAN, strict=True):
        if src in _KEEP:
            assert got == src
    assert any(g != s for g, s in zip(out, _CLEAN, strict=True) if s == NORMAL[0])


def test_unset_and_matching_nothing_are_the_baseline_on_every_chunked_leg() -> None:
    clean = [v for v in _CLEAN if v != OUT_OF_RANGE]  # unset date_shift rejects it
    chunks = split(_source(clean), 3)
    digests = set()
    for extra in ({}, {"preserve_values": []}, {"preserve_values": ["1999-09-09"]}):
        for strategy in ("bucket_perturb",):
            native, forced = run_pair([_col(strategy, **extra), passthrough("p")], chunks)
            assert_same_as_oracle(native, forced)
            digests.add(table_digest(pa.concat_tables(native.out, promote_options="default")))
    assert len(digests) == 1


@pytest.mark.parametrize(
    "extra", [{}, {"preserve_values": []}, {"preserve_values": ["1999-09-09"]}]
)
def test_date_shift_unset_and_matching_nothing_agree(extra: dict[str, Any]) -> None:
    clean = [v for v in _CLEAN if v != OUT_OF_RANGE]
    chunks = split(_source(clean), 3)
    base = run_one(make_config([_col("date_shift"), passthrough("p")]), chunks)
    got = run_one(make_config([_col("date_shift", **extra), passthrough("p")]), chunks)
    assert _digest(got) == _digest(base)


@_BOTH
def test_a_sentinel_never_reaches_the_kernel(
    strategy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_index_kernel(monkeypatch)
    run_one(
        make_config([_col(strategy, preserve_values=_KEEP), passthrough("p")]),
        split(_source(_CLEAN), 50_000),
    )
    assert spy.calls
    # Every row handed to the kernel is a non-sentinel: 2 sentinel kinds, 3 occurrences.
    handed = sum(c["rows"] for c in spy.calls)
    non_sentinel = [v for v in _CLEAN if v is not None and v not in _KEEP]
    assert handed == len(non_sentinel)


@_BOTH
def test_an_all_sentinel_chunk_makes_no_kernel_call(
    strategy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_index_kernel(monkeypatch)
    run = run_one(
        make_config([_col(strategy, preserve_values=_KEEP), passthrough("p")]),
        [_source([IN_RANGE, OUT_OF_RANGE, IN_RANGE])],
    )
    assert spy.calls == []
    assert run.out[0].column("d").to_pylist() == [IN_RANGE, OUT_OF_RANGE, IN_RANGE]
    # The compiled kernel never ran, so the route does not claim it did.
    (column,) = [
        c for c in run.sink[0].quality_metrics["chunked_route"]["columns"] if c["column"] == "d"
    ]
    assert column["executed_backend"] == "arrow_python"


@_BOTH
def test_chunked_preserved_count_aggregates_across_chunks_and_matches_the_oracle(
    strategy: str,
) -> None:
    chunks = split(_source(_CLEAN), 2)
    native, forced = run_pair([_col(strategy, preserve_values=_KEEP), passthrough("p")], chunks)
    want = sum(1 for v in _CLEAN if v in _KEEP)
    for run in (native, forced):
        evidence = aggregate_chunked_route_evidence(run.sink)
        (column,) = [c for c in evidence["columns"] if c["column"] == "d"]
        assert column["preserved_rows"] == want


@_BOTH
def test_chunked_count_key_is_absent_when_unset(strategy: str) -> None:
    run = run_one(make_config([_col(strategy), passthrough("p")]), split(_source(NORMAL), 50_000))
    for column in run.sink[0].quality_metrics["chunked_route"]["columns"]:
        assert "preserved_rows" not in column


def test_date_shift_chunked_sentinel_is_not_a_format_error_but_garbage_still_is() -> None:
    from decoy_engine.errors import RowErrorsFailedError

    cfg = make_config([_col("date_shift", preserve_values=_KEEP), passthrough("p")])
    run_one(cfg, [_source([OUT_OF_RANGE, NORMAL[0]])])
    with pytest.raises(RowErrorsFailedError) as caught:
        run_one(cfg, [_source([OUT_OF_RANGE, "garbage", NORMAL[0]])])
    assert [r.row_index for r in caught.value.records] == [1]


@_BOTH
def test_chunked_when_selection_keeps_sentinels_in_the_selected_rows(strategy: str) -> None:
    col = _col(strategy, preserve_values=_KEEP)
    col["when"] = "p >= 1"
    native, forced = run_pair([col, passthrough("p")], split(_source(_CLEAN), 3))
    for got, want in zip(native.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))
    joined = pa.concat_tables(native.out, promote_options="default").column("d").to_pylist()
    assert all(g == s for g, s in zip(joined, _CLEAN, strict=True) if s in _KEEP)


@_BOTH
def test_chunked_invalid_config_is_the_oracle_error(strategy: str) -> None:
    cfg = make_config([_col(strategy, preserve_values=["nope"]), passthrough("p")])
    with pytest.raises(Exception) as caught:
        run_one(cfg, [_source(NORMAL)])
    assert "nope" not in str(caught.value)


# ---- native full-frame -----------------------------------------------------------------------


@pytest.fixture
def _pandas_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _shadow_helpers,
        "run_pipeline",
        functools.partial(run_pipeline, unified_slice_enabled=False),
    )


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


def _frame_column(strategy: str, **pc: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"date_format": "%Y-%m-%d", **pc}
    cfg.update(
        {"bucket": "month"} if strategy == "bucket_perturb" else {"min_days": -30, "max_days": 30}
    )
    return {"name": "c", "strategy": strategy, "namespace": "ns", "provider_config": cfg}


@_BOTH
@pytest.mark.parametrize("batch", [2, 50_000])
def test_native_full_frame_matches_the_pandas_pipeline(
    strategy: str, batch: int, tmp_path: Path, _pandas_oracle: None
) -> None:
    values = [v for v in _VALUES if v != "not-a-date"]
    source = pa.table({"c": pa.array(values, pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_frame_column(strategy, preserve_values=_KEEP)])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp(), batch_size_rows=batch)
    assert_shadow_matches_oracle(run)
    out = run.shadow.outputs["t"].column("c").to_pylist()
    for got, src in zip(out, values, strict=True):
        if src in _KEEP:
            assert got == src


@_BOTH
def test_native_full_frame_all_sentinel_table(
    strategy: str, tmp_path: Path, _pandas_oracle: None
) -> None:
    source = pa.table({"c": pa.array([IN_RANGE, OUT_OF_RANGE], pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_frame_column(strategy, preserve_values=_KEEP)])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    assert_shadow_matches_oracle(run)
    assert run.shadow.outputs["t"].column("c").to_pylist() == [IN_RANGE, OUT_OF_RANGE]

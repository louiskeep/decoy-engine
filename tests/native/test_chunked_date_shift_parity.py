"""C4 acceptance: date_shift on the chunked route, parity, value-keyed KAT and determinism.

Native chunked output must equal the pandas-oracle chunked output (values, Arrow field
type, metadata) across chunk shapes, chunk sizes, shift ranges and thread counts. The
unparseable-value case is a fail-closed error on both legs and lives in
`test_chunked_date_shift_types_errors.py`.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.determinism import derive
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from tests.native._b8_support import identical
from tests.native._chunked_date_shift_support import (
    assert_same_as_oracle,
    date_value,
    ds_col,
    make_config,
    passthrough,
    run_one,
    run_outcome,
    run_pair,
    source,
)
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    column_values,
    key_provider,
    split,
)
from tests.native.test_chunked_entry_values_schema import _full_frame

_VALUED = [date_value(i) for i in range(23)]
_RAGGED: list[str | None] = [None if i % 6 == 2 else v for i, v in enumerate(_VALUED)]

_SHAPES: dict[str, list[str | None]] = {
    "empty": [],
    "all_null": [None] * 7,
    "single_row": [date_value(3)],
    "ragged": _RAGGED,
    "all_null_beside_valued": [None, date_value(1), None, None, date_value(2), None],
    "null_block_then_valued": [None] * 7 + [date_value(i) for i in range(9)],
    "valued_then_null_block": [date_value(i) for i in range(9)] + [None] * 7,
}
_RANGES = {"default": (-30, 30), "one_day": (0, 0), "wide": (-365, 365), "inverted": (30, -30)}


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("bounds", sorted(_RANGES))
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_native_chunked_equals_oracle_chunked(
    shape: str, bounds: str, size: int, threads: int
) -> None:
    lo, hi = _RANGES[bounds]
    chunks = split(source(_SHAPES[shape]), size) or [source([])]
    native, forced = run_pair(
        [ds_col(min_days=lo, max_days=hi), passthrough("p")], chunks, native_threads=threads
    )
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_empty_and_all_null_chunks_between_valued_chunks_are_identical(threads: int) -> None:
    chunks = [
        source([date_value(i) for i in range(4)]),
        source([]),
        source([None, None, None]),
        source([date_value(i) for i in range(4, 9)]),
        source([]),
    ]
    native, forced = run_pair([ds_col(), passthrough("p")], chunks, native_threads=threads)
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_a_multi_chunk_fifty_thousand_row_run_is_identical_on_both_legs(threads: int) -> None:
    values = [None if i % 11 == 3 else date_value(i) for i in range(100_003)]
    native, forced = run_pair(
        [ds_col(), passthrough("p")], split(source(values), 50_000), native_threads=threads
    )
    assert len(native.out) == 3
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
def test_two_namespaces_give_independent_output_and_both_match_the_oracle() -> None:
    table = pa.table(
        {
            "a": pa.array([date_value(i) for i in range(12)], pa.string()),
            "b": pa.array([date_value(i) for i in range(12)], pa.string()),
        }
    )
    columns = [ds_col("a", namespace="ns_a"), ds_col("b", namespace="ns_b")]
    native, forced = run_pair(columns, split(table, 5))
    assert_same_as_oracle(native, forced)
    assert column_values(native.out, "a") != column_values(native.out, "b")


# ---------------------------------------------------------------------------
# 2. Value-keyed known-answer vectors (frozen from the oracle, plus the formula).
# ---------------------------------------------------------------------------

_KAT_VALUES: list[str | None] = [
    "2020-01-15",
    "2021-02-28",
    "2024-02-29",
    "2019-12-31",
    "1960-03-01",
    None,
    "2020-01-15",
]
_KAT = {
    ("ns_a", -30, 30): [
        "2019-12-24",
        "2021-02-02",
        "2024-03-30",
        "2019-12-10",
        "1960-02-15",
        None,
        "2019-12-24",
    ],
    ("ns_b", -30, 30): [
        "2019-12-23",
        "2021-02-07",
        "2024-02-16",
        "2020-01-26",
        "1960-02-03",
        None,
        "2019-12-23",
    ],
    ("ns_a", 0, 3650): [
        "2027-10-24",
        "2025-05-27",
        "2026-02-10",
        "2022-11-01",
        "1969-05-29",
        None,
        "2027-10-24",
    ],
}


def _expected_by_formula(
    value: str | None, ns: str, lo: int, hi: int, secret: bytes | None = None
) -> str | None:
    """The oracle's documented offset, computed here from `derive` alone."""
    if value is None:
        return None
    import datetime as dt

    key = (key_provider() if secret is None else key_provider(secret)).mask_key()
    digest = derive(key, ns, _canonicalize_source(value))
    shift = lo + int.from_bytes(digest[:8], "big") % (hi - lo + 1)
    day = dt.datetime.strptime(value, "%Y-%m-%d") + dt.timedelta(days=shift)
    return day.strftime("%Y-%m-%d")


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_KAT))
def test_native_chunked_output_matches_the_frozen_vectors_and_the_formula(
    case: tuple[str, int, int],
) -> None:
    ns, lo, hi = case
    run = run_one(
        make_config([ds_col(namespace=ns, min_days=lo, max_days=hi), passthrough("p")]),
        split(source(_KAT_VALUES), 3),
    )
    assert run.ev[0].native_admitted is True
    got = column_values(run.out, "d")
    assert got == _KAT[case]
    assert got == [_expected_by_formula(v, ns, lo, hi) for v in _KAT_VALUES]


_OTHER_SECRET = bytes(range(1, 33))


@NEEDS_COMPANION
def test_a_different_mask_key_gives_a_different_shift_pinned_to_the_formula() -> None:
    """Kills a mutant that hard-codes or swaps in a wrong mask key while keeping the
    namespace: both runs share namespace and input, so only the key differs, and each
    output must equal the scalar `derive(mask_key, ns, value)` shift for ITS key."""
    values = [date_value(i) for i in range(40)]
    config = make_config([ds_col(), passthrough("p")])
    outputs = {}
    for label, secret in (("default", None), ("other", _OTHER_SECRET)):
        out = run_outcome(config, [source(values)], secret=secret)
        assert out.error is None and out.ev[0].native_admitted is True
        got = column_values(out.out, "d")
        assert got == [_expected_by_formula(v, "ns_d", -30, 30, secret) for v in values], label
        outputs[label] = got
    assert outputs["default"] != outputs["other"]


@NEEDS_COMPANION
def test_a_different_namespace_gives_a_different_shift() -> None:
    values = [date_value(i) for i in range(40)]
    base = run_one(make_config([ds_col(), passthrough("p")]), [source(values)])
    other_ns = run_one(
        make_config([ds_col(namespace="ns_other"), passthrough("p")]), [source(values)]
    )
    assert column_values(base.out, "d") != column_values(other_ns.out, "d")
    assert base.ev[0].native_admitted is True and other_ns.ev[0].native_admitted is True


@NEEDS_COMPANION
def test_same_value_shifts_the_same_in_every_chunk_and_matches_full_frame(tmp_path: Path) -> None:
    values: list[str | None] = [
        "2020-02-10",
        "2021-02-10",
        "2024-02-29",
        None,
        "2020-02-10",
        "2019-12-31",
        "2024-02-29",
        "2021-02-10",
        "2020-03-31",
        "2020-02-10",
    ]
    config = make_config([ds_col(), passthrough("p")])
    table = source(values)
    run = run_one(config, split(table, 3))
    assert run.ev[0].native_admitted is True
    got = column_values(run.out, "d")
    seen: dict[str, str] = {}
    for src, out in zip(values, got, strict=True):
        if src is None:
            assert out is None
            continue
        assert seen.setdefault(src, out) == out
    assert got == _full_frame(config, table, tmp_path).column("d").to_pylist()


# ---------------------------------------------------------------------------
# 3. Thread-count invariance.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_output_bytes_do_not_depend_on_the_thread_count() -> None:
    values = [None if i % 9 == 4 else date_value(i) for i in range(6_000)]
    config = make_config([ds_col(), passthrough("p")])
    chunks = split(source(values), 1_700)
    first = run_one(config, chunks, native_threads=1)
    assert first.ev[0].native_admitted is True
    for threads in (2, 4, 8):
        other = run_one(config, chunks, native_threads=threads).out
        assert len(other) == len(first.out)
        assert all(identical(a, b) for a, b in zip(first.out, other, strict=True)), threads

from __future__ import annotations

import polars as pl
import pytest

from decoy_engine.subset._errors import SubsetConfigError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._seed import select_seed_rows
from decoy_engine.subset._types import Predicate, SeedSpec
from tests.unit.subset.conftest import JOB_SEED


def _kf(**cols) -> pl.DataFrame:
    return pl.DataFrame(cols).with_row_index(RI)


def test_sample_mode_deterministic_and_seed_sensitive() -> None:
    kf = _kf(id=list(range(1000)))
    spec = SeedSpec(table="t", mode="sample", key_columns=("id",), fraction=0.02)
    rows1, counts1, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    rows2, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert len(rows1["t"]) == 20
    assert counts1["t"] == 20
    assert rows1["t"] == rows2["t"]

    other_seed = b"\x08\x07\x06\x05\x04\x03\x02\x01"
    rows3, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=other_seed)
    assert rows3["t"] != rows1["t"]


def test_sample_mode_row_order_independent_for_unique_keys() -> None:
    kf = _kf(id=list(range(200)))
    spec = SeedSpec(table="t", mode="sample", key_columns=("id",), fraction=0.1)
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    selected_ids = set(kf.filter(pl.col(RI).is_in(list(rows["t"])))["id"].to_list())

    shuffled = kf.select("id").sample(fraction=1.0, shuffle=True, seed=42).with_row_index(RI)
    rows2, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": shuffled}, job_seed=JOB_SEED)
    selected_ids2 = set(shuffled.filter(pl.col(RI).is_in(list(rows2["t"])))["id"].to_list())

    assert selected_ids == selected_ids2


def test_count_wins_over_fraction_and_clamps_to_n() -> None:
    kf = _kf(id=list(range(10)))
    spec = SeedSpec(table="t", mode="sample", key_columns=("id",), count=5)
    rows, counts, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert counts["t"] == 5

    spec_big = SeedSpec(table="t", mode="sample", key_columns=("id",), count=1000)
    rows_big, counts_big, _ = select_seed_rows(
        seeds=(spec_big,), key_frames={"t": kf}, job_seed=JOB_SEED
    )
    assert counts_big["t"] == 10  # clamped to n


def test_fraction_floor_selects_at_least_one_row() -> None:
    kf = _kf(id=list(range(1000)))
    spec = SeedSpec(table="t", mode="sample", key_columns=("id",), fraction=0.0001)
    rows, counts, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert counts["t"] == 1


def test_null_key_components_excluded_and_counted() -> None:
    kf = _kf(id=[1, 2, None, 4])
    spec = SeedSpec(table="t", mode="sample", key_columns=("id",), count=2)
    _, _, null_excluded = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert null_excluded["t"] == 1


def test_float_key_column_raises_uncanonicalizable() -> None:
    kf = _kf(id=[1.0, 2.0, 3.0])
    spec = SeedSpec(table="t", mode="sample", key_columns=("id",), count=1)
    with pytest.raises(SubsetConfigError) as excinfo:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert excinfo.value.code == "subset_seed_key_uncanonicalizable"


def test_filter_mode_predicates_and_ops() -> None:
    kf = _kf(region=["EU", "US", "EU", "EU"], active=[True, True, False, True], id=[1, 2, 3, 4])
    spec = SeedSpec(
        table="t",
        mode="filter",
        predicates=(
            Predicate(column="region", op="eq", value="EU"),
            Predicate(column="active", op="eq", value=True),
        ),
    )
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert set(kf.filter(pl.col(RI).is_in(list(rows["t"])))["id"].to_list()) == {1, 4}


def test_filter_mode_in_and_is_null() -> None:
    kf = _kf(id=[1, 2, 3, 4], tag=["a", None, "c", "a"])
    spec_in = SeedSpec(
        table="t", mode="filter", predicates=(Predicate(column="tag", op="in", value=("a", "c")),)
    )
    rows_in, _, _ = select_seed_rows(seeds=(spec_in,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert set(kf.filter(pl.col(RI).is_in(list(rows_in["t"])))["id"].to_list()) == {1, 3, 4}

    spec_null = SeedSpec(
        table="t", mode="filter", predicates=(Predicate(column="tag", op="is_null"),)
    )
    rows_null, _, _ = select_seed_rows(seeds=(spec_null,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert set(kf.filter(pl.col(RI).is_in(list(rows_null["t"])))["id"].to_list()) == {2}


def test_keys_mode_selects_all_matching_duplicates() -> None:
    kf = _kf(id=[1, 1, 2, 3])
    spec = SeedSpec(table="t", mode="keys", key_columns=("id",), keys=((1,),))
    rows, counts, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert counts["t"] == 2


def test_keys_mode_wrong_type_raises() -> None:
    kf = _kf(id=[1, 2, 3])
    spec = SeedSpec(table="t", mode="keys", key_columns=("id",), keys=(("not-an-int",),))
    with pytest.raises(SubsetConfigError) as excinfo:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert excinfo.value.code == "subset_seed_key_type"


def test_composite_framing_injectivity() -> None:
    # The load-bearing assertion: the per-row digest framing (length-prefixed
    # components) must NOT collide when "ab"+"c" == "a"+"bc" as raw concatenation.
    from decoy_engine.determinism._derive import DeriveContext
    from decoy_engine.generation.pool._canonicalize import _canonicalize_source

    ctx = DeriveContext.for_column(JOB_SEED, "subset/sample/t")

    def digest(parts: tuple[str, ...]) -> bytes:
        source = b"".join(
            len(part := _canonicalize_source(component)).to_bytes(4, "big") + part
            for component in parts
        )
        return ctx.derive_source("subset/sample/t", source)

    assert digest(("ab", "c")) != digest(("a", "bc"))


# --- explicit keys: lossless cast to the key column dtype ---------------------
# Marker values a refusal message must never echo back (value-free errors).
_CANARY_FLOAT = 31337.4242
_CANARY_INT = 7_311_337_000
_CANARY_STR = "31337-canary"


def _keys_rows(kf: pl.DataFrame, *keys: tuple) -> set:
    spec = SeedSpec(table="t", mode="keys", key_columns=tuple(kf.columns[1:]), keys=keys)
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    return set(kf.filter(pl.col(RI).is_in(list(rows["t"])))["id"].to_list())


def _assert_keys_refused(kf: pl.DataFrame, *keys: tuple, markers: tuple[str, ...]) -> None:
    spec = SeedSpec(table="t", mode="keys", key_columns=tuple(kf.columns[1:]), keys=keys)
    with pytest.raises(SubsetConfigError) as excinfo:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert excinfo.value.code == "subset_seed_key_type"
    text = str(excinfo.value)
    assert "'t'" in text and "'id'" in text
    for marker in markers:
        assert marker not in text


def test_keys_fractional_float_vs_int_column_refused() -> None:
    kf = _kf(id=pl.Series([31337, 31338], dtype=pl.Int64))
    _assert_keys_refused(kf, (_CANARY_FLOAT,), markers=("31337.4242", "31337.42", "31337"))


def test_keys_integral_float_vs_int_column_selects_exact_row() -> None:
    # 31337.0 converts exactly to Int64 31337, so it is accepted.
    kf = _kf(id=pl.Series([31337, 31338], dtype=pl.Int64))
    assert _keys_rows(kf, (31337.0,)) == {31337}


def test_keys_out_of_range_int_vs_narrow_int_column_refused() -> None:
    kf = _kf(id=pl.Series([1, 2], dtype=pl.Int32))
    _assert_keys_refused(kf, (_CANARY_INT,), markers=(str(_CANARY_INT),))


def test_keys_bool_vs_int_column_refused() -> None:
    kf = _kf(id=pl.Series([0, 1], dtype=pl.Int64))
    _assert_keys_refused(kf, (True,), markers=("True",))


def test_keys_string_number_vs_int_column_refused() -> None:
    # "007" and "7" would both parse to 7: a str -> int parse is not injective.
    kf = _kf(id=pl.Series([7, 8], dtype=pl.Int64))
    _assert_keys_refused(kf, ("007",), markers=("007",))
    _assert_keys_refused(kf, (_CANARY_STR,), markers=(_CANARY_STR,))


def test_keys_float_vs_float_column_exact_match_only() -> None:
    kf = _kf(id=pl.Series([0.5, 1.25], dtype=pl.Float64))
    assert _keys_rows(kf, (0.5,)) == {0.5}
    assert _keys_rows(kf, (0.5000001,)) == set()
    # 0.1 has no exact Float32 representation.
    kf32 = _kf(id=pl.Series([0.5, 1.25], dtype=pl.Float32))
    assert _keys_rows(kf32, (1.25,)) == {1.25}
    _assert_keys_refused(kf32, (0.1,), markers=("0.1",))


def test_keys_int_vs_float_column_beyond_2_53_refused() -> None:
    kf = _kf(id=pl.Series([1.0, 2.0], dtype=pl.Float64))
    assert _keys_rows(kf, (1,)) == {1.0}
    _assert_keys_refused(kf, (2**53 + 1,), markers=(str(2**53 + 1),))


def test_keys_timezone_mismatch_refused() -> None:
    import datetime as dt

    naive = _kf(id=pl.Series([dt.datetime(2020, 1, 1)], dtype=pl.Datetime("us")))
    aware_key = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    _assert_keys_refused(naive, (aware_key,), markers=("2020",))
    aware = _kf(id=pl.Series([aware_key], dtype=pl.Datetime("us", "UTC")))
    _assert_keys_refused(aware, (dt.datetime(2020, 1, 1),), markers=("2020",))
    assert _keys_rows(aware, (aware_key,)) == {aware_key}


def test_keys_mixed_python_types_in_one_column_refused() -> None:
    kf = _kf(id=pl.Series([1, 2], dtype=pl.Int64))
    _assert_keys_refused(kf, (1,), (_CANARY_STR,), markers=(_CANARY_STR,))


def test_keys_wrong_type_message_is_value_free() -> None:
    kf = _kf(id=pl.Series([1, 2], dtype=pl.Int32))
    _assert_keys_refused(kf, (_CANARY_STR,), markers=(_CANARY_STR,))


def test_sample_uncanonicalizable_message_is_value_free() -> None:
    kf = _kf(id=[_CANARY_FLOAT, 2.0])
    spec = SeedSpec(table="t", mode="sample", key_columns=("id",), count=1)
    with pytest.raises(SubsetConfigError) as excinfo:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert excinfo.value.code == "subset_seed_key_uncanonicalizable"
    text = str(excinfo.value)
    assert "31337" not in text and "'t'" in text

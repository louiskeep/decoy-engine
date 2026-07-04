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

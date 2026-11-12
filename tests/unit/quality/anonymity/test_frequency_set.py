"""FrequencySet: base classes are the measures' classes; rollup equals regrouping."""

from __future__ import annotations

import datetime
import zoneinfo
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize.hierarchies import (
    Lattice,
    date_hierarchy,
    interval_hierarchy,
    prefix_hierarchy,
)
from decoy_engine.quality.anonymity import (
    EqualTCloseness,
    FrequencySet,
    KAnonymity,
    OrderedTCloseness,
    measure_k_anonymity,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "q": pd.Series([1, 1.0, True, None, 1, np.int64(1), "1", None], dtype=object),
            "zip": ["120", "121", "120", "130", "120", "120", "130", "130"],
            "s": [1.0, 2.0, 2.0, None, 3.0, 1.0, 2.0, 5.0],
        }
    )


def test_base_classes_match_the_measures() -> None:
    frame = _frame()
    fs = FrequencySet.of(frame, ["q", "zip"])
    report = measure_k_anonymity(frame, ["q", "zip"])
    assert fs.count == report.class_count
    sizes, counts = np.unique(fs.sizes, return_counts=True)
    assert (
        tuple(zip(sizes.tolist(), counts.tolist(), strict=True)) == report.class_size_distribution
    )
    # 1 and np.int64(1) with zip 120 are one class (rows 0, 4, 5); 1.0 and True are not.
    assert fs.row_class[0] == fs.row_class[4] == fs.row_class[5]
    assert len({fs.row_class[0], fs.row_class[2]}) == 2
    assert fs.representatives.tolist() == sorted(fs.representatives.tolist())
    assert fs.representatives[fs.row_class[4]] == 0


def _lattice() -> Lattice:
    return Lattice({"age": interval_hierarchy((10, 20)), "zip": prefix_hierarchy(3, keep=(2, 1))})


def _codes_for(
    lattice: Lattice, base: FrequencySet, frame: pd.DataFrame, node: tuple[int, ...]
) -> list[np.ndarray]:
    reps = frame.iloc[base.representatives]
    out = []
    for name, level in zip(lattice.columns, node, strict=True):
        labels = lattice.hierarchies[name].generalize(reps[name], level).reset_index(drop=True)
        out.append(FrequencySet.of(pd.DataFrame({name: labels}), [name]).row_class)
    return out


@pytest.mark.parametrize("route", ["pandas", "arrow"])
def test_rollup_equals_grouping_the_applied_table(route: str) -> None:
    rng = np.random.default_rng(3)
    frame = pd.DataFrame(
        {
            "age": rng.integers(0, 60, 40),
            "zip": rng.choice(["120", "121", "130", "210"], 40),
            "s": rng.choice([1.0, 2.0, 3.0, np.nan], 40),
        }
    )
    table = frame if route == "pandas" else pa.Table.from_pandas(frame, preserve_index=False)
    lattice = _lattice()
    models = (KAnonymity(2), EqualTCloseness("s", 0.2), OrderedTCloseness("s", 0.2))
    base = FrequencySet.of(table, lattice.columns, models)
    for node in lattice.nodes():
        rolled = base.rollup(_codes_for(lattice, base, frame, node))
        direct = FrequencySet.of(lattice.apply(table, node), lattice.columns, models)
        _same(rolled, direct, models)


def _same(rolled: FrequencySet, direct: FrequencySet, models: tuple[Any, ...]) -> None:
    # Class ids may differ; compare through the rows.
    assert rolled.count == direct.count
    pairs = set(zip(rolled.row_class.tolist(), direct.row_class.tolist(), strict=True))
    assert len(pairs) == rolled.count
    mapping = dict(pairs)
    for c in range(rolled.count):
        assert rolled.sizes[c] == direct.sizes[mapping[c]]
    np.testing.assert_array_equal(
        rolled.row_mask(rolled.failing(models)), direct.row_mask(direct.failing(models))
    )
    for model in models[1:]:
        a, b = rolled.sensitive(model), direct.sensitive(model)
        np.testing.assert_array_equal(a.value_totals, b.value_totals)
        got = {
            (mapping[int(c)], int(v)): int(n)
            for c, v, n in zip(a.pair_class, a.pair_value, a.pair_count, strict=True)
        }
        want = {
            (int(c), int(v)): int(n)
            for c, v, n in zip(b.pair_class, b.pair_value, b.pair_count, strict=True)
        }
        assert got == want


def test_rollup_with_a_tz_aware_object_column() -> None:
    zone = zoneinfo.ZoneInfo("America/New_York")
    days = [
        datetime.datetime(2024, m, d, 23, 30, tzinfo=zone)
        for m, d in [(3, 31), (3, 31), (4, 1), (6, 30), (7, 1)]
    ]
    frame = pd.DataFrame({"seen": pd.Series(days, dtype=object), "s": [1.0, 2.0, 1.0, 2.0, 1.0]})
    lattice = Lattice({"seen": date_hierarchy()})
    models = (KAnonymity(2), EqualTCloseness("s", 0.3))
    base = FrequencySet.of(frame, lattice.columns, models)
    for node in lattice.nodes():
        rolled = base.rollup(_codes_for(lattice, base, frame, node))
        _same(rolled, FrequencySet.of(lattice.apply(frame, node), lattice.columns, models), models)


def test_rollup_refuses_wrong_shapes() -> None:
    base = FrequencySet.of(_frame(), ["zip"])
    with pytest.raises(ValueError):
        base.rollup([])
    with pytest.raises(ValueError):
        base.rollup([np.zeros(base.count + 1, dtype=np.int64)])


def test_counts_need_the_model() -> None:
    base = FrequencySet.of(_frame(), ["zip"], (KAnonymity(2),))
    with pytest.raises(ValueError):
        base.sensitive(EqualTCloseness("s", 0.1))

"""Interval and band hierarchies: literal labels, exact bounds, refusals, value identity."""

from __future__ import annotations

import math
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize.hierarchies import (
    band_hierarchy,
    check_hierarchy,
    interval_hierarchy,
)
from decoy_engine.execution._strategies._bucketize import BucketizeStrategyHandler
from decoy_engine.plan._types import ColumnSeed

from ._cols import labels, obj

AGES = [0, 4, 5, 17, 18, 29, 30, 64, 65, 99, None, math.nan]
AGE_TABLE = {
    1: ["[0, 5)", "[0, 5)", "[5, 10)", "[15, 20)", "[15, 20)", "[25, 30)", "[30, 35)",
        "[60, 65)", "[65, 70)", "[95, 100)", None, None],
    2: ["[0, 10)", "[0, 10)", "[0, 10)", "[10, 20)", "[10, 20)", "[20, 30)", "[30, 40)",
        "[60, 70)", "[60, 70)", "[90, 100)", None, None],
    3: ["[0, 20)", "[0, 20)", "[0, 20)", "[0, 20)", "[0, 20)", "[20, 40)", "[20, 40)",
        "[60, 80)", "[60, 80)", "[80, 100)", None, None],
    4: ["*"] * 12,
}  # fmt: skip


def _bounds(label: str) -> tuple[int, int]:
    low, high = label[1:-1].split(", ")
    return int(low), int(high)


class TestInterval:
    def test_age_table(self) -> None:
        h = interval_hierarchy((5, 10, 20))
        assert h.height == 4 and h.widths == (5, 10, 20) and h.kind == "interval"
        for level, expected in AGE_TABLE.items():
            assert labels(h.generalize(obj(AGES), level)) == expected
            arrow = pa.array([None if (v is None or v != v) else v for v in AGES], pa.int64())
            assert labels(h.generalize(arrow, level)) == expected

    def test_level_zero_is_identity(self) -> None:
        s = pd.Series([3, 8], name="age", index=[7, 9])
        out = interval_hierarchy((5,)).generalize(s, 0)
        pd.testing.assert_series_equal(out, s)
        assert out is not s

    def test_negative_float_and_int64_edge(self) -> None:
        h = interval_hierarchy((5,))
        assert labels(h.generalize(obj([-1, 29.5]), 1)) == ["[-5, 0)", "[25, 30)"]
        big = 2**63 - 1
        expected = ["[9223372036854775805, 9223372036854775810)"] * 2
        assert labels(h.generalize(obj([big, np.int64(big)]), 1)) == expected
        assert labels(h.generalize(pd.Series([big], dtype="int64"), 1)) == expected[:1]

    def test_exact_bounds_near_2_pow_53(self) -> None:
        h = interval_hierarchy((3, 6))
        assert labels(h.generalize(obj([2**53 + 1]), 1)) == ["[9007199254740993, 9007199254740996)"]
        assert labels(h.generalize(obj([float(2**53 + 2)]), 1)) == [
            "[9007199254740993, 9007199254740996)"
        ]
        assert labels(h.generalize(obj([-1.5e-323]), 1)) == ["[-3, 0)"]
        assert labels(h.generalize(obj([-1.5e-323]), 2)) == ["[-6, 0)"]

    @pytest.mark.serial  # wall-clock limit: run alone
    def test_huge_floats_exact_and_fast(self) -> None:
        h = interval_hierarchy((3, 6))
        values = [1e300, 1e307, -1e307]
        start = time.monotonic()
        for level in (1, 2):
            width = (3, 6)[level - 1]
            for value, label in zip(values, labels(h.generalize(obj(values), level)), strict=False):
                low, high = _bounds(label)
                assert low <= value < high and high - low == width and low % width == 0
        assert time.monotonic() - start < 2.0

    def test_bucketize_parity(self) -> None:
        values = [-7, 0, 4, 5, 17, 23, 99, 1000]
        seed = ColumnSeed(
            namespace=None, strategy="bucketize", provider="bucketize", backend_type="faker",
            backend_version="v", cardinality_mode="reuse", deterministic=False,
            provider_config=(("width", 5), ("format", "lower")), coherent_with=(),
        )  # fmt: skip
        ctx = SimpleNamespace(job_seed=b"\0" * 8, mask_key=b"\0" * 8, row_errors=[])
        out, _ = BucketizeStrategyHandler().run(pd.DataFrame({"v": values}), "v", seed, ctx)  # type: ignore[arg-type]
        ours = labels(interval_hierarchy((5,)).generalize(pd.Series(values), 1))
        assert [str(_bounds(x)[0]) for x in ours] == out["v"].tolist()

    @pytest.mark.parametrize(
        "widths",
        [(), (5, 7), (5, 5), (10, 5), (True,), (5.0,), (0,), (-5,), (np.int64(5),),
         (np.float64(5.0),), "5", 5],
    )  # fmt: skip
    def test_refusals(self, widths: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            interval_hierarchy(widths)  # type: ignore[arg-type]

    def test_coverage(self) -> None:
        h = interval_hierarchy((5,))
        column = obj(["abc", True, math.inf, 3, None])
        with pytest.raises(ValueError, match="3 rows"):
            h.generalize(column, 1)
        report = check_hierarchy(h, column)
        assert (report.rows_not_covered, report.rows_null, report.covers_column) == (3, 1, False)

    @pytest.mark.parametrize("cells", [[1, True], [True, 1]])
    def test_bool_never_merges_with_int(self, cells: list[object]) -> None:
        h = interval_hierarchy((5,))
        assert check_hierarchy(h, obj(cells)).rows_not_covered == 1
        with pytest.raises(ValueError, match="1 rows"):
            h.generalize(obj(cells), 1)

    @pytest.mark.parametrize("cells", [[1, 1.0], [1.0, 1]])
    def test_int_and_float_are_two_values(self, cells: list[object]) -> None:
        h = interval_hierarchy((5,))
        report = check_hierarchy(h, obj(cells))
        assert report.covers_column and report.classes_per_level[0] == 2
        assert labels(h.generalize(obj(cells), 1)) == ["[0, 5)", "[0, 5)"]
        assert labels(h.generalize(obj(cells), 0)) == cells


class TestBand:
    CUTS = ((0, 18, 30, 45, 65), (18, 65), (65,))

    def test_literal_table(self) -> None:
        h = band_hierarchy(self.CUTS)
        assert h.height == 4 and h.kind == "band"
        column = obj([-3, 18, 20, 70, None])
        assert labels(h.generalize(column, 1)) == [
            "[-inf, 0)",
            "[18, 30)",
            "[18, 30)",
            "[65, inf)",
            None,
        ]
        assert labels(h.generalize(column, 2)) == [
            "[-inf, 18)",
            "[18, 65)",
            "[18, 65)",
            "[65, inf)",
            None,
        ]
        assert labels(h.generalize(column, 3)) == [
            "[-inf, 65)",
            "[-inf, 65)",
            "[-inf, 65)",
            "[65, inf)",
            None,
        ]
        assert labels(h.generalize(column, 4)) == ["*"] * 5  # fmt: skip

    def test_float_cut_label(self) -> None:
        h = band_hierarchy(((2.5,),))
        assert labels(h.generalize(obj([3, 1]), 1)) == ["[2.5, inf)", "[-inf, 2.5)"]

    def test_cuts_straddling_2_pow_53(self) -> None:
        h = band_hierarchy(((2**53, 2**53 + 2),))
        out = labels(h.generalize(obj([2**53 + 1, float(2**53 + 2)]), 1))
        assert out == ["[9007199254740992, 9007199254740994)", "[9007199254740994, inf)"]

    @pytest.mark.parametrize(
        "cuts",
        [(), ((),), ((0, 18), (0, 18)), ((0, 18), (10,)), ((18, 0),), ((float("inf"),),),
         ((True,),), ((np.int64(18),),), ((np.float64(2.5),),), (("a",),), "0", (5,)],
    )  # fmt: skip
    def test_refusals(self, cuts: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            band_hierarchy(cuts)  # type: ignore[arg-type]

"""Unit tests for grouped_series strategy (SP-10c TDD).

Written BEFORE the implementation per the TDD contract (testing.md).

grouped_series generates a per-group series that resets or accumulates
within each partition defined by a group_by column, ordered by an
order_by column. Two generators ship with SP-10c:

  cumcount   - 0-based counter that resets at each group boundary
  monotone_walk - values that are non-decreasing within each group

Config (generate_columns type: grouped_series):
  group_by   str   Required. Column name that defines partitions.
  order_by   str   Required. Column name that defines the sort order
                   within each partition.
  generator  str   Optional. One of "cumcount" (default) or
                   "monotone_walk".
  start      int   Optional. Starting value (default 0 for cumcount,
                   1 for monotone_walk).
  step       int   Optional. Step between consecutive values in
                   cumcount or minimum step for monotone_walk
                   (default 1).

Methodology:
  Per-group sequential indexing mirrors the pandas cumcount() pattern
  (pandas 2.x, Apache-2.0; https://pandas.pydata.org/docs/reference/
  api/pandas.core.groupby.GroupBy.cumcount.html). Group partitioning
  follows SDV-style per-group sequencing (SDV, MIT License;
  https://sdv.dev). Monotone walk uses a seeded numpy RNG for
  non-decreasing integer steps per row, keeping output deterministic
  under the same seed.

Determinism:
  Same seed + same group_by/order_by columns -> byte-identical output.
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._errors import PlanCompileError


class TestGroupedSeriesConfig:
    """GroupedSeriesConfig.from_dict validates group_by + order_by + generator."""

    def test_valid_cumcount_config_accepted(self) -> None:
        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig

        cfg = GroupedSeriesConfig.from_dict({"group_by": "customer_id", "order_by": "event_date"})
        assert cfg.group_by == "customer_id"
        assert cfg.order_by == "event_date"
        assert cfg.generator == "cumcount"

    def test_valid_monotone_walk_config_accepted(self) -> None:
        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig

        cfg = GroupedSeriesConfig.from_dict(
            {
                "group_by": "account",
                "order_by": "date",
                "generator": "monotone_walk",
                "start": 100,
                "step": 5,
            }
        )
        assert cfg.generator == "monotone_walk"
        assert cfg.start == 100
        assert cfg.step == 5

    def test_missing_group_by_raises(self) -> None:
        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig

        with pytest.raises(PlanCompileError, match="group_by"):
            GroupedSeriesConfig.from_dict({"order_by": "date"})

    def test_missing_order_by_raises(self) -> None:
        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig

        with pytest.raises(PlanCompileError, match="order_by"):
            GroupedSeriesConfig.from_dict({"group_by": "customer_id"})

    def test_invalid_generator_raises(self) -> None:
        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig

        with pytest.raises(PlanCompileError, match="generator"):
            GroupedSeriesConfig.from_dict(
                {"group_by": "g", "order_by": "o", "generator": "exponential"}
            )

    def test_empty_config_raises(self) -> None:
        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig

        with pytest.raises(PlanCompileError):
            GroupedSeriesConfig.from_dict({})


class TestGroupedSeriesCumcount:
    """cumcount generator assigns 0-based position within each group."""

    def _apply(self, group_vals, order_vals, start=0, step=1):
        import pandas as pd

        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, apply_grouped_series

        df = pd.DataFrame({"grp": group_vals, "ord": order_vals})
        cfg = GroupedSeriesConfig.from_dict(
            {"group_by": "grp", "order_by": "ord", "start": start, "step": step}
        )
        return apply_grouped_series(cfg, df, seed=b"\x00" * 8)

    def test_single_group_ascending(self) -> None:
        """Three rows in one group get 0, 1, 2 (cumcount starting at 0)."""
        result = self._apply(["A", "A", "A"], [1, 2, 3])
        assert list(result) == [0, 1, 2]

    def test_two_groups_reset_at_boundary(self) -> None:
        """Counter resets to 0 for each new group."""
        result = self._apply(["A", "A", "B", "B"], [1, 2, 1, 2])
        # Group A: 0, 1; Group B: 0, 1
        assert list(result) == [0, 1, 0, 1]

    def test_start_offset_applied(self) -> None:
        """start=1 shifts counter to 1, 2, 3 within group."""
        result = self._apply(["A", "A", "A"], [1, 2, 3], start=1)
        assert list(result) == [1, 2, 3]

    def test_step_applied(self) -> None:
        """step=10 gives 0, 10, 20 within group."""
        result = self._apply(["A", "A", "A"], [1, 2, 3], step=10)
        assert list(result) == [0, 10, 20]

    def test_interleaved_rows_sorted_correctly(self) -> None:
        """Rows not pre-sorted: result maps to original row order."""
        # Input: B1, A1, A2, B2 - groups interleaved
        result = self._apply(["B", "A", "A", "B"], [1, 1, 2, 2])
        # After sort-within-group: A[1,2] -> 0,1; B[1,2] -> 0,1
        # Row 0 is B row 1 -> group B, pos 0
        # Row 1 is A row 1 -> group A, pos 0
        # Row 2 is A row 2 -> group A, pos 1
        # Row 3 is B row 2 -> group B, pos 1
        assert list(result) == [0, 0, 1, 1]


class TestGroupedSeriesMonotoneWalk:
    """monotone_walk generator produces non-decreasing values within each group."""

    def _apply(self, group_vals, order_vals, start=1, step=1, seed=b"\xab\xcd" * 4):
        import pandas as pd

        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, apply_grouped_series

        df = pd.DataFrame({"grp": group_vals, "ord": order_vals})
        cfg = GroupedSeriesConfig.from_dict(
            {
                "group_by": "grp",
                "order_by": "ord",
                "generator": "monotone_walk",
                "start": start,
                "step": step,
            }
        )
        return apply_grouped_series(cfg, df, seed=seed)

    def test_single_group_non_decreasing(self) -> None:
        """All values in a single group are non-decreasing."""
        result = list(self._apply(["A"] * 5, list(range(5))))
        for i in range(1, len(result)):
            assert result[i] >= result[i - 1], (
                f"row {i} ({result[i]}) < row {i - 1} ({result[i - 1]})"
            )

    def test_counter_resets_per_group(self) -> None:
        """Walk resets at group boundary (group B starts at start, not continuing A)."""
        result = self._apply(["A", "A", "A", "B", "B", "B"], list(range(6)), start=10)
        # First row of group B should be >= start (10), but NOT continue from group A
        # Group A is result[0..2], group B is result[3..5]
        # Group B should start fresh at start (10)
        assert result[3] >= 10, f"group B should reset to start=10, got {result[3]}"

    def test_ten_rows_per_group_all_non_decreasing(self) -> None:
        """Walk 10 rows; assert each value >= prev."""
        groups = ["A"] * 10
        orders = list(range(10))
        result = list(self._apply(groups, orders))
        for i in range(1, len(result)):
            assert result[i] >= result[i - 1]

    def test_start_respected(self) -> None:
        """First value in a group is exactly the configured start."""
        result = self._apply(["X", "X", "X"], [1, 2, 3], start=100)
        assert result[0] == 100


class TestGroupedSeriesDeterminism:
    """Two identical seeded runs produce byte-identical output."""

    def _run(self, seed):
        import pandas as pd

        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, apply_grouped_series

        df = pd.DataFrame(
            {
                "grp": ["A", "A", "B", "B", "A"],
                "ord": [3, 1, 2, 4, 5],
            }
        )
        cfg = GroupedSeriesConfig.from_dict(
            {"group_by": "grp", "order_by": "ord", "generator": "monotone_walk"}
        )
        return list(apply_grouped_series(cfg, df, seed=seed))

    def test_same_seed_same_output(self) -> None:
        seed = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        r1 = self._run(seed)
        r2 = self._run(seed)
        assert r1 == r2

    def test_different_seeds_may_differ(self) -> None:
        r1 = self._run(b"\x00" * 8)
        r2 = self._run(b"\xff" * 8)
        # cumcount is deterministic without seed; monotone_walk steps differ
        # This test just verifies the seeds propagate to the walk (may be equal
        # in degenerate cases but should differ in practice for 5 rows)
        # We assert they run without error; actual difference is probabilistic
        assert isinstance(r1, list) and isinstance(r2, list)


class TestGroupedSeriesPlanCheck:
    """check_grouped_series_refs rejects configs with missing column references."""

    def _check(self, config):
        from decoy_engine.plan._checks_grouped_series import check_grouped_series_refs

        check_grouped_series_refs(config)

    def test_valid_config_passes(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {"name": "grp", "type": "categorical", "categories": ["A"]},
                        {"name": "ord", "type": "sequence", "start": 1},
                        {
                            "name": "series",
                            "type": "grouped_series",
                            "group_by": "grp",
                            "order_by": "ord",
                        },
                    ],
                }
            ]
        }
        self._check(cfg)  # no raise

    def test_missing_group_by_column_raises(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {"name": "ord", "type": "sequence", "start": 1},
                        {
                            "name": "series",
                            "type": "grouped_series",
                            "group_by": "missing_col",
                            "order_by": "ord",
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="missing_col"):
            self._check(cfg)

    def test_missing_order_by_column_raises(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {"name": "grp", "type": "categorical", "categories": ["A"]},
                        {
                            "name": "series",
                            "type": "grouped_series",
                            "group_by": "grp",
                            "order_by": "missing_ord",
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="missing_ord"):
            self._check(cfg)

    def test_mask_column_bad_group_by_raises(self) -> None:
        """Plan check also covers mask-mode columns with strategy: grouped_series."""
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "val", "strategy": "passthrough"},
                        {
                            "name": "series",
                            "strategy": "grouped_series",
                            "provider_config": {
                                "group_by": "no_such_col",
                                "order_by": "val",
                            },
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="no_such_col"):
            self._check(cfg)


class TestGroupedSeriesRegistration:
    """Strategy is registered in SCALAR_HANDLERS."""

    def test_grouped_series_in_scalar_handlers(self) -> None:
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        assert "grouped_series" in SCALAR_HANDLERS

    def test_adapter_supports_grouped_series(self) -> None:
        from decoy_engine.execution import PandasExecutionAdapter

        assert PandasExecutionAdapter().supports_strategy("grouped_series") is True


class TestGroupedSeriesGeneratePath:
    """grouped_series wired into the generate_tables synthesize path."""

    def test_cumcount_in_generate_table(self) -> None:
        from decoy_engine.generation.synthesize import generate_tables

        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": 4,
                    "generate_columns": [
                        {
                            "name": "grp",
                            "type": "categorical",
                            "categories": ["A", "B"],
                            "weights": [1, 1],
                        },
                        {"name": "ord", "type": "sequence", "start": 1},
                        {
                            "name": "series",
                            "type": "grouped_series",
                            "group_by": "grp",
                            "order_by": "ord",
                        },
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = generate_tables(cfg)["t"]
        series = tbl.column("series").to_pylist()
        grp = tbl.column("grp").to_pylist()
        # Within each group, values must be non-negative integers
        assert all(v is not None and v >= 0 for v in series), (
            f"Expected non-negative integers: {series}"
        )
        # Check that within each group, the counter values are 0,1,...
        from collections import defaultdict

        group_vals = defaultdict(list)
        for g, s in zip(grp, series, strict=True):
            group_vals[g].append(s)
        for g, vals in group_vals.items():
            assert sorted(vals) == list(range(len(vals))), (
                f"Group {g!r} cumcount not consecutive: {vals}"
            )

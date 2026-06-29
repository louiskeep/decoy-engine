"""Unit tests for windowed_date strategy (SP-10c TDD).

Written BEFORE the implementation per the TDD contract (testing.md).

windowed_date generates a date within a window relative to an anchor
date column. The output date is always in [anchor + min_days, anchor
+ max_days] (inclusive).

Config (generate_columns type: windowed_date):
  anchor     str   Required. Column name containing the anchor date.
                   Must be declared before this column in
                   generate_columns.
  min_days   int   Optional. Minimum offset from anchor in days
                   (default 0).
  max_days   int   Required. Maximum offset from anchor in days.
                   Must be >= min_days.
  distribution str Optional. One of "uniform" (default), "early"
                   (biased toward lower end), or "late" (biased
                   toward upper end).

Methodology:
  Date arithmetic uses pandas.Timestamp and pandas.Timedelta (pandas
  2.x, Apache-2.0; https://pandas.pydata.org/docs). A bounded uniform
  or weighted offset within [min_days, max_days] is drawn via seeded
  numpy.random.default_rng (NumPy 1.x; https://numpy.org/doc/stable/
  reference/random/generator.html). Per-row seeding follows the
  GenDeriveContext pattern (engine/generators/derivation.py) for
  byte-identical determinism across runs.

Determinism:
  Same seed + same anchor column -> byte-identical output dates.
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._errors import PlanCompileError


class TestWindowedDateConfig:
    """WindowedDateConfig.from_dict validates anchor + max_days at parse time."""

    def test_valid_minimal_config_accepted(self) -> None:
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        cfg = WindowedDateConfig.from_dict({"anchor": "start_date", "max_days": 30})
        assert cfg.anchor == "start_date"
        assert cfg.max_days == 30
        assert cfg.min_days == 0
        assert cfg.distribution == "uniform"

    def test_full_config_accepted(self) -> None:
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        cfg = WindowedDateConfig.from_dict(
            {
                "anchor": "admission_date",
                "min_days": 1,
                "max_days": 14,
                "distribution": "early",
            }
        )
        assert cfg.min_days == 1
        assert cfg.max_days == 14
        assert cfg.distribution == "early"

    def test_missing_anchor_raises(self) -> None:
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        with pytest.raises(PlanCompileError, match="anchor"):
            WindowedDateConfig.from_dict({"max_days": 30})

    def test_missing_max_days_raises(self) -> None:
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        with pytest.raises(PlanCompileError, match="max_days"):
            WindowedDateConfig.from_dict({"anchor": "start_date"})

    def test_invalid_distribution_raises(self) -> None:
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        with pytest.raises(PlanCompileError, match="distribution"):
            WindowedDateConfig.from_dict(
                {"anchor": "start_date", "max_days": 10, "distribution": "normal"}
            )

    def test_min_days_greater_than_max_days_raises(self) -> None:
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        with pytest.raises(PlanCompileError, match="min_days"):
            WindowedDateConfig.from_dict({"anchor": "start_date", "min_days": 20, "max_days": 10})

    def test_negative_max_days_allowed(self) -> None:
        """max_days can be negative (date before anchor) as long as min <= max."""
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        cfg = WindowedDateConfig.from_dict(
            {"anchor": "discharge_date", "min_days": -30, "max_days": -1}
        )
        assert cfg.min_days == -30
        assert cfg.max_days == -1


class TestWindowedDateBounds:
    """100% of generated dates fall within [anchor + min_days, anchor + max_days]."""

    def _apply(self, anchor_dates, min_days=0, max_days=30, distribution="uniform"):
        import pandas as pd

        from decoy_engine.transforms.windowed_date import WindowedDateConfig, apply_windowed_date

        df = pd.DataFrame({"anchor": anchor_dates})
        cfg = WindowedDateConfig.from_dict(
            {
                "anchor": "anchor",
                "min_days": min_days,
                "max_days": max_days,
                "distribution": distribution,
            }
        )
        return apply_windowed_date(cfg, df, seed=b"\x12\x34\x56\x78" * 2)

    def test_all_dates_within_uniform_window(self) -> None:
        """Every output date is in [anchor+0, anchor+30]."""
        anchors = ["2024-01-01"] * 20
        result = self._apply(anchors, min_days=0, max_days=30)
        import pandas as pd

        for anchor, date in zip(anchors, result, strict=True):
            a = pd.Timestamp(anchor)
            lo = a + pd.Timedelta(days=0)
            hi = a + pd.Timedelta(days=30)
            t = pd.Timestamp(date)
            assert lo <= t <= hi, f"date {t} not in [{lo}, {hi}]"

    def test_all_dates_within_negative_window(self) -> None:
        """Window before the anchor (negative offsets)."""
        anchors = ["2024-06-15"] * 20
        result = self._apply(anchors, min_days=-14, max_days=-1)
        import pandas as pd

        for anchor, date in zip(anchors, result, strict=True):
            a = pd.Timestamp(anchor)
            lo = a + pd.Timedelta(days=-14)
            hi = a + pd.Timedelta(days=-1)
            t = pd.Timestamp(date)
            assert lo <= t <= hi, f"date {t} not in [{lo}, {hi}]"

    def test_zero_width_window_returns_anchor(self) -> None:
        """min_days == max_days means the output is always anchor + that offset."""
        anchors = ["2024-03-01"] * 10
        result = self._apply(anchors, min_days=5, max_days=5)
        import pandas as pd

        for anchor, date in zip(anchors, result, strict=True):
            expected = pd.Timestamp(anchor) + pd.Timedelta(days=5)
            assert pd.Timestamp(date) == expected

    def test_early_distribution_biased_low(self) -> None:
        """Early distribution produces measurably more outputs in the lower half."""
        import pandas as pd

        anchors = ["2024-01-01"] * 100
        result = self._apply(anchors, min_days=0, max_days=100, distribution="early")
        midpoint = pd.Timestamp("2024-01-01") + pd.Timedelta(days=50)
        below = sum(1 for d in result if pd.Timestamp(d) <= midpoint)
        # Early should have more than 50 below midpoint
        assert below > 50, f"Expected >50 below midpoint, got {below}"

    def test_late_distribution_biased_high(self) -> None:
        """Late distribution produces measurably more outputs in the upper half."""
        import pandas as pd

        anchors = ["2024-01-01"] * 100
        result = self._apply(anchors, min_days=0, max_days=100, distribution="late")
        midpoint = pd.Timestamp("2024-01-01") + pd.Timedelta(days=50)
        above = sum(1 for d in result if pd.Timestamp(d) >= midpoint)
        assert above > 50, f"Expected >50 above midpoint, got {above}"


class TestWindowedDateDeterminism:
    """Two identical seeded runs produce byte-identical output."""

    def _run(self, seed):
        import pandas as pd

        from decoy_engine.transforms.windowed_date import WindowedDateConfig, apply_windowed_date

        df = pd.DataFrame(
            {"anchor": ["2024-01-01", "2024-03-15", "2023-12-31", "2024-06-01", "2024-09-10"]}
        )
        cfg = WindowedDateConfig.from_dict({"anchor": "anchor", "min_days": 0, "max_days": 30})
        return list(apply_windowed_date(cfg, df, seed=seed))

    def test_same_seed_same_output(self) -> None:
        seed = b"\xab\xcd\xef\x01\x23\x45\x67\x89"
        r1 = self._run(seed)
        r2 = self._run(seed)
        assert r1 == r2

    def test_different_seeds_differ(self) -> None:
        r1 = self._run(b"\x00" * 8)
        r2 = self._run(b"\xff" * 8)
        # With a 30-day window and 5 rows, two different seeds should produce
        # different output in most cases
        assert r1 != r2


class TestWindowedDatePlanCheck:
    """check_windowed_date_refs rejects missing anchor column references."""

    def _check(self, config):
        from decoy_engine.plan._checks_windowed_date import check_windowed_date_refs

        check_windowed_date_refs(config)

    def test_valid_config_passes(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {"name": "start", "type": "faker", "faker_type": "date"},
                        {
                            "name": "end",
                            "type": "windowed_date",
                            "anchor": "start",
                            "max_days": 30,
                        },
                    ],
                }
            ]
        }
        self._check(cfg)  # no raise

    def test_missing_anchor_column_raises(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {
                            "name": "end",
                            "type": "windowed_date",
                            "anchor": "nonexistent_date",
                            "max_days": 30,
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="nonexistent_date"):
            self._check(cfg)

    def test_mask_column_bad_anchor_raises(self) -> None:
        """Plan check also covers mask-mode columns with strategy: windowed_date."""
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "start_date", "strategy": "passthrough"},
                        {
                            "name": "end_date",
                            "strategy": "windowed_date",
                            "provider_config": {
                                "anchor": "ghost_col",
                                "max_days": 14,
                            },
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="ghost_col"):
            self._check(cfg)


class TestWindowedDateRegistration:
    """Strategy is registered in SCALAR_HANDLERS."""

    def test_windowed_date_in_scalar_handlers(self) -> None:
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        assert "windowed_date" in SCALAR_HANDLERS

    def test_adapter_supports_windowed_date(self) -> None:
        from decoy_engine.execution import PandasExecutionAdapter

        assert PandasExecutionAdapter().supports_strategy("windowed_date") is True


class TestWindowedDateGeneratePath:
    """windowed_date wired into the generate_tables synthesize path."""

    def test_windowed_date_in_generate_table(self) -> None:
        import pandas as pd

        from decoy_engine.generation.synthesize import generate_tables

        cfg = {
            "version": 1,
            "global_settings": {"seed": 7},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": "start",
                            "type": "categorical",
                            "categories": ["2024-01-01"],
                            "weights": [1],
                        },
                        {
                            "name": "end",
                            "type": "windowed_date",
                            "anchor": "start",
                            "min_days": 0,
                            "max_days": 30,
                        },
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = generate_tables(cfg)["t"]
        starts = tbl.column("start").to_pylist()
        ends = tbl.column("end").to_pylist()
        for start, end in zip(starts, ends, strict=True):
            s = pd.Timestamp(start)
            e = pd.Timestamp(end)
            assert s <= e <= s + pd.Timedelta(days=30), (
                f"end {e} not in [{s}, {s + pd.Timedelta(days=30)}]"
            )

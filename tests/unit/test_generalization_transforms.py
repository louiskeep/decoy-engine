"""Sprint A · Item 33 — generalization transforms.

Covers ``truncate`` (string-prefix generalization) and ``bucketize``
(numeric-bin generalization). Both are mask strategies built for HIPAA
Safe Harbor's geographic / age generalization rules; the tests assert
the deterministic shape (same input → same output, NULL passthrough,
invalid config falls back gracefully) plus the format-flag matrix
exhaustively because the Disguise YAMLs reference these by name."""

import pandas as pd
import pytest

from decoy_engine.transforms.truncate import TruncateStrategy
from decoy_engine.transforms.bucketize import BucketizeStrategy
from decoy_engine.transforms.factory import create_strategy


# ── truncate ─────────────────────────────────────────────────────────────────


def test_truncate_keeps_first_n(mock_logger):
    """Default behavior: first N chars. ZIP3 generalization is the
    motivating case ("97477" → "974")."""
    strat = TruncateStrategy(seed=0, logger=mock_logger)
    column = pd.Series(['97477', '95014', '10118'])
    out = strat.apply(column, {'column': 'zip', 'type': 'truncate', 'length': 3})
    assert list(out) == ['974', '950', '101']


def test_truncate_from_end(mock_logger):
    """`from_end: true` keeps the LAST N chars — useful for last-4 of a
    card / SSN. Distinct from the ZIP3 case but same primitive."""
    strat = TruncateStrategy(seed=0, logger=mock_logger)
    column = pd.Series(['1234567890123456', '9999888877776666'])
    out = strat.apply(
        column,
        {'column': 'card', 'type': 'truncate', 'length': 4, 'from_end': True},
    )
    assert list(out) == ['3456', '6666']


def test_truncate_passes_nulls(mock_logger):
    strat = TruncateStrategy(seed=0, logger=mock_logger)
    column = pd.Series(['abcdef', None, pd.NA])
    out = strat.apply(column, {'column': 'x', 'type': 'truncate', 'length': 2})
    assert out.iloc[0] == 'ab'
    assert pd.isna(out.iloc[1])
    assert pd.isna(out.iloc[2])


def test_truncate_short_input_unchanged(mock_logger):
    """If the input is shorter than `length`, return it whole — slicing
    short of available chars is a no-op in Python and matches the natural
    "keep up to N chars" mental model."""
    strat = TruncateStrategy(seed=0, logger=mock_logger)
    column = pd.Series(['ab', 'abc', 'abcde'])
    out = strat.apply(column, {'column': 'x', 'type': 'truncate', 'length': 4})
    assert list(out) == ['ab', 'abc', 'abcd']


def test_truncate_invalid_length_falls_back(mock_logger):
    strat = TruncateStrategy(seed=0, logger=mock_logger)
    column = pd.Series(['12345'])
    for bad in (None, 0, -3, 'three', True, 1.5):
        out = strat.apply(
            column, {'column': 'x', 'type': 'truncate', 'length': bad}
        )
        assert list(out) == ['12345'], (
            f"length={bad!r} should pass column through but got {list(out)}"
        )


def test_truncate_factory_path(mock_logger):
    strat = create_strategy('truncate', logger=mock_logger)
    assert isinstance(strat, TruncateStrategy)


# ── bucketize ────────────────────────────────────────────────────────────────


def test_bucketize_decade_lower(mock_logger):
    """`preset: by_decade` rounds to the lower bound of the decade."""
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([0, 7, 12, 25, 89, 90, 91])
    out = strat.apply(
        column, {'column': 'age', 'type': 'bucketize', 'preset': 'by_decade'}
    )
    assert list(out) == ['0', '0', '10', '20', '80', '90', '90']


def test_bucketize_5_years_range(mock_logger):
    """`by_5_years` + `format: range` shows the explicit bucket boundary."""
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([0, 7, 12, 24])
    out = strat.apply(
        column,
        {
            'column': 'age', 'type': 'bucketize',
            'preset': 'by_5_years', 'format': 'range',
        },
    )
    # int width 5: lower=0, upper_excl=5, inclusive upper = 4 → "0-4"
    assert list(out) == ['0-4', '5-9', '10-14', '20-24']


def test_bucketize_extended_presets(mock_logger):
    """New presets added by the 2026-05-20 audit cover common time-axis
    + currency-axis buckets. Verify each preset resolves to the documented
    width."""
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    cases = [
        # (preset, input, expected lower-bound output)
        ('by_year',         pd.Series([0, 1, 7, 12]),          ['0', '1', '7', '12']),
        ('by_2_years',      pd.Series([0, 1, 3, 7, 12]),       ['0', '0', '2', '6', '12']),
        ('by_century',      pd.Series([1, 99, 100, 250]),      ['0', '0', '100', '200']),
        ('by_thousand',     pd.Series([0, 999, 1000, 4500]),   ['0', '0', '1000', '4000']),
        ('by_ten_thousand', pd.Series([0, 9999, 10000, 75000]),
                            ['0', '0', '10000', '70000']),
    ]
    for preset, column, expected in cases:
        out = strat.apply(
            column, {'column': 'v', 'type': 'bucketize', 'preset': preset},
        )
        assert list(out) == expected, f"preset={preset!r} produced {list(out)}"


def test_bucketize_midpoint_format(mock_logger):
    """`format: midpoint` returns the bucket center. Convenient when
    downstream code expects a single representative number per bucket."""
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([0, 7, 12, 25])
    out = strat.apply(
        column,
        {
            'column': 'age', 'type': 'bucketize',
            'preset': 'by_decade', 'format': 'midpoint',
        },
    )
    # decade buckets: midpoints land at 5, 15, 25, … (truncated to int).
    assert list(out) == ['5', '5', '15', '25']


def test_bucketize_custom_width_float(mock_logger):
    """Float widths preserve precision — useful for monetary buckets."""
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([1.0, 1.4, 1.5, 1.9, 2.1])
    out = strat.apply(
        column, {'column': 'price', 'type': 'bucketize', 'width': 0.5}
    )
    # buckets: [1.0, 1.5), [1.5, 2.0), [2.0, 2.5)
    assert list(out) == ['1.0', '1.0', '1.5', '1.5', '2.0']


def test_bucketize_negative_values(mock_logger):
    """Negative inputs round toward more-negative buckets (math.floor),
    so the bucket containing -3 with width=10 is [-10, 0)."""
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([-3, -10, -11, 0])
    out = strat.apply(
        column, {'column': 'x', 'type': 'bucketize', 'width': 10}
    )
    assert list(out) == ['-10', '-10', '-20', '0']


def test_bucketize_passes_nulls_and_non_numeric(mock_logger):
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([1, None, 'not a number', 12])
    out = strat.apply(
        column, {'column': 'x', 'type': 'bucketize', 'width': 10}
    )
    assert out.iloc[0] == '0'
    assert pd.isna(out.iloc[1])
    assert out.iloc[2] == 'not a number'  # passthrough
    assert out.iloc[3] == '10'


def test_bucketize_invalid_config_falls_back(mock_logger):
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([10, 20, 30])
    for rule in (
        {'column': 'x', 'type': 'bucketize'},                           # neither preset nor width
        {'column': 'x', 'type': 'bucketize', 'width': 0},               # zero width
        {'column': 'x', 'type': 'bucketize', 'width': -5},              # negative width
        {'column': 'x', 'type': 'bucketize', 'width': 'ten'},           # non-numeric
        {'column': 'x', 'type': 'bucketize', 'preset': 'by_minute'},    # unknown preset
    ):
        out = strat.apply(column, rule)
        assert list(out) == [10, 20, 30], f"expected passthrough on {rule}"


def test_bucketize_unknown_format_warns_and_uses_lower(mock_logger):
    strat = BucketizeStrategy(seed=0, logger=mock_logger)
    column = pd.Series([7])
    out = strat.apply(
        column,
        {
            'column': 'x', 'type': 'bucketize',
            'preset': 'by_decade', 'format': 'square',
        },
    )
    assert list(out) == ['0']


def test_bucketize_factory_path(mock_logger):
    strat = create_strategy('bucketize', logger=mock_logger)
    assert isinstance(strat, BucketizeStrategy)

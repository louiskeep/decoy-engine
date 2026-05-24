# tests/unit/test_strategies.py
"""
Unit tests for masking strategies.
"""

import pandas as pd
import pytest

from decoy_engine.transforms.categorical import CategoricalStrategy
from decoy_engine.transforms.factory import create_strategy
from decoy_engine.transforms.faker_based import FakerStrategy
from decoy_engine.transforms.hash import HashStrategy
from decoy_engine.transforms.passthrough import PassthroughStrategy
from decoy_engine.transforms.redact import RedactStrategy
from decoy_engine.transforms.reference import ReferenceStrategy
from decoy_engine.transforms.registry import StrategyManager
from decoy_engine.transforms.shuffle import ShuffleStrategy


@pytest.fixture
def sample_data():
    """Create sample data for testing strategies."""
    return pd.Series(["John", "Jane", "Alice", "Bob", "Carol", None])


def test_faker_strategy(sample_data, mock_logger):
    """Test faker strategy."""
    # Initialize strategy
    strategy = FakerStrategy(seed=42, logger=mock_logger)

    # Apply masking
    rule = {"column": "name", "type": "faker", "faker_type": "first_name"}
    result = strategy.apply(sample_data, rule)

    # Assertions
    assert len(result) == len(sample_data)
    assert result.isna().sum() == 1  # Preserve NULL values
    assert result[0] != sample_data[0]  # Values should be changed

    # Test deterministic behavior with same seed
    strategy2 = FakerStrategy(seed=42, logger=mock_logger)
    result2 = strategy2.apply(sample_data, rule)
    pd.testing.assert_series_equal(result, result2)

    # Test different output with different seed
    strategy3 = FakerStrategy(seed=43, logger=mock_logger)
    result3 = strategy3.apply(sample_data, rule)
    assert not result.equals(result3)


def test_hash_strategy(sample_data, mock_logger):
    """Test hash strategy."""
    strategy = HashStrategy(seed=42, logger=mock_logger)

    rule = {"column": "name", "type": "hash"}
    result = strategy.apply(sample_data, rule)

    assert len(result) == len(sample_data)
    assert result.isna().sum() == 1  # Preserve NULL values
    assert all(
        isinstance(x, str) and len(x) == 64 for x in result.dropna()
    )  # SHA-256 produces 64 char hex


def test_hash_strategy_truncate(sample_data, mock_logger):
    """`truncate: N` clips the 64-char hex output to N chars while keeping
    the result deterministic — same input + same seed (or master key) gives
    the same prefix every time. Lets users target legacy CHAR(N) columns."""
    strategy = HashStrategy(seed=42, logger=mock_logger)

    full = strategy.apply(sample_data, {"column": "name", "type": "hash"})
    sliced = strategy.apply(sample_data, {"column": "name", "type": "hash", "truncate": 12})

    assert all(len(x) == 12 for x in sliced.dropna())
    # Slice is bitwise the prefix of the full hash — proves we're truncating
    # rather than re-hashing.
    for full_v, sliced_v in zip(full.dropna(), sliced.dropna(), strict=False):
        assert sliced_v == full_v[:12]


def test_hash_strategy_truncate_invalid_falls_back(sample_data, mock_logger):
    """Out-of-range or non-integer truncate values are warned about and
    treated as "no truncate" instead of raising — keeps the run alive on a
    single bad rule."""
    strategy = HashStrategy(seed=42, logger=mock_logger)

    for bad in (0, -1, 65, "twelve", 12.5):
        result = strategy.apply(sample_data, {"column": "name", "type": "hash", "truncate": bad})
        # 0 means "no truncate" by spec; everything else should also pass through.
        assert all(len(x) == 64 for x in result.dropna()), (
            f"expected fallback to full hash for truncate={bad!r}"
        )


def test_redact_strategy(sample_data, mock_logger):
    """Test redact strategy."""
    strategy = RedactStrategy(seed=42, logger=mock_logger)

    rule = {"column": "name", "type": "redact", "redact_with": "REDACTED"}
    result = strategy.apply(sample_data, rule)

    assert len(result) == len(sample_data)
    assert result.isna().sum() == 1  # Preserve NULL values
    assert all(x == "REDACTED" for x in result.dropna())


def test_categorical_strategy(sample_data, mock_logger, tmp_path, monkeypatch):
    """Categorical masking draws from configured values without local state."""
    monkeypatch.chdir(tmp_path)
    legacy_state_dir = tmp_path / "mappings"
    strategy = CategoricalStrategy(seed=42, logger=mock_logger)
    rule = {
        "column": "name",
        "type": "categorical",
        "categories": ["Alpha", "Beta", "Gamma"],
        "weights": [7, 2, 1],
    }

    result = strategy.apply(sample_data, rule)
    result2 = strategy.apply(sample_data, rule)

    assert len(result) == len(sample_data)
    assert result.isna().sum() == 1
    assert set(result.dropna()) <= {"Alpha", "Beta", "Gamma"}
    pd.testing.assert_series_equal(result, result2)
    assert result.iloc[0] == strategy.apply(pd.Series(["John"]), rule).iloc[0]
    assert not legacy_state_dir.exists()


def test_categorical_strategy_rejects_bad_policy(sample_data, mock_logger):
    strategy = CategoricalStrategy(seed=42, logger=mock_logger)
    with pytest.raises(ValueError, match="categories"):
        strategy.apply(sample_data, {"column": "name", "type": "categorical"})
    with pytest.raises(ValueError, match="weights"):
        strategy.apply(
            sample_data,
            {"column": "name", "type": "categorical", "categories": ["A"], "weights": [1, 2]},
        )


def test_shuffle_strategy(mock_logger):
    """Test shuffle strategy."""
    # Create data with duplicate values to test shuffling behavior
    data = pd.Series(["A", "B", "C", "D", "A", "B", None, None])

    strategy = ShuffleStrategy(seed=42, logger=mock_logger)
    rule = {"column": "data", "type": "shuffle"}

    result = strategy.apply(data, rule)

    assert len(result) == len(data)
    assert result.isna().sum() == 2  # Preserve NULL values
    assert set(result.dropna()) == set(data.dropna())  # Same set of values
    assert not result.equals(data)  # Values should be shuffled


def test_passthrough_strategy(sample_data, mock_logger):
    """Test passthrough strategy."""
    strategy = PassthroughStrategy(seed=42, logger=mock_logger)

    rule = {"column": "name", "type": "passthrough"}
    result = strategy.apply(sample_data, rule)

    # Should be identical to input
    pd.testing.assert_series_equal(result, sample_data)


def test_reference_strategy(sample_data, mock_logger, tmp_path):
    """Reference strategy: each input maps deterministically to a value
    drawn from the reference dataset; same input → same picked value."""
    ref_csv = tmp_path / "fake_names.csv"
    ref_csv.write_text("name\nAvery\nQuinn\nMorgan\nRiley\nJordan\n", encoding="utf-8")

    strategy = ReferenceStrategy(seed=42, logger=mock_logger)
    rule = {"column": "name", "type": "reference", "reference": str(ref_csv)}

    result = strategy.apply(sample_data, rule)

    assert len(result) == len(sample_data)
    assert result.isna().sum() == 1  # nulls preserved
    ref_values = {"Avery", "Quinn", "Morgan", "Riley", "Jordan"}
    assert set(result.dropna()) <= ref_values  # every pick comes from the ref
    # Determinism: re-running yields the same picks.
    result2 = strategy.apply(sample_data, rule)
    pd.testing.assert_series_equal(result, result2)
    # Determinism: same input → same output across rows.
    duped = pd.Series(["John", "John", "Jane", None])
    out = strategy.apply(duped, rule)
    assert out[0] == out[1]  # 'John' picks the same row both times
    assert pd.isna(out[3])


def test_reference_strategy_key_column(sample_data, mock_logger, tmp_path):
    """Multi-column reference + explicit key_column picks from that column."""
    ref_csv = tmp_path / "people.csv"
    ref_csv.write_text(
        "first,last\nAvery,Quinn\nMorgan,Riley\nJordan,Park\n",
        encoding="utf-8",
    )
    strategy = ReferenceStrategy(seed=42, logger=mock_logger)
    rule = {
        "column": "name",
        "type": "reference",
        "reference": str(ref_csv),
        "key_column": "last",
    }
    result = strategy.apply(sample_data, rule)
    assert set(result.dropna()) <= {"Quinn", "Riley", "Park"}


def test_reference_strategy_missing_path(mock_logger):
    """Validation: bad path raises at apply-time."""
    strategy = ReferenceStrategy(seed=42, logger=mock_logger)
    rule = {"column": "name", "type": "reference", "reference": "/nope/does-not-exist.csv"}
    with pytest.raises(ValueError, match="Reference dataset not found"):
        strategy.apply(pd.Series(["x"]), rule)


def test_strategy_factory(mock_logger):
    """Test strategy factory."""
    strategies = [
        ("faker", FakerStrategy),
        ("hash", HashStrategy),
        ("redact", RedactStrategy),
        ("categorical", CategoricalStrategy),
        ("shuffle", ShuffleStrategy),
        ("passthrough", PassthroughStrategy),
        ("reference", ReferenceStrategy),
    ]

    for strategy_type, expected_class in strategies:
        strategy = create_strategy(strategy_type, seed=42, logger=mock_logger)
        assert isinstance(strategy, expected_class)

    # Test invalid strategy type
    with pytest.raises(ValueError):
        create_strategy("invalid_type", seed=42, logger=mock_logger)
    with pytest.raises(ValueError):
        create_strategy("map", seed=42, logger=mock_logger)


def test_strategy_manager(sample_data, mock_logger):
    """Test the strategy manager."""
    manager = StrategyManager(seed=42, logger=mock_logger)

    # Test applying a single rule
    rule = {"column": "name", "type": "faker", "faker_type": "first_name"}
    result = manager.apply_masking_rule(sample_data, rule)

    assert len(result) == len(sample_data)
    assert result.isna().sum() == 1  # Preserve NULL values

    # Test applying multiple rules to a DataFrame
    df = pd.DataFrame(
        {
            "name": ["John", "Jane", "Alice"],
            "email": ["john@example.com", "jane@example.com", "alice@example.com"],
        }
    )

    rules = [
        {"column": "name", "type": "faker", "faker_type": "first_name"},
        {"column": "email", "type": "faker", "faker_type": "email"},
    ]

    result_df = manager.apply_masking_rules(df, rules)

    assert result_df.shape == df.shape
    assert not result_df["name"].equals(df["name"])
    assert not result_df["email"].equals(df["email"])

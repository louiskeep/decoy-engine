"""
Factory pattern for creating masking strategies based on configuration.
"""

from decoy_engine.transforms.base import BaseMaskingStrategy


def create_strategy(
    strategy_type: str,
    seed: int = 42,
    logger=None,
    derive_key=None,
) -> BaseMaskingStrategy:
    """
    Factory function to create appropriate masking strategy

    Args:
        strategy_type: Type of masking strategy to create
        seed: Random seed for deterministic masking (legacy fallback)
        logger: Logger instance (optional)
        derive_key: Optional ``(info: str) -> bytes`` for keyed determinism

    Returns:
        Appropriate masking strategy instance

    Raises:
        ValueError: If strategy type is not supported
    """
    strategy_type = strategy_type.lower()

    if strategy_type == "faker":
        from decoy_engine.transforms.faker_based import FakerStrategy

        return FakerStrategy(seed, logger, derive_key=derive_key)
    elif strategy_type == "hash":
        from decoy_engine.transforms.hash import HashStrategy

        return HashStrategy(seed, logger, derive_key=derive_key)
    elif strategy_type == "redact":
        from decoy_engine.transforms.redact import RedactStrategy

        return RedactStrategy(seed, logger)
    elif strategy_type == "categorical":
        from decoy_engine.transforms.categorical import CategoricalStrategy

        return CategoricalStrategy(seed, logger, derive_key=derive_key)
    elif strategy_type == "shuffle":
        from decoy_engine.transforms.shuffle import ShuffleStrategy

        return ShuffleStrategy(seed, logger)
    elif strategy_type == "passthrough":
        from decoy_engine.transforms.passthrough import PassthroughStrategy

        return PassthroughStrategy(seed, logger)
    elif strategy_type == "date_shift":
        from decoy_engine.transforms.date_shift import DateShiftStrategy

        return DateShiftStrategy(seed, logger, derive_key=derive_key)
    elif strategy_type == "formula":
        from decoy_engine.transforms.formula import FormulaStrategy

        return FormulaStrategy(seed, logger)
    elif strategy_type == "reference":
        from decoy_engine.transforms.reference import ReferenceStrategy

        return ReferenceStrategy(seed, logger, derive_key=derive_key)
    elif strategy_type == "truncate":
        from decoy_engine.transforms.truncate import TruncateStrategy

        return TruncateStrategy(seed, logger)
    elif strategy_type == "bucketize":
        from decoy_engine.transforms.bucketize import BucketizeStrategy

        return BucketizeStrategy(seed, logger)
    elif strategy_type == "fpe":
        from decoy_engine.transforms.fpe import FPEStrategy

        return FPEStrategy(seed, logger, derive_key=derive_key)
    else:
        raise ValueError(f"Unsupported strategy type: {strategy_type}")

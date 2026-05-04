# forge_engine/strategies/factory.py
"""
Factory pattern for creating masking strategies based on configuration.
"""

from typing import Dict, Any, Optional

from forge_engine.transforms.base import BaseMaskingStrategy


def create_strategy(strategy_type: str, seed: int = 42, logger=None) -> BaseMaskingStrategy:
    """
    Factory function to create appropriate masking strategy
    
    Args:
        strategy_type: Type of masking strategy to create
        seed: Random seed for deterministic masking
        logger: Logger instance (optional)
        
    Returns:
        Appropriate masking strategy instance
        
    Raises:
        ValueError: If strategy type is not supported
    """
    strategy_type = strategy_type.lower()
    
    if strategy_type == 'faker':
        from forge_engine.transforms.faker_based import FakerStrategy
        return FakerStrategy(seed, logger)
    elif strategy_type == 'hash':
        from forge_engine.transforms.hash import HashStrategy
        return HashStrategy(seed, logger)
    elif strategy_type == 'redact':
        from forge_engine.transforms.redact import RedactStrategy
        return RedactStrategy(seed, logger)
    elif strategy_type == 'map':
        from forge_engine.transforms.map import MapStrategy
        return MapStrategy(seed, logger)
    elif strategy_type == 'shuffle':
        from forge_engine.transforms.shuffle import ShuffleStrategy
        return ShuffleStrategy(seed, logger)
    elif strategy_type == 'passthrough':
        from forge_engine.transforms.passthrough import PassthroughStrategy
        return PassthroughStrategy(seed, logger)
    elif strategy_type == 'date_shift':
        from forge_engine.transforms.date_shift import DateShiftStrategy
        return DateShiftStrategy(seed, logger)
    elif strategy_type == 'formula':
        from forge_engine.transforms.formula import FormulaStrategy
        return FormulaStrategy(seed, logger)
    else:
        raise ValueError(f"Unsupported strategy type: {strategy_type}")
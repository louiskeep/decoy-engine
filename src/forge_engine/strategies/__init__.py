# forge_engine/strategies/__init__.py
"""
Masking strategies module for the forge_engine package.
Provides various strategies for masking sensitive data.
"""

from forge_engine.strategies.base import BaseMaskingStrategy
from forge_engine.strategies.faker import FakerStrategy
from forge_engine.strategies.hash import HashStrategy
from forge_engine.strategies.redact import RedactStrategy
from forge_engine.strategies.map import MapStrategy
from forge_engine.strategies.shuffle import ShuffleStrategy
from forge_engine.strategies.passthrough import PassthroughStrategy
from forge_engine.strategies.dateshift import DateShiftStrategy
from forge_engine.strategies.factory import create_strategy
from forge_engine.strategies.manager import StrategyManager

__all__ = [
    'BaseMaskingStrategy',
    'FakerStrategy',
    'HashStrategy',
    'RedactStrategy',
    'MapStrategy',
    'ShuffleStrategy',
    'PassthroughStrategy',
    'DateShiftStrategy',
    'create_strategy',
    'StrategyManager'
]
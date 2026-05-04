# forge_engine/strategies/__init__.py
"""
Masking strategies module for the forge_engine package.
Provides various strategies for masking sensitive data.
"""

from forge_engine.transforms.base import BaseMaskingStrategy
from forge_engine.transforms.faker_based import FakerStrategy
from forge_engine.transforms.hash import HashStrategy
from forge_engine.transforms.redact import RedactStrategy
from forge_engine.transforms.map import MapStrategy
from forge_engine.transforms.shuffle import ShuffleStrategy
from forge_engine.transforms.passthrough import PassthroughStrategy
from forge_engine.transforms.date_shift import DateShiftStrategy
from forge_engine.transforms.factory import create_strategy
from forge_engine.transforms.registry import StrategyManager

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
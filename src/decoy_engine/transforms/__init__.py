# decoy_engine/strategies/__init__.py
"""
Masking strategies module for the decoy_engine package.
Provides various strategies for masking sensitive data.
"""

from decoy_engine.transforms.base import BaseMaskingStrategy
from decoy_engine.transforms.faker_based import FakerStrategy
from decoy_engine.transforms.hash import HashStrategy
from decoy_engine.transforms.redact import RedactStrategy
from decoy_engine.transforms.map import MapStrategy
from decoy_engine.transforms.shuffle import ShuffleStrategy
from decoy_engine.transforms.passthrough import PassthroughStrategy
from decoy_engine.transforms.date_shift import DateShiftStrategy
from decoy_engine.transforms.fpe import FPEStrategy
from decoy_engine.transforms.factory import create_strategy
from decoy_engine.transforms.registry import StrategyManager

__all__ = [
    'BaseMaskingStrategy',
    'FakerStrategy',
    'HashStrategy',
    'RedactStrategy',
    'MapStrategy',
    'ShuffleStrategy',
    'PassthroughStrategy',
    'DateShiftStrategy',
    'FPEStrategy',
    'create_strategy',
    'StrategyManager'
]

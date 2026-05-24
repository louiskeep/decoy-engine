"""
Masking strategies module for the decoy_engine package.
Provides various strategies for masking sensitive data.
"""

from decoy_engine.transforms.base import BaseMaskingStrategy
from decoy_engine.transforms.categorical import CategoricalStrategy
from decoy_engine.transforms.date_shift import DateShiftStrategy
from decoy_engine.transforms.factory import create_strategy
from decoy_engine.transforms.faker_based import FakerStrategy
from decoy_engine.transforms.fpe import FPEStrategy
from decoy_engine.transforms.hash import HashStrategy
from decoy_engine.transforms.passthrough import PassthroughStrategy
from decoy_engine.transforms.redact import RedactStrategy
from decoy_engine.transforms.registry import StrategyManager
from decoy_engine.transforms.shuffle import ShuffleStrategy

__all__ = [
    "BaseMaskingStrategy",
    "CategoricalStrategy",
    "DateShiftStrategy",
    "FPEStrategy",
    "FakerStrategy",
    "HashStrategy",
    "PassthroughStrategy",
    "RedactStrategy",
    "ShuffleStrategy",
    "StrategyManager",
    "create_strategy",
]

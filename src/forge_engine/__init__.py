# forge_engine/__init__.py
"""
forge_engine — data masking and synthetic generation library.

Public API:
    Masker          orchestrates a masking pipeline from a YAML config
    DataGenerator   generates synthetic data with referential integrity

Anything not listed in __all__ is private and may change without a version bump.
"""

from forge_engine.masker import Masker
from forge_engine.generators import DataGenerator

__version__ = '0.1.0'
__all__ = ['Masker', 'DataGenerator']

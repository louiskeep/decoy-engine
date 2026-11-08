"""Generation primitives (engine-v2; S5+).

Pool-backed sampling lives in `decoy_engine.generation.pool`. Other
generation primitives (e.g. derive-value direct generation for custom
identifiers) land in sibling subpackages as S6+ sprints add them.
"""

from decoy_engine.generation._numeric_typing import generated_table_to_pandas

__all__ = ["generated_table_to_pandas"]

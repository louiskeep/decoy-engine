"""CardinalityMode enum.

Per S5 spec §6 + R6 reshape: four values. `deterministic_map` is NOT here.
Deterministic-vs-random is a separate first-class per-column plan field
(`deterministic: bool`) that composes orthogonally with cardinality mode.
The 2x4 matrix lives in the spec §6 table.
"""

from __future__ import annotations

from enum import Enum


class CardinalityMode(Enum):
    """Output cardinality shape for a pool-sampled column.

    Composes orthogonally with the `deterministic: bool` plan field;
    see S5 spec §6 for the 2x4 truth table.
    """

    REUSE = "reuse"
    UNIQUE = "unique"
    MATCH_SOURCE_CARDINALITY = "match_source_cardinality"
    SCALE_SOURCE_CARDINALITY = "scale_source_cardinality"

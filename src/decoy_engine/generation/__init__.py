"""Generation primitives (engine-v2; S5+).

Pool-backed sampling lives in `decoy_engine.generation.pool`. Other
generation primitives (e.g. derive-value direct generation for custom
identifiers) land in sibling subpackages as S6+ sprints add them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decoy_engine.generation._bounds import FloorShortfall

__all__ = ["detected_memory_bytes", "resident_floor_shortfall"]


# Both import lazily: every generation submodule imports this package, and the floor pulls in
# Faker and the column admission tables.
def resident_floor_shortfall(config: dict[str, Any], available_bytes: int) -> FloorShortfall | None:
    """The resident-generation floor decision for a validated config, or `None` when it fits."""
    from decoy_engine.generation._admission import classify_plan
    from decoy_engine.generation._bounds import generation_floor_shortfall

    return generation_floor_shortfall(config, classify_plan(config), available_bytes)


def detected_memory_bytes() -> int | None:
    """The whole memory ceiling the floor is compared with, or `None` when it cannot be read."""
    from decoy_engine.generation import _bounds

    return _bounds.detected_memory_bytes()

"""QualityWarning event shape.

S5 emits events; S10 consumes them into the manifest's `quality_summary`.
Per cross-sprint contracts R14: S5 owns the event shape; S10 owns the
manifest field. No double-definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class QualityWarning:
    """One non-fatal quality observation surfaced by the pool sampler/builder.

    Codes owned by S5:

    - `low_distinct_ratio`: sampled output distinct ratio below target.
    - `pool_scaled_up`: pool size grew to accommodate source distinct count.
    - `pool_scale_up_exceeded_budget`: scale-up would exceed cache budget;
      converted to fall_back instead.
    - `pool_fallback`: pool path bypassed; per-row generation used.
    - `pool_dominates_cache`: single pool occupies >25% of cache budget.
    """

    code: str
    provider: str
    column: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

"""QualityWarning event shape.

S5 emits events; S10 consumes them into the manifest's `quality_summary`.
Per cross-sprint contracts R14: S5 owns the event shape; S10 owns the
manifest field. No double-definition.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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

    Codes added by later sprints (same shape, same emission channel; the type
    stays S5-owned per R14):

    - `mimesis_locale_fallback` (S7): an adopted Mimesis provider was asked for
      a locale outside Mimesis coverage; the adapter fell back to Faker for
      that locale + provider. `detail` carries `requested_locale` +
      `mimesis_locales`.
    - `generation_fidelity_below_threshold` (statistical fidelity gate): a generated table's
      overall score, or one comparable column's score, is below `fidelity_warn_threshold`.
      `detail` carries `table`, `snapshot_index`, `overall_score`, `threshold`, `trigger`,
      `worst_columns` and `below_columns`; scores and names only, never a cell value.
    - `generation_condition_parent_unseen` (conditional statistical column, or a categorical
      column with `condition_on` and `by`; `provider` says which): some rows had a `condition_on`
      parent with no fitted combination or `by` entry and drew from the column's overall mix.
      `detail` carries `table`, `column`, `condition_on`, `rows`, `rows_null_parent`,
      `rows_total`, `parent_values` (a capped count of distinct parent keys) and `parent_values_capped`; names and counts only.
    """

    code: str
    provider: str
    column: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def emit_generation_warning(
    record: QualityWarning,
    log: logging.Logger,
    summary: str,
    on_warning: Callable[[QualityWarning], None] | None,
) -> None:
    """The one emitter for value-free generation warnings: log the summary, then hand the record on.

    `summary` carries names and counts only; the callback sees the structured record.
    """
    log.warning("%s", summary)
    if on_warning is not None:
        on_warning(record)

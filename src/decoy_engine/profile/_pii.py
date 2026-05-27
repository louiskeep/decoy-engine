"""STORM PII detection bridge for the Profile module.

When walk_dataframe is called with run_pii_detection=True, this module
calls decoy_engine.storm.run_storm against the DataFrame and translates
its DetectorMatch hits into the closed PIIClass enum.

Confidence policy: only STORM "high"-confidence matches set pii_class.
"high" is the safe-to-auto-apply bucket per STORM's own docstring
(detectors.py): name hint + content match rate >= 45%, OR content
alone >= 75%. Medium / low matches return None at this layer; the
planner should not key on probabilistic signals without explicit opt-in.

Custom detectors (CustomDetectorSpec ids namespaced like
custom__uk_nhs_number) match against columns but their detector_id is
not in PIIClass. They are intentionally not represented at the Profile
layer; the column gets pii_class=None. A V2+ extension can add a
sibling custom_pii_class field if needed.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.profile._types import PIIClass
from decoy_engine.storm import DetectorMatch, run_storm


def detect_pii_classes(df: pd.DataFrame, table_name: str) -> dict[str, PIIClass]:
    """Run STORM on the DataFrame and return high-confidence PII tags per column.

    Returns a dict {column_name: PIIClass}. Columns with no high-confidence
    detector match, or matches whose detector_id is not in PIIClass
    (custom detectors), do not appear in the dict.

    Args:
        df: source data. Passed through to STORM unchanged.
        table_name: used as STORM's source_label for lineage events.

    Returns:
        Mapping from column name to PIIClass for columns where STORM
        fired a high-confidence built-in detector.
    """
    storm_profile = run_storm(df, source_label=table_name)
    tags: dict[str, PIIClass] = {}
    for field_stats in storm_profile.fields:
        best = _best_high_confidence_match(field_stats.detector_matches)
        if best is None:
            continue
        # detector_id is a string. PIIClass(value) raises ValueError for
        # ids outside the closed enum (custom detectors, future built-ins
        # not yet enumerated). Skip those rather than guessing.
        try:
            tags[field_stats.name] = PIIClass(best.detector_id)
        except ValueError:
            continue
    return tags


def _best_high_confidence_match(matches: list[DetectorMatch]) -> DetectorMatch | None:
    """Pick the high-confidence detector with the highest match_rate.

    Returns None if no detector fired with confidence="high". Ties on
    match_rate resolve to the first detector in list order; STORM's
    detector evaluation order is stable, so this stays deterministic.
    """
    high_matches = [m for m in matches if m.confidence == "high"]
    if not high_matches:
        return None
    return max(high_matches, key=lambda m: m.match_rate)

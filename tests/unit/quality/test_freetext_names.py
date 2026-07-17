"""Precision/recall tests for the HC-7 free-text name-hint recognizer.

The tricky negatives (`note_id`, `description_code`, `phone_number`) are
the whole point of the suffix-anchored design documented in the module
docstring: a naive "term appears anywhere as a bounded token" matcher
(the STORM `_hits_name_hint` style) would false-positive on all three.
"""

from __future__ import annotations

import pytest

from decoy_engine.quality._freetext_names import matches_freetext_name


@pytest.mark.parametrize(
    "col_name",
    [
        "clinical_notes",
        "claim_description",
        "patient_notes",
        "notes",
        "note",
        "comment",
        "comments",
        "description",
        "desc",
        "memo",
        "remark",
        "remarks",
        "narrative",
        "summary",
        "free_text",
        "freetext",
        "chief_complaint",
        "hpi",
        "assessment",
        "progress_note",
        "diagnosis_text",
        "reason",
        "findings",
        "impression",
        "physician_progress_note",
        "CLINICAL_NOTES",
        "Claim_Description",
    ],
)
def test_matches_known_freetext_names(col_name: str) -> None:
    assert matches_freetext_name(col_name) is True


@pytest.mark.parametrize(
    "col_name",
    [
        "note_id",
        "description_code",
        "phone_number",
        "customer_id",
        "notebook_id",
        "descriptive_stats",
        "reasoning_engine",
        "summary_id",
        "email",
        "ssn",
        "",
    ],
)
def test_does_not_match_negatives(col_name: str) -> None:
    assert matches_freetext_name(col_name) is False

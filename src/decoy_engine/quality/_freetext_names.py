"""Pure name-hint recognizer for clinical/claims free-text columns (HC-7).

Reuses the token-boundary matching style of `storm/detectors.py`'s
`_hint()` (case-insensitive, `._-`-delimited token boundaries), with one
deliberate divergence: `_hint()` allows a matched term to sit anywhere in
the name (prefix, infix, or suffix), which is right for STORM's PII
detectors but wrong here -- it would match `note_id` and
`description_code` on the bare `note` / `description` tokens, exactly the
`*_id` / `*_code` noise this advisory must not produce (an identifier
column is not free text just because a free-text word appears in its
name). This module instead anchors the match to the END of the column
name (optionally preceded by other `._-`-delimited segments): the
free-text word must be the LAST semantic segment, not merely present.
`clinical_notes` / `claim_description` / `patient_notes` all end in a
token; `note_id` / `description_code` end in `id` / `code`, so they miss.

No I/O, no config, no source-data dependency -- pure string matching.
"""

from __future__ import annotations

import re

# Kept tight (per the HC-7 design note) to bias toward precision over
# recall: a false negative here just means the length+distinctness
# fallback (or nothing) catches the column; a false positive is a noisy
# warning on every unmasked config that happens to use one of these words
# for a non-clinical column.
_FREETEXT_NAME_TOKENS: tuple[str, ...] = (
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
    "clinical_notes",
    "claim_description",
    "chief_complaint",
    "hpi",
    "assessment",
    "progress_note",
    "diagnosis_text",
    "reason",
    "findings",
    "impression",
)

_FREETEXT_NAME_PATTERN = re.compile(
    r"(?i)^(?:.*[._-])?(" + "|".join(re.escape(t) for t in _FREETEXT_NAME_TOKENS) + r")$"
)


def matches_freetext_name(col_name: str) -> bool:
    """True if `col_name`'s final `._-`-delimited segment is a known
    clinical/claims free-text token (or the whole name equals one).

    Case-insensitive. Deterministic; no I/O.
    """
    return bool(_FREETEXT_NAME_PATTERN.match(col_name or ""))

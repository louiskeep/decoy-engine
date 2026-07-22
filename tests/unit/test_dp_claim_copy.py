"""DPS Scope B (guide section 8.4): the canonical DP limitations page must
carry the reviewed claim wording, and must never omit "approximate,"
"marginal," or the joint-exclusion statement, under any future revision.

This protects the required semantic phrases, not the entire prose
verbatim (guide section 8.4): a documentation pass may reword the
surrounding text freely, but these specific claims are load-bearing and
this test fails the build if any of them is dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "what-we-cannot-prove.md"


def _doc_text() -> str:
    """Markdown source, with blockquote markers stripped and runs of
    whitespace (including line-wrap newlines within a paragraph)
    collapsed to single spaces, so a required phrase is found regardless
    of where the prose happens to wrap or whether it sits inside a `>`
    blockquote."""
    raw = _DOC_PATH.read_text(encoding="utf-8")
    unquoted = re.sub(r"(?m)^>\s?", "", raw)
    return re.sub(r"\s+", " ", unquoted)


def test_dp_claim_copy_is_marginal_and_names_joint_exclusion():
    text = _doc_text()

    # "approximate (epsilon, delta)" or the project's rendered equivalent.
    assert "approximate `(epsilon, delta)`" in text or "approximate (epsilon, delta)" in text

    # "single-column marginal".
    assert "single-column marginal" in text

    # An explicit denial of joint and cross-column guarantees.
    assert "does not cover joint distributions, cross-column correlations" in text

    # The pinned-Plan boundary.
    assert "DP-verified pinned Plan" in text

    # Release-ID composition language.
    assert "privacy losses compose" in text
    assert "charged once" in text


def test_dp_claim_copy_names_no_removed_option_a_claims():
    """Every Option A overclaim guide section 8.1 lists must be gone."""
    text = _doc_text().lower()

    removed_claims = [
        "generation may safely reread the same path",
        "a content hash identifies a privacy release",
        "numeric support implies joint protection",
        "categorical support reveals only values already safe to disclose",
        "successful compilation alone protects callers that bypass the compiled plan",
    ]
    for claim in removed_claims:
        assert claim not in text, f"Option A overclaim still present: {claim!r}"

    # Option A mechanism names must not appear as current, supported
    # behavior -- the mechanism was deleted outright (pre-GA hard delete).
    assert "apply_dp_noise" not in text
    assert "dp_mode" not in text

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


def test_dp_claim_copy_names_the_typed_carrier_and_its_adapter():
    """DPS-CODEC phase 8: the guarantee is now stated over declared typed
    carriers (`number`/`text`/`flag`), not pandas storage, and the pandas
    adapter that produces them is itself certified as a stability-1
    transformation. Pin the carrier-accurate claims so a future edit cannot
    silently reintroduce the retired stringification wording."""
    text = _doc_text()

    assert "certified as a stability-1 transformation" in text
    assert "text` column releases only genuine string cells" in text
    assert "flag` column releases the two canonical labels `true` and `false`" in text


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


def test_dp_claim_copy_does_not_overstate_the_forgery_defenses():
    """Security review 2026-07-23 (B1, H1): both defenses were described as
    stronger than they are, and neither overstatement was pinned, so nothing
    caught the drift.

    B1: the numeric shape check reads four attacker-writable fields. Nulling
    `mean`/`std`/`quantiles` and declaring the observed min/max defeats it
    with no DP knowledge, so the page must not claim that defeating it
    requires replicating the artifact format, and must not present the
    numeric path as defended relative to the categorical one.

    H1: the embedded snapshot digest covers bytes in the same file, so a
    hostile edit that recomputes it is undetectable, and the surviving
    receipt fields do not distinguish the tampered plan from an honest one.
    """
    text = _doc_text()
    lowered = text.lower()

    # B1: the false causal claim must stay gone.
    assert "replicated the entire dp artifact format" not in lowered
    assert "replicates a genuine release's shape from scratch" not in lowered

    # B1: what actually defeats the check must be stated.
    assert "attacker-writable" in lowered
    assert "still the exact, unnoised histogram" in lowered
    assert "guard against copy-paste, not against an adversary" in lowered

    # B1: the asymmetry must be scoped to the copy-paste case.
    assert "asymmetry is real for the copy-paste case alone" in lowered

    # H1: the digest's lack of authentication value must be stated, along
    # with the payload-swap case and the indistinguishable receipt.
    assert "no authentication value" in lowered
    assert "recomputes the digest passes" in lowered
    assert "distinguishes it from an honest one" in lowered
    # dennis round 10 (LOW-2): the page described the working edit
    # imprecisely. A wholesale exact snapshot carries no `dp` block and is
    # rejected by the provenance check; what works is keeping the honest
    # `dp` block and swapping only `top_values`. Stating the weaker
    # version would let a reader conclude the provenance check covers it.
    assert "replace only the pinned snapshot's `top_values`" in lowered
    assert "does not work" in lowered


def test_dp_claim_copy_scopes_the_domain_to_values_not_live_objects():
    """Codex round 10 (BLOCKER): a cell whose `__float__` raised a direct
    `BaseException` subclass escaped the `Exception` guards and aborted
    the fit, which is the fit-success channel by another route.

    The guards now catch `BaseException` and drop the row, except for
    `KeyboardInterrupt` and `SystemExit`, which are re-raised so an
    operator can still interrupt a fit. That leaves a narrow residual --
    a cell raising one of those two from its own methods -- and a
    guarantee page may not simply omit it. Totality cannot be absolute
    over arbitrary Python objects, so the domain has to say so.
    """
    text = _doc_text().lower()

    assert "must be values, not live objects" in text
    assert "keyboardinterrupt" in text and "systemexit" in text
    assert "outside this guarantee" in text
    # And why the residual is narrow rather than merely disclosed.
    assert "nothing loaded from a file can be such a cell" in text

    # Codex round 11: the page went on to describe the OLD handling two
    # paragraphs later -- `except Exception`, and a claim that catching
    # `BaseException` would swallow Ctrl-C -- both the opposite of what
    # ships. A guarantee page carrying two contradictory descriptions of
    # its own enforcement is worse than one that describes neither.
    assert "enforced with `except baseexception`" in text
    assert "two types are deliberately re-raised" in text

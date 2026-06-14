"""Sentry test: published performance claims trace to a measurement.

engineering-best-practices section 6.4: a performance claim we publish must
trace to a benchmark. A bare "10x faster" or "2M rows/sec" in a doc that no
measurement backs is exactly the kind of statement a customer's engineer will
test and find false.

Scope is deliberately narrow: this gate scans `docs/` (the published surface).
It does NOT scan source-code comments. Inline notes like "~50x faster on wide
tables" next to the code that earns them are engineering observations, not
published claims, and requiring each to cite a benchmark file would be noise.
The rule is about what we stand behind in writing to users.

For any doc that states a quantitative perf claim, the same doc must reference
a measurement source (a path under a benchmark/calibration tree, or an explicit
"measured" / "benchmark" / "calibration" citation). The check is file-scoped on
purpose: it is a guardrail that forces a benchmark link to exist, not a proof
that the link backs the specific number. The reviewer confirms the latter.

There are no perf claims in `docs/` today, so this is a forward-looking gate:
it goes red the first time someone publishes a number without a source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
DOCS = REPO / "docs"

# A quantitative performance claim: an "Nx faster/slower/speedup" ratio, or a
# throughput figure in rows/records/bytes per unit time. The "x" allows the
# multiplication sign (built from an escape so no ambiguous literal glyph lands
# in source, per RUF001).
_TIMES = "x" + chr(0xD7)  # 'x' or U+00D7 multiplication sign, no literal glyph in source
_PERF_CLAIM = re.compile(
    r"\b\d+(?:\.\d+)?\s*[" + _TIMES + r"]\s*(?:faster|slower|speedup)\b"
    r"|\b\d[\d,]*[kKmMgG]?\s*(?:rows?|records?|MB|GB)\s*(?:/|\bper\b)\s*"
    r"(?:s\b|sec|second|min)",
    re.IGNORECASE,
)

# Evidence that a measurement backs the doc: a benchmark/calibration reference.
_PROVENANCE = re.compile(r"(?i)\b(benchmark|calibration|measured)\b")


def _doc_files() -> list[Path]:
    if not DOCS.exists():
        return []
    return sorted(DOCS.rglob("*.md"))


@pytest.mark.parametrize(
    "doc",
    _doc_files(),
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_perf_claims_cite_a_measurement(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    claim = _PERF_CLAIM.search(text)
    if claim is None:
        return
    assert _PROVENANCE.search(text), (
        f"{doc.relative_to(REPO)} states a performance claim "
        f"({claim.group(0).strip()!r}) but references no measurement "
        f"(best-practices section 6.4). Cite the benchmark or calibration "
        f"source behind the number, or move the claim out of published docs."
    )


def test_claim_pattern_matches_examples() -> None:
    """Meta-test: the claim regex catches the forms we mean to gate."""
    for s in ["10x faster", "2.5x speedup", "2M rows/sec", "500 records per second", "120 MB/s"]:
        assert _PERF_CLAIM.search(s), f"should match a perf claim: {s!r}"


def test_claim_pattern_ignores_non_claims() -> None:
    """Meta-test: version strings and counts without a rate do not match."""
    for s in ["v2.1", "10 rows in the table", "3 columns", "released 2026-Q4"]:
        assert _PERF_CLAIM.search(s) is None, f"should NOT match: {s!r}"


def test_sentry_catches_a_planted_claim_without_provenance(tmp_path: Path) -> None:
    """Meta-test: a claim with no benchmark reference fails the provenance check."""
    text = "# Speed\n\nThe pipeline is 10x faster than the legacy path.\n"
    assert _PERF_CLAIM.search(text) is not None
    assert _PROVENANCE.search(text) is None


def test_sentry_accepts_a_claim_with_provenance() -> None:
    """Meta-test: the same claim with a benchmark reference passes."""
    text = (
        "# Speed\n\nThe pipeline is 10x faster (see the calibration benchmark "
        "in tests/benchmark/calibration/results.md).\n"
    )
    assert _PERF_CLAIM.search(text) is not None
    assert _PROVENANCE.search(text) is not None

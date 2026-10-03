"""Acceptance test 11 of plan 2026-10-02-b6b-lazy-batch-input (rev 2.1): sentries and
bookkeeping for the LazySource batch input.

The seam-disconnection lists are updated in `tests/sentry/test_physical_seam_disconnection.py`;
this module pins the rest: module-size bars, the unchanged public surface, the
compatibility-contract entries (all three new error codes by name), and the CHANGELOG and
CODEMAP entries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.sentry import test_module_size as size_sentry
from tests.sentry import test_physical_seam_disconnection as seam

REPO = Path(__file__).resolve().parents[3]
EXEC = REPO / "src" / "decoy_engine" / "execution"
NEW = "_chunked_input.py"


def test_module_sizes_hold_their_bars() -> None:
    assert size_sentry._loc(EXEC / NEW) <= 600
    assert f"src/decoy_engine/execution/{NEW}" not in size_sentry.ALLOWLIST
    assert size_sentry._loc(EXEC / "_planner.py") <= 600
    assert size_sentry.ALLOWLIST["src/decoy_engine/execution/_pipeline.py"] == 679
    assert size_sentry._loc(EXEC / "_pipeline.py") == 679


def test_the_new_module_is_guarded_by_the_seam_disconnection_sentry() -> None:
    assert f"execution/{NEW}" in seam.GUARDED_MODULES
    assert not seam._imports_physical(EXEC / NEW)


def test_b6b_adds_no_public_name() -> None:
    import decoy_engine
    from decoy_engine import execution

    for name in ("SourceFacts", "OpenedLazyBatches", "open_input", "_chunked_input"):
        assert name not in decoy_engine.__all__
        assert name not in execution.__all__


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a : text.index(end, a + len(start))]


@pytest.mark.parametrize(
    "needle",
    [
        "lazy_source_changed",
        "lazy_source_row_count_mismatch",
        "hold_back_spill_unavailable",
        "spill_parent",
        "footer_facts",
        "open_batches",
        "OpenedLazyBatches",
        "LazySource",
        "loaded_fully_in_memory",
        "auto_chunk.input",
    ],
)
def test_the_compatibility_contract_records_the_new_surface(needle: str) -> None:
    contract = (REPO / "docs" / "compatibility-contract.md").read_text(encoding="utf-8")
    assert needle in contract, needle


def test_the_contract_run_pipeline_entry_admits_lazy_sources_to_auto_chunk() -> None:
    contract = (REPO / "docs" / "compatibility-contract.md").read_text(encoding="utf-8")
    api = _section(contract, "### 3.4", "### 3.5")
    assert "LazySource" in api and "auto-chunk" in api


def test_spill_parent_is_not_part_of_the_transactional_sink_protocol() -> None:
    from decoy_engine.execution._transactional_sink import TransactionalSink

    assert "spill_parent" not in dir(TransactionalSink)


def test_changelog_unreleased_names_the_lazy_input() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = _section(changelog, "## [Unreleased]", "\n## [0.")
    for needle in ("LazySource", "lazy_source_changed", "spill_parent"):
        assert needle in unreleased, needle


def test_codemap_has_the_ownership_row_for_the_new_module() -> None:
    assert NEW in (REPO / "CODEMAP.md").read_text(encoding="utf-8")


def test_run_pipeline_docstring_and_admission_comment_describe_the_lazy_input() -> None:
    pipeline = (EXEC / "_pipeline.py").read_text(encoding="utf-8")
    assert "LazySource" in pipeline and "streams when the output streams" in pipeline
    admission = (EXEC / "_unified_slice_admission.py").read_text(encoding="utf-8")
    assert "B6b" in admission or "kept lazy" in admission


def test_the_old_planner_refusal_helper_is_gone() -> None:
    from decoy_engine.execution import _pipeline_sources

    assert not hasattr(_pipeline_sources, "lazy_source_rejection")

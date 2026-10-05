"""Acceptance test 12 of plan 2026-10-01-dispatcher-auto-chunk (rev 5): sentries
and bookkeeping for the auto-chunk dispatcher lane.

The seam-disconnection lists are updated in `tests/sentry/test_physical_seam_disconnection.py`
and lossless forwarding in `tests/physical/test_lossless_forwarding.py`; this
module pins the rest: the module-size bars, the unchanged public surface, and the
active-doc inventory of Design 10.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.sentry import test_module_size as size_sentry
from tests.sentry import test_physical_seam_disconnection as seam

REPO = Path(__file__).resolve().parents[3]
EXEC = REPO / "src" / "decoy_engine" / "execution"


def test_pipeline_stays_at_its_exact_ratchet_and_the_new_module_under_the_goal() -> None:
    # B2 adds no net lines to `_pipeline.py`: the ratchet is neither raised nor loosened.
    assert size_sentry.ALLOWLIST["src/decoy_engine/execution/_pipeline.py"] == 684
    assert size_sentry._loc(EXEC / "_pipeline.py") == 684
    assert size_sentry._loc(EXEC / "_pipeline_auto_chunk.py") <= 600
    assert "src/decoy_engine/execution/_pipeline_auto_chunk.py" not in size_sentry.ALLOWLIST


def test_the_new_module_is_guarded_by_the_seam_disconnection_sentry() -> None:
    assert "execution/_pipeline_auto_chunk.py" in seam.GUARDED_MODULES
    assert not seam._imports_physical(EXEC / "_pipeline_auto_chunk.py")


def test_b2_adds_no_public_name() -> None:
    import decoy_engine
    from decoy_engine import execution

    for name in ("run_auto_chunk", "join_dispatcher_chunks", "_pipeline_auto_chunk"):
        assert name not in decoy_engine.__all__
        assert name not in execution.__all__


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a : text.index(end, a + len(start))]


def test_compatibility_contract_records_the_signature_and_the_output_cutover() -> None:
    contract = (REPO / "docs" / "compatibility-contract.md").read_text(encoding="utf-8")
    api = _section(contract, "### 3.4", "### 3.5")
    for needle in (
        "chunked_dispatcher_enabled",
        "native_threads",
        "ROUTE-OUTPUT-CONTRACT",
        "masked values",
    ):
        assert needle in api, needle


def test_changelog_unreleased_states_the_output_change_and_the_kill_switch() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = _section(changelog, "## [Unreleased]", "\n## [0.")
    for needle in ("chunked_dispatcher_enabled", "native_threads", "pre-GA", "Faker"):
        assert needle in unreleased, needle


def test_codemap_has_the_ownership_row_for_the_new_module() -> None:
    codemap = (REPO / "CODEMAP.md").read_text(encoding="utf-8")
    assert "_pipeline_auto_chunk.py" in codemap


# Active-doc inventory (Design 10): each stale claim the revision-5 probe found.
STALE_CLAIMS = {
    "src/decoy_engine/execution/_pipeline.py": ["Every route is byte-output-neutral"],
    "src/decoy_engine/execution/__init__.py": ["every route is byte-output-equivalent"],
    "CODEMAP.md": ["a memory-only win with byte-identical output"],
    "src/decoy_engine/execution/_pipeline_finalize.py": [
        "the strict chunk concat refuses to merge chunks whose schemas"
    ],
}


@pytest.mark.parametrize("path", sorted(STALE_CLAIMS))
def test_stale_route_neutrality_claims_are_gone(path: str) -> None:
    text = re.sub(r"\s+", " ", (REPO / path).read_text(encoding="utf-8")).replace("# ", "")
    for phrase in STALE_CLAIMS[path]:
        assert phrase not in text, f"{path}: {phrase!r}"


def test_finalize_comment_names_the_dispatcher_join_and_guarantee_3() -> None:
    text = (EXEC / "_pipeline_finalize.py").read_text(encoding="utf-8")
    assert "join_dispatcher_chunks" in text


@pytest.mark.parametrize(
    "path",
    [
        "_planner.py",
        "_pipeline_routing.py",
        "_pipeline_route_exec.py",
    ],
)
def test_docstrings_no_longer_say_auto_chunk_routes_through_the_oracle(path: str) -> None:
    text = re.sub(r"\s+", " ", (EXEC / path).read_text(encoding="utf-8"))
    assert "run_mask_chunked" in text
    assert "ROUTES this mode through `run_mask_pipeline_chunked`" not in text
    assert "streams it through `run_mask_pipeline_chunked`" not in text

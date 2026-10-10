"""Acceptance test 12 of plan 2026-10-02-b6a-incremental-output-sink (rev 2.1): sentries
and bookkeeping for the incremental output sink.

The seam-disconnection lists are updated in `tests/sentry/test_physical_seam_disconnection.py`;
this module pins the rest: the module-size bars, the unchanged public surface, the
compatibility-contract signature, and the CHANGELOG and CODEMAP entries.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from tests.sentry import test_module_size as size_sentry
from tests.sentry import test_physical_seam_disconnection as seam

REPO = Path(__file__).resolve().parents[3]
EXEC = REPO / "src" / "decoy_engine" / "execution"
NEW = "_chunked_output_sink.py"


def test_pipeline_stays_at_its_exact_ratchet_and_the_new_module_under_the_goal() -> None:
    # R3 (run-context refactor) restructured _pipeline.py; the B2/B6a/B6b no-net-change pin is superseded by the census sentry.
    assert "src/decoy_engine/execution/_pipeline.py" not in size_sentry.ALLOWLIST
    assert size_sentry._loc(EXEC / "_pipeline.py") == 457
    assert size_sentry._loc(EXEC / NEW) <= 600
    assert f"src/decoy_engine/execution/{NEW}" not in size_sentry.ALLOWLIST


def test_the_new_module_is_guarded_by_the_seam_disconnection_sentry() -> None:
    assert f"execution/{NEW}" in seam.GUARDED_MODULES
    assert not seam._imports_physical(EXEC / NEW)


def test_b6a_adds_no_public_name() -> None:
    import decoy_engine
    from decoy_engine import execution

    for name in ("OutputPublish", "decide_output_mode", "_chunked_output_sink"):
        assert name not in decoy_engine.__all__
        assert name not in execution.__all__


def test_the_run_pipeline_signature_has_the_new_knob_next_to_the_lane_knobs() -> None:
    from decoy_engine.execution import run_pipeline

    params = inspect.signature(run_pipeline).parameters
    assert params["stream_chunked_output"].default is True
    assert params["stream_chunked_output"].kind is inspect.Parameter.KEYWORD_ONLY
    names = list(params)
    assert names.index("stream_chunked_output") == names.index("chunked_dispatcher_enabled") + 1


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a : text.index(end, a + len(start))]


def test_compatibility_contract_records_the_knob_and_the_sink_meaning() -> None:
    contract = (REPO / "docs" / "compatibility-contract.md").read_text(encoding="utf-8")
    api = _section(contract, "### 3.4", "### 3.5")
    for needle in ("stream_chunked_output", "outputs_streamed"):
        assert needle in api, needle


def test_changelog_unreleased_names_the_knob_and_the_streamed_result_shape() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = _section(changelog, "## [Unreleased]", "\n## [0.")
    for needle in ("stream_chunked_output", "TransactionalSink", "outputs"):
        assert needle in unreleased, needle


def test_codemap_has_the_ownership_row_for_the_new_module() -> None:
    assert NEW in (REPO / "CODEMAP.md").read_text(encoding="utf-8")


def test_the_unified_slice_comment_and_run_pipeline_docstring_name_the_new_consumer() -> None:
    admission = (EXEC / "_unified_slice_admission.py").read_text(encoding="utf-8")
    assert "never consumed" not in admission or "auto-chunk" in admission
    pipeline = (EXEC / "_pipeline.py").read_text(encoding="utf-8")
    assert "stream_chunked_output" in pipeline

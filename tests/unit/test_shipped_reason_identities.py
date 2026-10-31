"""Every reason identity that shipped in a tagged release still resolves through the registry.

The table is the v0.16.0 reason surface: the literals returned from the route decider
(`_pipeline_routing._sequential_eligible` / `decide_execution_route`) and from the chunked output
sink's `decide_output_mode`, read from `git show v0.16.0:src/decoy_engine/execution/...`. Persisted
routing evidence from that release names these codes; a renamed one must stay readable as a
deprecated tombstone that links to its replacement (registry class rule: append-only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decoy_engine import reasons

_MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.json"

# (code, replacement or None when the identity is still live under its shipped name)
V0_16_0_ROUTE_REASONS: tuple[tuple[str, str | None], ...] = (
    # _sequential_eligible
    ("fidelity_report_requested", None),
    ("generate_plus_mask", None),
    ("no_relationships", None),
    ("non_pandas_substrate_requested", None),
    ("post_validation_requested", None),
    ("pure_mask_fk", None),
    ("run_storm_requested", "post_scan_integrity_requested"),
    ("validators_present", None),
    ("vault_writer_requested", None),
    # decide_execution_route
    ("byte_estimate_bounded_out_of_core", None),
    ("byte_estimate_full_frame_fits", None),
    ("mixed_generate_mask_chunked", None),
    ("out_of_core_large_fk", None),
    ("override_full_frame", None),
    ("override_out_of_core", None),
    ("probe_recovered_full_frame", None),
    # decide_output_mode (chunked output sink)
    ("eligible", None),
    ("fidelity_report", None),
    ("legacy_lane", None),
    ("no_sink", None),
    ("post_validation", None),
    ("quarantine_enabled", None),
    ("run_storm", "post_scan_integrity"),
    ("sink_not_streaming", None),
    ("split_extra_sources_present", None),
    ("split_full_frame_present", None),
    ("streaming_disabled", None),
)


@pytest.mark.parametrize(
    ("code", "replacement"), V0_16_0_ROUTE_REASONS, ids=[c for c, _ in V0_16_0_ROUTE_REASONS]
)
def test_shipped_route_reason_resolves(code: str, replacement: str | None) -> None:
    spec = reasons.find("route", code)
    assert spec is not None, f"route.{code} shipped in v0.16.0 and no longer resolves"
    if replacement is None:
        assert spec.deprecated_in is None and spec.replaced_by is None
        return
    assert spec.deprecated_in is not None, "a renamed identity is a tombstone, not a live entry"
    assert spec.replaced_by == ("route", replacement)
    live = reasons.get("route", replacement)
    assert live.deprecated_in is None, "a replacement must itself be live"
    assert live.kind is spec.kind and live.rules == spec.rules


def test_the_pinned_manifest_carries_tombstone_and_replacement() -> None:
    entries = {(e["namespace"], e["code"]): e for e in json.loads(_MANIFEST.read_text())["entries"]}
    for code, replacement in V0_16_0_ROUTE_REASONS:
        assert ("route", code) in entries
        if replacement is not None:
            assert entries[("route", code)]["replaced_by"] == ["route", replacement]
            assert ("route", replacement) in entries


def test_no_tombstone_without_a_live_replacement() -> None:
    for spec in reasons.specs():
        if spec.deprecated_in is None:
            continue
        assert spec.replaced_by is not None, f"{spec.identity} is retired with no replacement"
        assert reasons.get(*spec.replaced_by).deprecated_in is None

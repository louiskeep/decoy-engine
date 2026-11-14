"""SCAN-MULTI S3-engine.1: the starter decision port and the central detector descriptors.

T3-parity: the engine function gives what the CLI's `_infer_strategy_for_column` gave at
8fba6af (strategy, provider, provider_config, review) for every checked-in case, and the
`DEFAULT_STRATEGY_BY_DETECTOR` compatibility view is unchanged and derived from the same
descriptors that carry the starter choices.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from decoy_engine.profiling import _starter_inference, recommendations
from decoy_engine.profiling._starter_inference import infer_starter_strategy
from decoy_engine.profiling.recommendations import (
    DEFAULT_STRATEGY_BY_DETECTOR,
    DETECTOR_DESCRIPTORS,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "scan_multi"
CASES = json.loads((FIXTURES / "cli_inference_8fba6af.json").read_text())["cases"]
BASE_DEFAULTS = json.loads((FIXTURES / "default_strategy_by_detector_base.json").read_text())


def _stats(case: dict) -> SimpleNamespace:
    return SimpleNamespace(
        name="c",
        inferred_type=case["inferred_type"],
        is_likely_unique=case["is_likely_unique"],
        detector_matches=[SimpleNamespace(**m) for m in case["matches"]],
    )


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_parity_with_the_cli_decision(case: dict) -> None:
    got = infer_starter_strategy(_stats(case))
    want = case["expected"]
    assert got.strategy == want["strategy"]
    assert got.provider == want["provider"]
    assert dict(got.provider_config) == want["provider_config"]
    assert got.deterministic == want["deterministic"]
    assert got.review == want["review"]


def test_the_fixture_covers_each_branch_the_port_must_keep() -> None:
    ids = {c["id"] for c in CASES}
    buckets = {m["confidence_bucket"] for c in CASES for m in c["matches"]}
    assert {"high", "medium", "low", None} <= buckets
    for wanted in (
        "no-matches-string",
        "no-matches-integer-unique",
        "ambiguous-gap-0.09",
        "clear-gap-0.10",
        "clear-gap-0.15",
        "unknown-detector-high",
    ):
        assert wanted in ids
    cli_detectors = {
        c["matches"][0]["detector_id"] for c in CASES if c["id"].startswith("detector-")
    }
    assert cli_detectors <= set(DETECTOR_DESCRIPTORS)


def test_default_view_equals_its_base_fixture() -> None:
    got = {k: [v[0], v[1]] for k, v in DEFAULT_STRATEGY_BY_DETECTOR.items()}
    assert got == BASE_DEFAULTS


def test_default_view_is_derived_from_the_descriptors() -> None:
    derived = {
        detector_id: descriptor.default
        for detector_id, descriptor in DETECTOR_DESCRIPTORS.items()
        if descriptor.default is not None
    }
    assert derived == DEFAULT_STRATEGY_BY_DETECTOR
    assert set(DEFAULT_STRATEGY_BY_DETECTOR) == recommendations.known_detector_ids()


def test_every_descriptor_holds_a_default_or_a_starter_choice() -> None:
    for detector_id, descriptor in DETECTOR_DESCRIPTORS.items():
        assert descriptor.detector_id == detector_id
        assert descriptor.default is not None or descriptor.starter is not None


def test_a_starter_choice_comes_from_the_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing one descriptor's starter provider changes the decision (the port holds no copy)."""
    from dataclasses import replace

    original = DETECTOR_DESCRIPTORS["email"]
    case = next(c for c in CASES if c["id"] == "detector-email")
    assert original.starter is not None
    assert infer_starter_strategy(_stats(case)).provider == "person_email"
    changed = replace(original, starter=replace(original.starter, provider="person_email_changed"))
    monkeypatch.setattr(
        recommendations, "DETECTOR_DESCRIPTORS", {**DETECTOR_DESCRIPTORS, "email": changed}
    )
    assert infer_starter_strategy(_stats(case)).provider == "person_email_changed"


def test_the_port_keeps_no_detector_table() -> None:
    """M3-copy-table: `_starter_inference.py` never spells a detector id."""
    tree = ast.parse(Path(_starter_inference.__file__).read_text())
    spelled = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not spelled & set(DETECTOR_DESCRIPTORS)


def test_the_real_detector_match_has_no_confidence_bucket_attribute() -> None:
    """The CLI read `confidence_bucket`, which `DetectorMatch` does not carry (it has `confidence`).

    The port keeps that reading as it is (behavior-exact at 8fba6af), so on a real match only the
    0.5 match-rate rule makes it confident. A fix is its own change with a CHANGELOG line.
    """
    from decoy_engine.detection.types import DetectorMatch

    match = DetectorMatch(detector_id="email", match_rate=0.3, confidence="high")
    stats = SimpleNamespace(
        name="c", inferred_type="string", is_likely_unique=False, detector_matches=[match]
    )
    assert infer_starter_strategy(stats).strategy == "redact"

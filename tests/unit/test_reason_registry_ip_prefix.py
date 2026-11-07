"""ip_prefix: the seven reason identities are registered and pinned append-only."""

from __future__ import annotations

import json
from pathlib import Path

from decoy_engine import reasons

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.7-ip-prefix.json"
)
_EXPECTED = {
    ("plan", "ip_prefix_unknown_option"): "decoy_engine.transforms.network.ip_prefix",
    ("plan", "ip_prefix_keep_bits_invalid"): "decoy_engine.transforms.network.ip_prefix",
    ("plan", "ip_prefix_keeps_too_many_bits"): "decoy_engine.transforms.network.ip_prefix",
    ("plan", "ip_prefix_namespace_keep_bits_mismatch"): "decoy_engine.transforms.network.ip_prefix",
    ("plan", "ip_prefix_unsupported_in_nested"): "decoy_engine.transforms.network.ip_prefix",
    ("runtime", "ip_prefix_requires_namespace"): "StrategyError",
    ("runtime", "ip_prefix_config_invalid"): "StrategyError",
}


def test_ip_prefix_reasons_are_registered_with_their_owners() -> None:
    for (namespace, code), owner in _EXPECTED.items():
        spec = reasons.find(namespace, code)
        assert spec is not None, code
        assert spec.owner == owner


def test_ip_prefix_reasons_are_pinned_in_their_own_fixture() -> None:
    pinned = json.loads(_FIXTURE.read_text())
    live = reasons.manifest()
    assert pinned["reason_schema_version"] == live["reason_schema_version"]
    live_entries = sorted(
        (e for e in live["entries"] if (e["namespace"], e["code"]) in _EXPECTED),
        key=lambda e: (e["namespace"], e["code"]),
    )
    assert pinned["entries"] == live_entries
    assert len(live_entries) == len(_EXPECTED)

"""The wheel-smoke parity script duplicates the core's expected ABI because its CI env installs
only the companion wheel, not the core. A silent drift between the two would let a wheel built at the
wrong ABI pass smoke (the exact gap that shipped once: parity_smoke stayed abi-2 after the core moved
to abi-3). This test runs in the core-present regression gate and fails if they diverge.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from decoy_engine.execution.native._crypto_ext import _EXPECTED_ABI_VERSION as CORE_ABI


def _parity_smoke_expected_abi() -> str:
    script = (
        Path(__file__).resolve().parents[2] / "decoy-engine-native" / "scripts" / "parity_smoke.py"
    )
    spec = importlib.util.spec_from_file_location("parity_smoke", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._EXPECTED_ABI_VERSION)


def test_parity_smoke_abi_matches_core() -> None:
    assert _parity_smoke_expected_abi() == CORE_ABI, (
        "decoy-engine-native/scripts/parity_smoke.py _EXPECTED_ABI_VERSION drifted from "
        "decoy_engine.execution.native._crypto_ext._EXPECTED_ABI_VERSION; bump both together."
    )

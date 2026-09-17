"""D9 peak-RSS fix: the source-shaped reconstruction is invoked only after the
shadow coordinator returns, so the retained full-source pandas frame is built
past the coordinator's native-execution peak, never across it.

This asserts exactly that call ordering (`source_shaped_output` runs after
`ShadowCoordinator.run` returns), not the whole peak-lifetime contract. Admission
still converts the source to pandas transiently to validate it, but that frame is
released before the coordinator runs -- the admission test asserts the candidate
carries no frame, so a regression cannot stash a pre-built frame on it. The RSS
outcome itself is D9-cert-gated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution import _unified_slice_reconstruct
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from tests.physical.test_unified_slice_exception_boundary import (
    _mixed_admitted_fixture,
    _run,
)


def test_reconstruction_runs_after_the_coordinator_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    real_run = ShadowCoordinator.run

    def _run_recording(self: ShadowCoordinator, *args: Any, **kwargs: Any) -> Any:
        result = real_run(self, *args, **kwargs)
        events.append("coordinator-return")
        return result

    real_reconstruct = _unified_slice_reconstruct.source_shaped_output

    def _reconstruct_recording(*args: Any, **kwargs: Any) -> Any:
        events.append("reconstruct")
        return real_reconstruct(*args, **kwargs)

    # `_unified_slice.py` reaches the helper by module attribute, so patching the
    # attribute here is seen at its call site.
    monkeypatch.setattr(ShadowCoordinator, "run", _run_recording)
    monkeypatch.setattr(_unified_slice_reconstruct, "source_shaped_output", _reconstruct_recording)

    config, path = _mixed_admitted_fixture(tmp_path)
    _run(config, path, flag=True)

    assert events == ["coordinator-return", "reconstruct"]

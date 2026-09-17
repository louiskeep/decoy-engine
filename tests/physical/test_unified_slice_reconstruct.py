"""D9 peak-RSS fix: the source-shaped reconstruction must run AFTER the shadow
coordinator returns, so the full-source pandas rebuild never overlaps the
coordinator's native-execution peak. A refactor that rebuilt the frame before or
during native execution would stay correct (byte parity holds either way) while
silently reintroducing the peak-RSS overshoot the fix removed. This ordering test
is the guard that the peak-lifetime contract cannot regress unnoticed.
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

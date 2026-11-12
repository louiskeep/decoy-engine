"""RUN-STREAM E1b: `native_threads` reaches the auto-chunk lane and the clamp is recorded.

The Rust resolver lowers a request to `min(requested, host_available, knee)`. The engine asks the
companion for that figure (`resolve_native_threads`) and records it beside the lane stamp, so the
record is the kernel's own answer and the clamp rule has one owner.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution import _pipeline_auto_chunk as lane
from decoy_engine.execution.pipeline import _host
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution.test_auto_chunk_dispatcher import _hash_redact_job


def _fake_kernel(monkeypatch: pytest.MonkeyPatch, resolver: Any) -> None:
    kernel = types.SimpleNamespace(resolve_native_threads=resolver)
    package = types.ModuleType("decoy_engine_native")
    package._kernel = kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", package)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", kernel)


def test_applied_is_the_kernels_own_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whatever the kernel resolves is recorded as is; Python repeats no clamp arithmetic.
    _fake_kernel(monkeypatch, lambda requested: 3)
    assert _host.native_thread_clamp(6) == {"requested": 6, "applied": 3}


def test_a_kernel_refusal_records_no_applied_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_requested: int) -> int:
        raise ValueError("native_mask_thread_knee_invalid: bad override")

    _fake_kernel(monkeypatch, refuse)
    assert _host.native_thread_clamp(6) == {"requested": 6, "applied": None}


def test_a_companion_without_the_resolver_records_no_applied_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_kernel(monkeypatch, None)
    monkeypatch.delattr(sys.modules["decoy_engine_native._kernel"], "resolve_native_threads")
    assert _host.native_thread_clamp(6)["applied"] is None


def test_native_threads_reaches_the_lane_and_the_clamp_is_in_the_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_kernel(monkeypatch, lambda requested: 4)
    cfg, src = _hash_redact_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    result = support.run_default(cfg, src, native_threads=6)
    calls = spies["entry.run_mask_chunked"]
    assert [kwargs["native_threads"] for _args, kwargs in calls] == [6]
    block = result.quality_metrics["auto_chunk"]
    assert block["lane"] == "dispatcher"
    assert block["native_threads"] == 6
    assert block["native_thread_clamp"] == {"requested": 6, "applied": 4}


def test_the_clamp_is_computed_by_the_one_helper() -> None:
    assert lane.native_thread_clamp is _host.native_thread_clamp


def test_the_default_thread_request_stamps_no_clamp(tmp_path: Path) -> None:
    cfg, src = _hash_redact_job(tmp_path)
    block = support.run_default(cfg, src).quality_metrics["auto_chunk"]
    assert "native_thread_clamp" not in block

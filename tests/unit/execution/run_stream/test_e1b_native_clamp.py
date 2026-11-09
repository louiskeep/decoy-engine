"""RUN-STREAM E1b: `native_threads` reaches the auto-chunk lane and the clamp is recorded.

The Rust resolver lowers a request to `min(requested, host_available, knee)` and exposes no
accessor for the result, so the engine records the same arithmetic beside the lane stamp. The
record states the request, the usable CPUs, the knee and the applied value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution import _pipeline_auto_chunk as lane
from decoy_engine.execution.pipeline import _host
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution.test_auto_chunk_dispatcher import _hash_redact_job

_KNEE_ENV = "DECOY_NATIVE_MASK_THREAD_KNEE"


def _clamp(
    requested: int, *, cpus: int, knee: str | None, mp: pytest.MonkeyPatch
) -> dict[str, Any]:
    mp.setattr(_host, "detect_usable_cpus", lambda **_: cpus)
    if knee is None:
        mp.delenv(_KNEE_ENV, raising=False)
    else:
        mp.setenv(_KNEE_ENV, knee)
    return _host.native_thread_clamp(requested)


def test_request_above_the_default_knee_is_clamped_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got = _clamp(6, cpus=8, knee=None, mp=monkeypatch)
    assert got == {"requested": 6, "usable_cpus": 8, "knee": 4, "applied": 4}


def test_usable_cpus_below_the_knee_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _clamp(6, cpus=2, knee=None, mp=monkeypatch)["applied"] == 2


def test_request_below_both_ceilings_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _clamp(3, cpus=8, knee="16", mp=monkeypatch)["applied"] == 3


@pytest.mark.parametrize("raw", ["16", " 16 ", "+16"])
def test_knee_override_follows_the_rust_grammar(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    got = _clamp(12, cpus=32, knee=raw, mp=monkeypatch)
    assert (got["knee"], got["applied"]) == (16, 12)


@pytest.mark.parametrize(
    "raw", ["0", "1025", "-2", "four", "", "1_0", "\N{ARABIC-INDIC DIGIT ONE}"]
)
def test_invalid_knee_override_records_no_applied_value(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The kernel fails closed on such an override, so no figure is claimed.
    got = _clamp(6, cpus=8, knee=raw, mp=monkeypatch)
    assert (got["knee"], got["applied"]) == (None, None)


def test_native_threads_reaches_the_lane_and_the_clamp_is_in_the_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_host, "detect_usable_cpus", lambda **_: 8)
    monkeypatch.delenv(_KNEE_ENV, raising=False)
    cfg, src = _hash_redact_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    result = support.run_default(cfg, src, native_threads=6)
    calls = spies["entry.run_mask_chunked"]
    assert [kwargs["native_threads"] for _args, kwargs in calls] == [6]
    block = result.quality_metrics["auto_chunk"]
    assert block["lane"] == "dispatcher"
    assert block["native_threads"] == 6
    assert block["native_thread_clamp"] == {
        "requested": 6,
        "usable_cpus": 8,
        "knee": 4,
        "applied": 4,
    }


def test_the_clamp_is_computed_by_the_one_helper() -> None:
    assert lane.native_thread_clamp is _host.native_thread_clamp


def test_the_default_thread_request_stamps_no_clamp(tmp_path: Path) -> None:
    cfg, src = _hash_redact_job(tmp_path)
    block = support.run_default(cfg, src).quality_metrics["auto_chunk"]
    assert "native_thread_clamp" not in block

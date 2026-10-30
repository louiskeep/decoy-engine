"""F22 r5 HIGH 1: one owner disposes the parent's staged dataset on every non-commit exit.

The matrix drives the real `run_pipeline_isolated` (and the in-process fallback) down each
exit path and asserts the parent-assigned staging directory is gone afterwards, the staged
dataset cannot be reached through the result, and nothing was published.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import _isolated_commit as ic
from decoy_engine.execution._isolated_commit import StagingLease
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from tests.unit.execution import _r3_scenarios as sc
from tests.unit.execution.test_isolated_run import _install_fake_worker

FAIL = (pub.CheckResult("fk", "fail"),)
PREFIX = "_decoy_isolated_stage_"

_STAGE_THEN = """
import json, os, sys, time
payload = json.load(open(sys.argv[1]))
stage = payload["staging_output_dir"]
os.makedirs(stage)
open(os.path.join(stage, "a.parquet"), "wb").write(b"PAR1")
{tail}
"""


def _leftovers(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.glob(f"{PREFIX}*"))


def _assert_unreachable(result: object, parent: Path, out: Path) -> None:
    assert _leftovers(parent) == []
    assert not out.exists()
    assert getattr(result, "outputs", None) is None
    assert getattr(result, "committed_output_dir", None) is None


def _real(tmp_path: Path, **kw: object):  # type: ignore[no-untyped-def]
    cfg, tables = sc.two_tables(tmp_path)
    out = tmp_path / "final"
    result = run_pipeline_isolated(
        cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=out, **kw
    )
    return result, out


# ------------------------------------------------------------ real child, each exit path


def test_gate_refusal_on_a_required_check_disposes_the_staging(tmp_path: Path) -> None:
    result, out = _real(tmp_path, checks=FAIL)
    assert result.publication_receipt.reason_code == "required_check_failed"
    _assert_unreachable(result, tmp_path, out)


def test_gate_refusal_on_the_expected_tables_disposes_the_staging(tmp_path: Path) -> None:
    result, out = _real(tmp_path, expected_tables=["a"])
    assert result.publication_receipt.reason_code == "child_envelope_invalid"
    _assert_unreachable(result, tmp_path, out)


def test_cancel_before_commit_disposes_the_staging(tmp_path: Path) -> None:
    result, out = _real(tmp_path, cancel_check=lambda: True)
    assert result.publication_receipt.reason_code == "cancelled"
    _assert_unreachable(result, tmp_path, out)


def test_a_successful_publish_leaves_no_staging_and_the_result(tmp_path: Path) -> None:
    result, out = _real(tmp_path)
    assert result.publication_receipt.outcome == "published"
    assert _leftovers(tmp_path) == [] and out.is_dir()


# ------------------------------------------------------------ fake child, each exit path


def test_a_child_crash_disposes_the_staging(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _install_fake_worker(
        tmp_path, monkeypatch, "r5_crash", _STAGE_THEN.format(tail="os._exit(3)")
    )
    out = tmp_path / "final"
    result = run_pipeline_isolated({}, None, engine_version="r5", output_dir=out)
    assert result.outcome == "crashed"
    _assert_unreachable(result, tmp_path, out)


def test_a_timeout_disposes_the_staging(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _install_fake_worker(
        tmp_path, monkeypatch, "r5_hang", _STAGE_THEN.format(tail="time.sleep(60)")
    )
    out = tmp_path / "final"
    result = run_pipeline_isolated({}, None, engine_version="r5", output_dir=out, timeout_s=2.0)
    assert result.outcome == "crashed" and "timeout" in (result.error or "")
    _assert_unreachable(result, tmp_path, out)


def test_a_malformed_child_envelope_disposes_the_staging(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tail = (
        "from decoy_engine.execution._isolated_common import RESULT_FILENAME\n"
        "json.dump({'outcome': 'completed', 'preparation': {'schema': 'bogus'}},\n"
        "          open(os.path.join(os.path.dirname(sys.argv[1]), RESULT_FILENAME), 'w'))\n"
    )
    _install_fake_worker(tmp_path, monkeypatch, "r5_bad", _STAGE_THEN.format(tail=tail))
    out = tmp_path / "final"
    result = run_pipeline_isolated({}, None, engine_version="r5", output_dir=out)
    assert result.publication_receipt.reason_code == "child_envelope_invalid"
    _assert_unreachable(result, tmp_path, out)


def test_an_exception_out_of_on_spawn_disposes_the_staging(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fixed = tmp_path / f"{PREFIX}fixed"
    monkeypatch.setattr(ic, "sibling_staging_dir", lambda _out: fixed)
    _install_fake_worker(
        tmp_path, monkeypatch, "r5_wait", _STAGE_THEN.format(tail="time.sleep(60)")
    )

    def boom(_pid: int) -> None:
        deadline = time.monotonic() + 20
        while not fixed.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fixed.exists(), "the child never staged"
        raise RuntimeError("governor bug")

    out = tmp_path / "final"
    with pytest.raises(RuntimeError, match="governor bug"):
        run_pipeline_isolated({}, None, engine_version="r5", output_dir=out, on_spawn=boom)
    _assert_unreachable(None, tmp_path, out)


def test_an_exception_in_the_promotion_disposes_the_staging(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from decoy_engine.execution import _isolated_run as run

    def explode(*_a: object, **_k: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(run, "atomic_commit", explode)
    cfg, tables = sc.two_tables(tmp_path)
    out = tmp_path / "final"
    with pytest.raises(OSError, match="disk on fire"):
        run_pipeline_isolated(cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=out)
    _assert_unreachable(None, tmp_path, out)


# ------------------------------------------------------------ in-process fallback


def test_the_in_process_fallback_disposes_the_staging_on_cancel(tmp_path: Path) -> None:
    result, out = _real(tmp_path, isolate=False, cancel_check=lambda: True)
    assert result.publication_receipt.reason_code == "cancelled"
    _assert_unreachable(result, tmp_path, out)


def test_the_in_process_fallback_disposes_the_staging_when_staging_itself_fails(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from decoy_engine.execution import _isolated_run as run

    def half_write(outputs: object, staging: Path) -> None:
        staging.mkdir(parents=True)
        (staging / "a.parquet").write_bytes(b"PAR1")
        raise OSError("disk full")

    monkeypatch.setattr(run, "stage_tables", half_write)
    cfg, tables = sc.two_tables(tmp_path)
    out = tmp_path / "final"
    with pytest.raises(OSError, match="disk full"):
        run_pipeline_isolated(
            cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=out, isolate=False
        )
    _assert_unreachable(None, tmp_path, out)


def test_the_in_process_fallback_disposes_the_staging_on_a_gate_refusal(tmp_path: Path) -> None:
    result, out = _real(tmp_path, isolate=False, checks=FAIL)
    assert result.publication_receipt.reason_code == "required_check_failed"
    _assert_unreachable(result, tmp_path, out)


# ------------------------------------------------------------ the lease itself


def test_a_lease_disposes_a_directory_on_exit(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    (d / "x.parquet").write_bytes(b"x")
    with StagingLease(d):
        assert d.exists()
    assert not d.exists()


def test_a_lease_disposes_on_an_exception(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    with pytest.raises(KeyError), StagingLease(d):
        raise KeyError("x")
    assert not d.exists()


def test_disposal_is_idempotent_and_tolerates_an_absent_path(tmp_path: Path) -> None:
    lease = StagingLease(tmp_path / "never-made")
    lease.dispose()
    lease.dispose()
    assert not lease.path.exists()


def test_a_symlink_swapped_in_for_the_staging_is_unlinked_not_followed(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    d = tmp_path / "s"
    d.symlink_to(victim, target_is_directory=True)
    StagingLease(d).dispose()
    assert not os.path.lexists(d)
    assert (victim / "keep.txt").read_text() == "keep"


def test_a_lease_does_not_touch_a_directory_it_no_longer_owns_after_commit(tmp_path: Path) -> None:
    """Promotion renames the staging away: the lease then sees nothing at its path, and a
    result that now lives at the target is never deleted through the old name."""
    staging = tmp_path / "s"
    staging.mkdir()
    (staging / "t.parquet").write_bytes(b"x")
    target = tmp_path / "final"
    with StagingLease(staging):
        os.replace(staging, target)
    assert (target / "t.parquet").exists()


def test_dispose_from_two_threads_is_safe(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    for i in range(50):
        (d / f"{i}.parquet").write_bytes(b"x")
    lease = StagingLease(d)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            lease.dispose()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and not d.exists()

"""Parallel test runs: worker selection, xdist report scrubbing, and the short socket path."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
GB = 1024 * 1024  # kB per GiB, the unit /proc/meminfo uses


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "dev" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


workers = _load("_gate_workers")


def _choose(requested: str = "auto", nproc: int = 8, total: int = 16, avail: int = 12, cg=None):
    return workers.choose(requested, nproc, total * GB, avail * GB, cg)


@pytest.mark.parametrize(
    ("kwargs", "want"),
    [
        ({"nproc": 8, "total": 16}, 4),  # VM-class box: min(nproc, 4)
        ({"nproc": 2, "total": 16}, 2),
        ({"nproc": 1, "total": 16}, 1),
        ({"nproc": 8, "total": 8, "avail": 6}, 2),  # devbox-class: capped at 2
        ({"nproc": 1, "total": 8, "avail": 6}, 1),
        ({"nproc": 8, "total": 8, "avail": 3}, 1),  # devbox under memory pressure
        ({"nproc": 8, "total": 16, "cg": 6 * GB * 1024}, 2),  # a cgroup limit makes it small
        ({"nproc": 8, "total": 16, "cg": 6 * GB * 1024, "avail": 2}, 1),
    ],
)
def test_auto_worker_count(kwargs: dict[str, Any], want: int) -> None:
    count, why = _choose(**kwargs)
    assert count == want, why
    assert why


def test_explicit_worker_count_is_honoured_and_floored_at_one() -> None:
    assert _choose("3")[0] == 3
    assert _choose("0")[0] == 1
    assert _choose("1", total=4, avail=1)[0] == 1


def test_a_garbage_request_is_an_error() -> None:
    with pytest.raises(ValueError, match="GATES_WORKERS"):
        _choose("many")


def test_read_host_uses_fake_meminfo_and_cgroup(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        8388608 kB\nMemFree: 1 kB\nMemAvailable:   6291456 kB\n")
    cg = tmp_path / "memory.max"
    cg.write_text("max\n")
    assert workers.read_host(meminfo, cg) == (8388608, 6291456, None)
    cg.write_text("4294967296\n")
    assert workers.read_host(meminfo, cg)[2] == 4294967296
    assert workers.read_host(meminfo, tmp_path / "absent")[2] is None


def test_a_venv_without_xdist_resolves_to_one_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GATES_WORKERS", "4")
    monkeypatch.setattr(workers.importlib.util, "find_spec", lambda _name: None)
    assert workers.main() == 0
    printed = capsys.readouterr().out
    assert printed.startswith("1 ") and "pytest-xdist is not installed" in printed


# ---- xdist report scrubbing (AT-7) -----------------------------------------------------------


def _serializes(obj: Any) -> bool:
    execnet = pytest.importorskip("execnet")
    try:
        execnet.loads(execnet.dumps(obj))
    except Exception:
        return False
    return True


def test_a_report_with_a_lone_surrogate_does_not_serialize_until_scrubbed() -> None:
    from tests._xdist_safe_reports import scrub

    data = {
        "longrepr": {"lines": ["bad \ud800 text"]},
        "sections": [("Captured log call", "WARNING x:y.py:1 \ud800")],
        "nested": ("ok", ["\udfff"]),
    }
    assert not _serializes(data), "premise: execnet cannot encode a lone surrogate"
    cleaned = scrub(data)
    assert _serializes(cleaned)
    assert cleaned["longrepr"]["lines"] == ["bad \\ud800 text"]
    assert cleaned["sections"][0][1].endswith("\\ud800")
    assert data["nested"][1] == ["\udfff"], "scrub must not mutate its input"


def test_scrub_leaves_clean_values_alone() -> None:
    from tests._xdist_safe_reports import scrub

    data = {"a": 1, "b": [None, True, 2.5, "café \U0001f600"], "c": ("x",)}
    assert scrub(data) == data


def test_a_failing_test_that_logs_a_surrogate_runs_under_xdist(tmp_path: Path) -> None:
    pytest.importorskip("xdist")
    (tmp_path / "test_surrogate.py").write_text(
        textwrap.dedent(
            """\
            import logging

            def test_logs_surrogate():
                logging.getLogger("x").warning("lone \\ud800 here")
                assert False, "deliberate \\ud800"

            def test_ok():
                pass
            """
        )
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTEST_ADDOPTS": ""}
    proc = subprocess.run(  # noqa: S603 -- argv is literals plus this interpreter
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
            "-p", "tests._xdist_safe_reports", "-n", "2", "--rootdir", str(tmp_path),
            "-q", str(tmp_path / "test_surrogate.py"),
        ],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    out = proc.stdout + proc.stderr
    assert "INTERNALERROR" not in out, out
    assert proc.returncode == 1, out  # one deliberate failure, no crash
    assert "1 failed, 1 passed" in out, out


# ---- short socket paths (AT-12) --------------------------------------------------------------


def test_short_tmp_path_keeps_a_socket_path_under_the_af_unix_limit(
    short_tmp_path: Path, tmp_path: Path
) -> None:
    sock = short_tmp_path / "run" / "s.sock"
    assert len(str(sock).encode()) < 100
    assert not str(short_tmp_path).startswith(str(tmp_path))
    (short_tmp_path / "run").mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock))  # raises OSError "AF_UNIX path too long" for a long path
    finally:
        server.close()


@pytest.mark.skipif(sys.platform != "linux", reason="the policy tests are Linux file-access tests")
def test_the_socket_test_passes_under_xdist_with_a_very_long_basetemp(tmp_path: Path) -> None:
    pytest.importorskip("xdist")
    base = tmp_path / ("long-" + "x" * 120) / "basetemp"
    base.parent.mkdir()
    proc = subprocess.run(  # noqa: S603 -- literals plus this interpreter
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-v", "-n", "2",
            "-o", "addopts=", f"--basetemp={base}",
            "tests/fileaccess/test_policy_rooted.py", "tests/fileaccess/test_policy_portable.py",
        ],
        cwd=REPO, capture_output=True, text=True, timeout=900, check=False,
    )  # fmt: skip
    out = proc.stdout + proc.stderr
    assert "AF_UNIX path too long" not in out, out[-2000:]
    assert proc.returncode == 0, out[-2000:]
    for module in ("test_policy_rooted", "test_policy_portable"):
        needle = f"{module}.py::test_fifo_and_device_are_denied"
        assert any(needle in ln and "PASSED" in ln for ln in out.splitlines()), out[-2000:]

"""RUN-STREAM E1d: `detect_usable_cpus` over a fake /proc and cgroup tree.

Ported from the platform's `effective_cores` fixtures (`tests/test_b5_native_budget.py`), plus the
macOS shape (no `os.sched_getaffinity`, no /proc, no cgroup files) the platform never ran on.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

import decoy_engine
from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _duckdb
from decoy_engine.execution.pipeline._host import detect_usable_cpus


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _v2_mount(root: str = "/", mountpoint: str = "/sys/fs/cgroup") -> str:
    return f"36 35 0:30 {root} {mountpoint} rw,nosuid,nodev - cgroup2 cgroup2 rw\n"


def _v2(tmp_path: Path, membership: str, files: dict[str, str], *, mount: str | None = None):
    _write(tmp_path, "proc/self/cgroup", f"0::{membership}\n")
    _write(tmp_path, "proc/self/mountinfo", mount or _v2_mount())
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    return tmp_path


def _v1(tmp_path: Path, membership: str, files: dict[str, str], *, mounts: str | None = None):
    _write(tmp_path, "proc/self/cgroup", f"4:cpu,cpuacct:{membership}\n1:name=systemd:/x\n")
    _write(
        tmp_path,
        "proc/self/mountinfo",
        mounts or "40 35 0:35 / /sys/fs/cgroup/cpu,cpuacct rw - cgroup cgroup rw,cpu,cpuacct\n",
    )
    for rel, text in files.items():
        _write(tmp_path, rel, text)


def _cores(root: Path, affinity: int = 8) -> int:
    return detect_usable_cpus(fs_root=root, affinity=affinity)


def _refused(tmp_path: Path, match: str = "cpu_limit_unreadable") -> None:
    with pytest.raises(ExecutionError, match=match) as raised:
        _cores(tmp_path)
    assert raised.value.code == reasons.runtime.CPU_DETECTION_FAILED
    assert raised.value.__cause__ is None


def test_signature_is_the_injectable_keyword_only_one() -> None:
    params = inspect.signature(detect_usable_cpus).parameters
    assert [(p.name, p.kind, p.default) for p in params.values()] == [
        ("fs_root", inspect.Parameter.KEYWORD_ONLY, None),
        ("affinity", inspect.Parameter.KEYWORD_ONLY, None),
    ]
    assert decoy_engine.detect_usable_cpus is detect_usable_cpus
    assert "detect_usable_cpus" in decoy_engine.__all__


def test_v2_quota_200000_100000_gives_two(tmp_path: Path) -> None:
    _v2(tmp_path, "/worker", {"sys/fs/cgroup/worker/cpu.max": "200000 100000\n"})
    assert _cores(tmp_path) == 2


def test_v2_leaf_quota_floors_to_whole_cores(tmp_path: Path) -> None:
    _v2(tmp_path, "/worker", {"sys/fs/cgroup/worker/cpu.max": "150000 100000\n"})
    assert _cores(tmp_path) == 1


def test_v2_tighter_parent_wins(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/a/b",
        {"sys/fs/cgroup/a/b/cpu.max": "max 100000\n", "sys/fs/cgroup/a/cpu.max": "200000 100000\n"},
    )
    assert _cores(tmp_path) == 2


def test_v2_leaf_tighter_than_unlimited_parent(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/a/b",
        {"sys/fs/cgroup/a/b/cpu.max": "300000 100000\n", "sys/fs/cgroup/a/cpu.max": "max 100000\n"},
    )
    assert _cores(tmp_path) == 3


def test_v2_two_finite_quotas_take_the_smaller_whichever_level_holds_it(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/a/b",
        {
            "sys/fs/cgroup/a/b/cpu.max": "200000 100000\n",
            "sys/fs/cgroup/a/cpu.max": "300000 100000\n",
        },
    )
    assert _cores(tmp_path) == 2
    _write(tmp_path, "sys/fs/cgroup/a/b/cpu.max", "300000 100000\n")
    _write(tmp_path, "sys/fs/cgroup/a/cpu.max", "200000 100000\n")
    assert _cores(tmp_path) == 2


def test_v2_every_level_unlimited_uses_affinity(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/a/b",
        {
            "sys/fs/cgroup/a/b/cpu.max": "max 100000\n",
            "sys/fs/cgroup/a/cpu.max": "max 100000\n",
            "sys/fs/cgroup/cpu.max": "max 100000\n",
        },
    )
    assert _cores(tmp_path, affinity=8) == 8


def test_v2_malformed_ancestor_is_a_coded_error(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/a/b",
        {"sys/fs/cgroup/a/b/cpu.max": "max 100000\n", "sys/fs/cgroup/a/cpu.max": "abc\n"},
    )
    _refused(tmp_path)


@pytest.mark.parametrize("content", ["0 100000\n", "-5 100000\n", "100000 0\n", "100000\n", ""])
def test_v2_other_malformed_forms_are_a_coded_error(tmp_path: Path, content: str) -> None:
    _v2(tmp_path, "/a", {"sys/fs/cgroup/a/cpu.max": content})
    _refused(tmp_path)


def test_v2_present_but_unreadable_file_is_a_coded_error(tmp_path: Path) -> None:
    _v2(tmp_path, "/a/b", {"sys/fs/cgroup/a/b/cpu.max": "max 100000\n"})
    (tmp_path / "sys/fs/cgroup/a/cpu.max").mkdir(parents=True)  # present, not readable as text
    _refused(tmp_path, r"cpu_limit_unreadable \(path=")


def test_v2_level_without_the_file_is_skipped(tmp_path: Path) -> None:
    _v2(tmp_path, "/a/b", {"sys/fs/cgroup/a/cpu.max": "200000 100000\n"})
    (tmp_path / "sys/fs/cgroup/a/b").mkdir(parents=True)
    assert _cores(tmp_path) == 2


def test_affinity_below_quota_wins(tmp_path: Path) -> None:
    _v2(tmp_path, "/w", {"sys/fs/cgroup/w/cpu.max": "400000 100000\n"})
    assert _cores(tmp_path, affinity=2) == 2


def test_quota_below_one_core_floors_to_one(tmp_path: Path) -> None:
    _v2(tmp_path, "/w", {"sys/fs/cgroup/w/cpu.max": "50000 100000\n"})
    assert _cores(tmp_path) == 1


def test_no_cgroup_mount_uses_affinity(tmp_path: Path) -> None:
    _write(tmp_path, "proc/self/cgroup", "0::/w\n")
    _write(tmp_path, "proc/self/mountinfo", "22 1 0:21 / /proc rw - proc proc rw\n")
    assert _cores(tmp_path, affinity=6) == 6


def test_v2_non_root_mount_maps_membership_relative_to_mount_root(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/tenant/container/worker",
        {
            "sys/fs/cgroup/worker/cpu.max": "max 100000\n",
            "sys/fs/cgroup/cpu.max": "200000 100000\n",
            "sys/fs/cgroup/tenant/container/worker/cpu.max": "10000 100000\n",  # naive-join trap
        },
        mount=_v2_mount("/tenant/container"),
    )
    assert _cores(tmp_path) == 2


def test_v2_namespace_root_membership_reads_up_to_mountpoint(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/",
        {"sys/fs/cgroup/cpu.max": "300000 100000\n"},
        mount=_v2_mount("/tenant/container"),
    )
    assert _cores(tmp_path) == 3


def test_v2_membership_outside_mount_root_is_a_coded_error(tmp_path: Path) -> None:
    _v2(
        tmp_path,
        "/elsewhere/worker",
        {"sys/fs/cgroup/worker/cpu.max": "100000 100000\n"},
        mount=_v2_mount("/tenant/container"),
    )
    _refused(tmp_path)


def test_v2_two_matching_mounts_are_a_coded_error(tmp_path: Path) -> None:
    both = _v2_mount("/", "/sys/fs/cgroup") + _v2_mount("/", "/mnt/other")
    _v2(tmp_path, "/w", {"sys/fs/cgroup/w/cpu.max": "100000 100000\n"}, mount=both)
    _refused(tmp_path)


_V1 = "sys/fs/cgroup/cpu,cpuacct"
_V1_TENANT_MOUNT = (
    "40 35 0:35 /tenant/container /sys/fs/cgroup/cpu,cpuacct rw - cgroup cgroup rw,cpu,cpuacct\n"
)


def test_v1_unlimited_leaf_tight_parent(tmp_path: Path) -> None:
    _v1(
        tmp_path,
        "/a/b",
        {
            f"{_V1}/a/b/cpu.cfs_quota_us": "-1\n",
            f"{_V1}/a/b/cpu.cfs_period_us": "100000\n",
            f"{_V1}/a/cpu.cfs_quota_us": "100000\n",
            f"{_V1}/a/cpu.cfs_period_us": "100000\n",
        },
    )
    assert _cores(tmp_path) == 1


def test_v1_unlimited_quota_everywhere_uses_affinity(tmp_path: Path) -> None:
    _v1(
        tmp_path,
        "/a",
        {f"{_V1}/a/cpu.cfs_quota_us": "-1\n", f"{_V1}/a/cpu.cfs_period_us": "100000\n"},
    )
    assert _cores(tmp_path, affinity=8) == 8


def test_v1_malformed_ancestor_is_a_coded_error(tmp_path: Path) -> None:
    _v1(
        tmp_path,
        "/a/b",
        {
            f"{_V1}/a/b/cpu.cfs_quota_us": "-1\n",
            f"{_V1}/a/b/cpu.cfs_period_us": "100000\n",
            f"{_V1}/a/cpu.cfs_quota_us": "junk\n",
            f"{_V1}/a/cpu.cfs_period_us": "100000\n",
        },
    )
    _refused(tmp_path)


def test_v1_non_root_mount_maps_membership_relative_to_mount_root(tmp_path: Path) -> None:
    _v1(
        tmp_path,
        "/tenant/container/worker",
        {
            f"{_V1}/worker/cpu.cfs_quota_us": "-1\n",
            f"{_V1}/worker/cpu.cfs_period_us": "100000\n",
            f"{_V1}/cpu.cfs_quota_us": "200000\n",
            f"{_V1}/cpu.cfs_period_us": "100000\n",
            f"{_V1}/tenant/container/worker/cpu.cfs_quota_us": "10000\n",
            f"{_V1}/tenant/container/worker/cpu.cfs_period_us": "100000\n",
        },
        mounts=_V1_TENANT_MOUNT,
    )
    assert _cores(tmp_path) == 2


def test_v1_membership_outside_mount_root_is_a_coded_error(tmp_path: Path) -> None:
    _v1(
        tmp_path,
        "/elsewhere/worker",
        {f"{_V1}/worker/cpu.cfs_quota_us": "100000\n"},
        mounts=_V1_TENANT_MOUNT,
    )
    _refused(tmp_path)


def test_macos_shape_without_affinity_or_proc_uses_cpu_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    assert detect_usable_cpus(fs_root=tmp_path) == 10  # no /proc, no cgroup files: not an error


def test_macos_shape_with_unknown_cpu_count_uses_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert detect_usable_cpus(fs_root=tmp_path) == 1


def test_linux_shape_prefers_affinity_over_cpu_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2}, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    assert detect_usable_cpus(fs_root=tmp_path) == 3


def test_duckdb_thread_sizing_uses_the_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 3)
    assert _duckdb._threads_for_memory_limit("1048576MB") == 3  # 1 TiB: the CPU ceiling binds
    assert _duckdb._threads_for_memory_limit("4096MB") == 2  # 2 GiB per thread binds


def _detection_fails() -> int:
    raise ExecutionError(code=reasons.runtime.CPU_DETECTION_FAILED, message="cpu_limit_unreadable")


def test_thread_cap_survives_failed_cpu_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    # The documented `out_of_core_threads` workaround for an unreadable cgroup must not need the
    # detector at all.
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", _detection_fails)
    assert _duckdb._threads_for_memory_limit(None, thread_cap=4) == 4
    assert _duckdb._threads_for_memory_limit("4096MB", thread_cap=4) == 2  # memory term still binds


def test_failed_cpu_detection_surfaces_without_a_thread_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", _detection_fails)
    with pytest.raises(ExecutionError, match="cpu_limit_unreadable"):
        _duckdb._threads_for_memory_limit("4096MB")


def test_cgroup_membership_parsing_has_one_owner() -> None:
    from decoy_engine.execution.out_of_core import _budget
    from decoy_engine.execution.pipeline import _cgroup, _host

    assert _budget.parse_membership is _cgroup.parse_membership
    assert _host.parse_membership is _cgroup.parse_membership
    assert not hasattr(_host, "_parse_mountinfo") and not hasattr(_host, "_Mount")

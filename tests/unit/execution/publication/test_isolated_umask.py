"""`test_umask_0477_first_attempt_publishes`, isolated part (plan 5.7, 7.2 V1-b; slice c2).

Under umask `0o477` a plain new file is born `0o200`: unreadable to its owner. One attempt of
the isolated route, the child under the same umask, publishes: the parent writes the payload
sources owner-only, the real child stages owner-only files, and the parent sets each staged
entry it admits to `0600` (and the stage to `0700`) through the entry's own descriptor before it
measures, so the bound unit's files are `0600` and its directory `0700`. The second child is a
fake producer that leaves its table `0644` (readable, wider than a unit file may be): the
parent narrows it. A producer file its owner cannot read is refused, not repaired
(`test_isolated_parent_measures`). Run under uid 0 and uid 65534.

Red before c2 (start commit `f9054196`): the unit holds the child's files with the child's
modes and no descriptor, and the parent has no receipt.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.fileaccess.publication._dirmode_support import mode, run_child
from tests.unit.execution.publication._isolated_support import (
    FAKE_WORKER_SRC,
    job,
    umask_isolated_case,
)
from tests.unit.publication._publish_support import DESCRIPTOR, stages_left

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


@pytest.mark.parametrize("child", ["real", "plain_writer"])
def test_umask_0477_first_attempt_publishes(tmp_path: Path, child: str) -> None:
    base = tmp_path / "root"
    (base / "out").mkdir(parents=True)
    os.chmod(base, 0o755)  # noqa: S103 - the existing parents must be searchable
    os.chmod(base / "out", 0o755)  # noqa: S103
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(job(tmp_path, 3)), encoding="utf-8")
    spec: dict[str, object] = {"base": str(base), "cfg": str(cfg_path), "umask": 0o477}
    if child == "plain_writer":
        fake_dir = tmp_path / "_fake_workers"
        fake_dir.mkdir()
        (fake_dir / "f22_fake_worker.py").write_text(FAKE_WORKER_SRC, encoding="utf-8")
        spec["fake_dir"] = str(fake_dir)
    code, got = run_child(umask_isolated_case, spec, tmp_path)
    assert got["probe"] == 0o200  # non-vacuity: a plain new file loses owner read
    assert code == 0 and got["outcome"] == "completed", got
    assert got["receipt"] == "published", got
    leaf = base / "out" / "leaf"
    assert mode(leaf) == 0o700
    assert sorted(p.name for p in leaf.iterdir()) == [DESCRIPTOR, "t.parquet"]
    assert mode(leaf / "t.parquet") == 0o600
    assert mode(leaf / DESCRIPTOR) == 0o600
    assert stages_left(base / "out") == []

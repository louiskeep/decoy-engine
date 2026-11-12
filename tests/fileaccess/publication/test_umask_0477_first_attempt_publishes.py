"""F22 V1-b: under umask 0o477 a first attempt publishes; every stage directory is born usable.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 row
`test_umask_0477_first_attempt_publishes`, slice a1a part: the stage and a `create_dir`
subdirectory are 0700 on both POSIX policies; slice a1b part: under the publication label the
unit files are 0600 and every unit file and directory is fsynced before `publish()` returns (c2
adds the isolated route).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.fileaccess.publication._dirmode_support import (
    POLICIES,
    UNIT_BYTES,
    UNIT_FILE,
    mode,
    run_child,
    umask_stage_case,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")


@pytest.mark.parametrize("label", ["table_sink", "publication"])
@pytest.mark.parametrize("policy", POLICIES)
def test_umask_0477_first_attempt_publishes(tmp_path: Path, policy: str, label: str) -> None:
    base = tmp_path / "root"
    (base / "out").mkdir(parents=True)
    os.chmod(base, 0o755)  # noqa: S103 - the existing parents must be searchable
    os.chmod(base / "out", 0o755)  # noqa: S103
    spec = {"base": str(base), "policy": policy, "umask": 0o477, "label": label}
    code, got = run_child(umask_stage_case, spec, tmp_path)
    assert got["probe"] == 0o200  # non-vacuity: a plain new file loses owner read
    assert code == 0 and got["outcome"] == "published", got
    assert got["stage_mode"] == 0o700 and got["sub_mode"] == 0o700
    leaf = base / "out" / "leaf"
    if label == "publication":  # a1b: unit files 0600, every unit entry fsynced before publish
        assert mode(leaf / UNIT_FILE) == 0o600 and mode(leaf / "sub" / "g") == 0o600
        assert got["unit_keys"] and got["synced"] == got["unit_keys"]
        assert (leaf / UNIT_FILE).read_bytes() == UNIT_BYTES
        return
    # file modes are a1b; under this umask the unit file is born 0200, so its owner makes it
    # readable before the content check
    os.chmod(base / "out" / "leaf" / UNIT_FILE, 0o600)
    assert (base / "out" / "leaf" / UNIT_FILE).read_bytes() == UNIT_BYTES
    assert (base / "out" / "leaf" / "sub" / "g").exists()

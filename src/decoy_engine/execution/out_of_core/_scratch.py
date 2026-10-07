"""The out-of-core runner's scratch root: engine-created, or one a caller names (ambient only)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from decoy_engine import fileaccess


def scratch_root(temp_dir: Path | None) -> Path:
    """The private (0700) directory the runner spills into.

    With no `temp_dir` the engine makes its own. A directory the caller names is a caller path,
    which only the ambient policy follows, so any other policy is refused before any I/O."""
    if temp_dir is not None:
        fileaccess.require_ambient("run_fk_out_of_core with a caller-named temp_dir")
    root = Path(tempfile.mkdtemp(prefix="decoy-ooc-")) if temp_dir is None else temp_dir
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root

"""The one rule for a relative path written in a pipeline YAML.

A relative locator resolves from the folder that holds the YAML file; an absolute one is used as
given. Callers with no config file pass `base_dir=None` and keep today's behavior for relative
locators (working directory under the ambient policy, root-relative under a strict one).
Same shape as Docker Compose's project-directory rule for relative paths.
"""

from __future__ import annotations

import os
from pathlib import Path


def config_relative(locator: str, base_dir: str | Path | None) -> str:
    """Return `locator` joined onto `base_dir`, lexically, as an absolute `str`.

    `locator` comes back unchanged when `base_dir` is `None` or `locator` is absolute, so no
    file-system call happens on the no-config path. A relative `base_dir` means the working
    directory at the call.
    """
    if base_dir is None or os.path.isabs(locator):
        return locator
    return os.path.abspath(os.path.join(os.fspath(base_dir), locator))

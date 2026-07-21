"""Cache-directory resolution for ETL-produced corpora (HC-1 slice 2).

The engine has no existing cache-dir convention to reuse (checked:
``decoy_engine`` never reads ``XDG_CACHE_HOME`` or a ``~/.cache`` path
anywhere in ``src/``). This follows the standard XDG Base Directory
convention instead of inventing a bespoke one, consistent with CLAUDE.md's
"use established methodology" rule. ``DECOY_CODESET_CACHE_DIR`` is the
explicit override for callers (CI, a pinned platform deployment) that want a
fixed, non-default location.

Deliberately separate from ``src/decoy_engine/codesets/`` (the shipped-seed
directory): nothing under the cache dir is ever bundled into the wheel or
read by default. A pipeline opts in by pointing
``provider_config.corpus_source`` at ``customer:<cache_dir>/<name>.parquet``.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_cache_dir() -> Path:
    """Return the directory ETL-produced corpora are written to by default.

    Resolution order:
      1. ``$DECOY_CODESET_CACHE_DIR``, if set (explicit override).
      2. ``$XDG_CACHE_HOME/decoy-engine/codesets`` (XDG Base Directory spec).
      3. ``~/.cache/decoy-engine/codesets`` (XDG's own default when
         ``XDG_CACHE_HOME`` is unset -- the common case on Linux/macOS).

    Never created here -- callers (``_write.write_normalized_corpus``)
    create it lazily on first write, so merely resolving the path has no
    filesystem side effect.
    """
    override = os.environ.get("DECOY_CODESET_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg_base = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_base).expanduser() if xdg_base else Path.home() / ".cache"
    return base / "decoy-engine" / "codesets"

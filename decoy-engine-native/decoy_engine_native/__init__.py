"""Companion package for decoy-engine's compiled native masking kernels.

The compiled extension lives at ``decoy_engine_native._kernel`` (built by
maturin from ``src/lib.rs``); this file exists only to make the package
importable, so importing the submodule directly does not require re-exporting
anything here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # Sourced from installed package metadata (the maturin-built wheel's own
    # version, set once in pyproject.toml), never a duplicated literal here --
    # a hand-copied string would drift the moment one of the two changed.
    # Identity/provenance only: the ABI tag, not this version, is what the
    # loader enforces compatibility on (see docs/native/supported-matrix.md).
    __version__ = _version("decoy-engine-native")
except PackageNotFoundError:
    # Editable/unbuilt checkout (e.g. `python -c "import decoy_engine_native"`
    # against a source tree with no installed distribution record). Real
    # installs, including every CI wheel job, always resolve the branch above.
    __version__ = "0+unknown"

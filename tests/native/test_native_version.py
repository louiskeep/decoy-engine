"""Task 3.1 D3: decoy_engine_native.__version__ tracks installed package metadata.

`__version__` is sourced from `importlib.metadata.version("decoy-engine-native")`,
never a literal copied from `pyproject.toml`, so the two cannot drift. This is
identity/provenance only: the ABI tag (`test_native_ext_abi.py`), not the package
version, is what the loader enforces compatibility on.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util

import pytest
from packaging.version import InvalidVersion, Version

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)


@_NEEDS_COMPANION
def test_version_attribute_exists_and_matches_installed_metadata() -> None:
    import decoy_engine_native

    assert hasattr(decoy_engine_native, "__version__")
    assert decoy_engine_native.__version__ == importlib.metadata.version("decoy-engine-native")


@_NEEDS_COMPANION
def test_version_parses_as_pep_440() -> None:
    import decoy_engine_native

    try:
        Version(decoy_engine_native.__version__)
    except InvalidVersion:
        pytest.fail(
            f"__version__ {decoy_engine_native.__version__!r} is not a valid PEP 440 version"
        )

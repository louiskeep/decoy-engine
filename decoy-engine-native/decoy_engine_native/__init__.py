"""Companion package for decoy-engine's compiled native masking kernels.

The compiled extension lives at ``decoy_engine_native._kernel`` (built by
maturin from ``src/lib.rs``); this file exists only to make the package
importable, so importing the submodule directly does not require re-exporting
anything here.
"""

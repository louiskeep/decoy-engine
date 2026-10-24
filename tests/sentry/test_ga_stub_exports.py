"""Sentry test: the public API carries no stub exports.

consultant-2026-07-09 F4 flagged `SchemaInspector` (raised NotImplementedError)
and `LicenseVerifier` (always returned a free tier) as fake public exports that
could ship silently into GA. Both were hard-deleted pre-GA (parity E3a,
2026-10-24). This sentry keeps them, and any module with their shape, from
coming back as exports: a real implementation lands under a reviewed name and
plan, never by reviving these symbols.
"""

from __future__ import annotations

import importlib

import pytest

import decoy_engine

_DELETED_STUBS = ("SchemaInspector", "LicenseVerifier")
_DELETED_MODULES = ("decoy_engine.schema", "decoy_engine.license")


@pytest.mark.parametrize("name", _DELETED_STUBS)
def test_deleted_stub_is_not_exported(name: str) -> None:
    assert name not in decoy_engine.__all__
    assert not hasattr(decoy_engine, name)


@pytest.mark.parametrize("module", _DELETED_MODULES)
def test_deleted_stub_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)

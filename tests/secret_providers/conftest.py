from __future__ import annotations

import pytest

from decoy_engine.secret_providers import SecretProviderRegistry, _cache, _registry
from decoy_engine.secret_providers._local import EnvSecretProvider, FileSecretProvider


@pytest.fixture
def registry(monkeypatch):
    """A fresh default registry (env + file) the test may add providers to; restored after."""
    reg = SecretProviderRegistry((EnvSecretProvider(), FileSecretProvider()))
    monkeypatch.setattr(_registry, "_DEFAULT", reg)
    _cache.default_cache().clear()
    yield reg
    _cache.default_cache().clear()

"""`DECOY_CUSTOM_PROVIDERS_DIR` names the custom provider folder; the bare name is not read."""

from __future__ import annotations

from pathlib import Path

import pytest
from faker import Faker

from decoy_engine.internal import faker_setup


@pytest.fixture(autouse=True)
def _isolated_registry():
    before_fns = dict(faker_setup._CUSTOM_FAKER_PROVIDERS)
    before_vals = dict(faker_setup._CUSTOM_FAKER_PROVIDER_VALUES)
    yield
    faker_setup._CUSTOM_FAKER_PROVIDERS.clear()
    faker_setup._CUSTOM_FAKER_PROVIDER_VALUES.clear()
    faker_setup._CUSTOM_FAKER_PROVIDERS.update(before_fns)
    faker_setup._CUSTOM_FAKER_PROVIDER_VALUES.update(before_vals)
    Faker.seed(0)


def _folder(tmp_path: Path, stem: str) -> Path:
    folder = tmp_path / "words"
    folder.mkdir()
    (folder / f"{stem}.txt").write_text("alpha\nbeta\n")
    return folder


def test_the_prefixed_variable_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUSTOM_PROVIDERS_DIR", raising=False)
    monkeypatch.setenv("DECOY_CUSTOM_PROVIDERS_DIR", str(_folder(tmp_path, "prefixed_e3")))
    monkeypatch.chdir(tmp_path)
    loaded = faker_setup.load_custom_providers()
    assert "custom.prefixed_e3" in loaded


def test_the_old_unprefixed_variable_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DECOY_CUSTOM_PROVIDERS_DIR", raising=False)
    monkeypatch.setenv("CUSTOM_PROVIDERS_DIR", str(_folder(tmp_path, "legacy_e3")))
    monkeypatch.chdir(tmp_path)
    assert faker_setup.load_custom_providers() == {}

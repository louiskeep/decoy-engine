"""AT-V13: the writer built from a config carries the admitted key version."""

from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.vault import open_vault, vault_writer_for_config
from tests.unit.vault._support import prepared


@pytest.mark.no_default_secret
def test_programmatic_provider_key_version_is_stamped(tmp_path: Path) -> None:
    provider = SecretKeyProvider(b"decoy-test-config-secret-0123456789", key_version="v7")
    writer = vault_writer_for_config({"global_settings": {"seed": 42}}, key_provider=provider)
    writer.add([("ns", "m", "s")])
    path = tmp_path / "v.vault"
    writer.write(path)
    opened = open_vault(path, prepared(provider.mask_key(), "v7"))
    assert opened.header.key_version == "v7"
    assert opened.mapping == {("ns", "m"): "s"}

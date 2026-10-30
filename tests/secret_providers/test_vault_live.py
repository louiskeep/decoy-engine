"""Live Vault KV v2 acceptance. Runs only when a real Vault dev server is provided.

  DECOY_TEST_VAULT_ADDR   e.g. http://127.0.0.1:8200   (a `vault server -dev` instance)
  DECOY_TEST_VAULT_TOKEN  its root token (used ONLY to provision fixtures; the workload client
                          below authenticates with a least-privilege token)

Without them this module is skipped and the check is recorded as NOT RUN (never as a pass).
"""

from __future__ import annotations

import base64
import os

import pytest

hvac = pytest.importorskip("hvac")

ADDR = os.environ.get("DECOY_TEST_VAULT_ADDR")
ROOT = os.environ.get("DECOY_TEST_VAULT_TOKEN")
pytestmark = pytest.mark.skipif(
    not (ADDR and ROOT), reason="live Vault not provided (DECOY_TEST_VAULT_ADDR/TOKEN)"
)

from decoy_engine import run_mask_pipeline_chunked  # noqa: E402
from decoy_engine.keyprovider import MaskSecretError  # noqa: E402
from decoy_engine.secret_providers.vault import VaultKV2Provider  # noqa: E402
from tests.secret_providers._helpers import (  # noqa: E402
    ENGINE_VERSION,
    TouchedSource,
    keyed_config,
)

MOUNT = "decoy-f13"
POLICY = f"""
path "{MOUNT}/data/app" {{ capabilities = ["read"] }}
"""


@pytest.fixture(scope="module")
def vault():
    admin = hvac.Client(url=ADDR, token=ROOT)
    if f"{MOUNT}/" not in admin.sys.list_mounted_secrets_engines()["data"]:
        admin.sys.enable_secrets_engine("kv", path=MOUNT, options={"version": "2"})
    key = base64.b64encode(b"L" * 40).decode()
    kv = admin.secrets.kv.v2
    for _ in range(3):
        kv.create_or_update_secret(path="app", secret={"key": key}, mount_point=MOUNT)
    kv.create_or_update_secret(path="other", secret={"key": key}, mount_point=MOUNT)
    admin.sys.create_or_update_policy("f13-reader", POLICY)
    token = admin.auth.token.create(policies=["f13-reader"], ttl="10m")["auth"]["client_token"]
    return admin, hvac.Client(url=ADDR, token=token)


def ref(path="app", version=3, field="key"):
    return f"vault://{MOUNT}/{path}?version={version}&field={field}&encoding=base64"


def run(vault_pair, registry, mask_ref, tmp_path):
    registry.register(VaultKV2Provider(vault_pair[1]))
    source = TouchedSource()
    cfg = keyed_config(tmp_path, mask_secret_ref=mask_ref)
    out = list(run_mask_pipeline_chunked(cfg, source, table="t", engine_version=ENGINE_VERSION))
    return out, source


def test_exact_version_read_masks_deterministically(vault, registry, tmp_path):
    out, source = run(vault, registry, ref(), tmp_path)
    assert out and source.touched > 0


@pytest.mark.parametrize(
    "mask_ref, code",
    [
        (ref(path="other"), "secret_access_denied"),  # policy grants read on app only
        (ref(version=99), "secret_version_unavailable"),
        (ref(field="nope"), "secret_provider_unavailable"),
    ],
)
def test_denied_missing_and_malformed_fail_before_any_read(
    vault, registry, tmp_path, mask_ref, code
):
    registry.register(VaultKV2Provider(vault[1]))
    source = TouchedSource()
    cfg = keyed_config(tmp_path, mask_secret_ref=mask_ref)
    with pytest.raises(MaskSecretError) as info:
        list(run_mask_pipeline_chunked(cfg, source, table="t", engine_version=ENGINE_VERSION))
    assert info.value.code == code and source.touched == 0


def test_deleted_and_destroyed_versions_are_unavailable_and_no_write_privilege(
    vault, registry, tmp_path
):
    admin, workload = vault
    admin.secrets.kv.v2.delete_secret_versions(path="app", versions=[1], mount_point=MOUNT)
    admin.secrets.kv.v2.destroy_secret_versions(path="app", versions=[2], mount_point=MOUNT)
    registry.register(VaultKV2Provider(workload))
    for version in (1, 2):
        cfg = keyed_config(tmp_path, mask_secret_ref=ref(version=version))
        with pytest.raises(MaskSecretError) as info:
            list(
                run_mask_pipeline_chunked(
                    cfg, TouchedSource(), table="t", engine_version=ENGINE_VERSION
                )
            )
        assert info.value.code == "secret_version_unavailable"
    with pytest.raises(hvac.exceptions.Forbidden):  # the workload token cannot write or list
        workload.secrets.kv.v2.create_or_update_secret(
            path="app", secret={"key": "x"}, mount_point=MOUNT
        )

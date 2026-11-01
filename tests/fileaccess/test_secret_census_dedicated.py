"""Census entries whose failure needs a real bad input, not a bomb: no engine frame keeps the key.

The three round-5 sites (the pack-signing key in `_check_signature`, the master in
`derive_pack_signing_key`, the signing key in `sign_pack` on a corrupt manifest), the census
constructions the bomb cells cannot reach (a closure cell wiped by its enclosing frame, a
dataclass validated after its generated `__init__`, a SECRET read failing part way, an
environment secret that cannot be encoded) and the vault writer under a policy that refuses it.
Each walks every `decoy_engine` frame on every chain link (`census_support.leaks`).
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess, vault
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.profiling.model_pack import provenance
from decoy_engine.profiling.model_pack.loader import ModelPackLoader, ModelPackLoadError
from decoy_engine.profiling.model_pack.types import ModelPackManifest
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.secret_providers._local import EnvSecretProvider
from decoy_engine.secret_providers._types import SecretRef
from tests.fileaccess.census_support import engine_frames, leaks
from tests.fileaccess.support import RootRelative

KEY = b"R6-SIGNING-CANARY-" + b"q" * 14  # 32 bytes
MASTER_SHORT = b"R6-MASTER-CANARY-short"  # an invalid (non-32-byte) master


def _manifest(**overrides: Any) -> ModelPackManifest:
    fields: dict[str, Any] = dict(
        pack_id="p",
        version="0.1.0",
        sha256="abc",
        eval_report_hash="def",
        training_seed=1,
        calibration_method="sigmoid",
        operating_threshold=0.5,
    )
    fields.update(overrides)
    return ModelPackManifest(**fields)


# --- the three round-5 sites -------------------------------------------------------------------


@pytest.mark.parametrize("stored", ["", "a" * 64], ids=["unsigned", "tampered"])
def test_check_signature_refusal_keeps_no_signing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    monkeypatch.setenv(provenance.SIGNING_KEY_ENV, KEY.hex())
    loader = ModelPackLoader(tmp_path)
    with pytest.raises(ModelPackLoadError) as info:
        loader._check_signature(_manifest(manifest_hmac=stored))
    assert leaks(info.value, KEY) == []
    assert leaks(info.value, KEY.hex()) == []


def test_check_signature_refuses_an_unparseable_key_without_keeping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "R6-ENV-CANARY-not-hex-" + "z" * 10
    monkeypatch.setenv(provenance.SIGNING_KEY_ENV, text)
    with pytest.raises(ModelPackLoadError) as info:
        ModelPackLoader(tmp_path)._check_signature(_manifest(manifest_hmac="a" * 64))
    assert leaks(info.value, text) == []


def test_verify_manifest_with_a_corrupt_stored_mac_keeps_no_key_or_expected_mac() -> None:
    manifest = _manifest(manifest_hmac="x")
    expected = provenance.sign_manifest(manifest, KEY)
    object.__setattr__(manifest, "manifest_hmac", 12345)  # a corrupt manifest field
    with pytest.raises(TypeError) as info:
        provenance.verify_manifest(manifest, KEY)
    assert leaks(info.value, KEY) == []
    assert leaks(info.value, expected) == []  # the MAC that would sign this manifest


@pytest.mark.parametrize("master", [MASTER_SHORT, MASTER_SHORT.decode()], ids=["bytes", "str"])
def test_derive_pack_signing_key_refusal_keeps_no_master(master: bytes | str) -> None:
    with pytest.raises(ValueError) as info:
        provenance.derive_pack_signing_key(master)  # type: ignore[arg-type]
    assert leaks(info.value, MASTER_SHORT) == []


@pytest.mark.parametrize("content", [b"{not json", b'{"pack_id": 1}'], ids=["json", "fields"])
def test_sign_pack_on_a_corrupt_manifest_keeps_no_key(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "manifest.json").write_bytes(content)
    with pytest.raises((ValueError, TypeError, KeyError)) as info:
        provenance.sign_pack(tmp_path, KEY)
    assert leaks(info.value, KEY) == []


def test_load_signing_key_from_env_keeps_nothing_when_logging_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short = b"R6-SHORT-KEY"  # under 16 bytes: the warning branch runs with the key live
    monkeypatch.setenv(provenance.SIGNING_KEY_ENV, short.hex())

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("a logging filter failed")

    monkeypatch.setattr(provenance._log, "warning", boom)
    with pytest.raises(RuntimeError) as info:
        provenance.load_signing_key_from_env()
    assert leaks(info.value, short) == []
    assert leaks(info.value, short.hex()) == []


# --- constructions the bomb cells cannot reach -------------------------------------------------


def test_a_closure_cell_is_cleared_by_its_enclosing_frame() -> None:
    from decoy_engine.execution.native import _kernels_scalar

    def failing(*_: object, **__: object) -> Any:
        raise RuntimeError("kernel absent and the oracle failed")

    import pyarrow as pa

    original = _kernels_scalar.mask_cell
    _kernels_scalar.mask_cell = failing  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as info:
            _kernels_scalar.native_text_mask(
                pa.array(["call 555-867-5309"]),
                mask_key=KEY,
                column="c",
                detectors=None,
                per_detector_strategy=None,
                unmatched_span_policy="redact",
                token="[X]",
                date_shift_bounds=(),
                sub_floor_span_policy=None,
            )
    finally:
        _kernels_scalar.mask_cell = original  # type: ignore[assignment]
    names = [function for function, _ in engine_frames(info.value)]
    # The closure frame is on the chain (`co_qualname` from 3.11, `co_name` before).
    assert any(n == "mask" or n.endswith(".mask") for n in names), names
    assert leaks(info.value, KEY) == []


def test_a_validated_after_init_dataclass_refuses_without_the_generated_frame() -> None:
    with pytest.raises(ValueError) as info:
        ShadowContext(mask_key=KEY, batch_size_rows=0)
    assert leaks(info.value, KEY) == []
    with pytest.raises(ProviderError) as info2:
        ProviderSpec(locale=None, deterministic=False, namespace=None, seed=KEY[:16])
    assert leaks(info2.value, KEY[:16]) == []


def test_validate_after_init_keeps_the_signature_and_runs_after_init() -> None:
    import dataclasses
    import inspect

    from decoy_engine._opaque import validate_after_init

    seen: list[str] = []

    @validate_after_init
    @dataclasses.dataclass(frozen=True)
    class Box:
        secret_key: bytes = dataclasses.field(repr=False)
        n: int = 1

        def _validate(self) -> None:
            seen.append("validated")
            if self.n < 1:
                raise ValueError("n")

    assert list(inspect.signature(Box).parameters) == ["secret_key", "n"]
    assert dataclasses.replace(Box(b"x"), n=2).n == 2 and seen == ["validated", "validated"]
    with pytest.raises(ValueError) as info:
        Box(KEY, n=0)
    assert leaks(info.value, KEY) == []


def test_a_secret_read_failing_part_way_keeps_no_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.fileaccess import _read

    (tmp_path / "root").mkdir()
    policy = RootRelative(tmp_path / "root")
    path = tmp_path / "root" / "s.key"
    path.write_bytes(KEY * 4096)  # several reads
    path.chmod(0o600)
    real_pread = os.pread
    calls = {"n": 0}

    def pread(fd: int, n: int, offset: int) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EIO, "injected")
        return real_pread(fd, n, offset)

    monkeypatch.setattr(_read.os, "pread", pread)
    try:
        with fileaccess.scope(policy), pytest.raises(OSError) as info:
            _read.read_checked(
                fileaccess.FileRole.SECRET,
                str(path),
                "global_settings.mask_secret_ref",
                lambda _info: None,
            )
    finally:
        policy.close()
    assert calls["n"] == 2
    assert leaks(info.value, KEY) == []


def test_an_env_secret_that_cannot_be_encoded_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "R6-ENV-SECRET-CANARY-\udcff"  # a lone surrogate: utf-8 encoding fails
    monkeypatch.setattr(os, "environ", {**os.environ, "DECOY_R6_ENV": text})
    with pytest.raises(UnicodeEncodeError) as info:
        EnvSecretProvider()._read(SecretRef(scheme="env", locator="DECOY_R6_ENV"))
    assert leaks(info.value, "R6-ENV-SECRET-CANARY-") == []


def test_vault_writer_refused_by_the_installed_policy_keeps_no_key(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    policy = RootRelative(tmp_path / "root")
    try:
        with fileaccess.scope(policy), pytest.raises(fileaccess.FileAccessUnavailable) as info:
            vault.VaultWriter(KEY, file_access=fileaccess.LocalFileAccess())
    finally:
        policy.close()
    assert leaks(info.value, KEY) == []


def test_the_dedicated_cells_cover_what_the_bomb_cells_cannot() -> None:
    census = json.loads(
        (
            Path(__file__).resolve().parents[2] / "tests/sentry/secret_material_census.json"
        ).read_text(encoding="utf-8")
    )["entries"]
    names = {(e["function"], e["name"]) for e in census}
    for site in [
        ("ModelPackLoader._check_signature", "key_box"),
        ("derive_pack_signing_key", "master"),
        ("sign_pack", "key"),
        ("VaultWriter.__init__", "mask_key"),
        ("native_text_mask.mask", "mask_key"),
        ("ShadowContext", "mask_key"),
        ("ProviderSpec", "seed"),
    ]:
        assert site in names, site

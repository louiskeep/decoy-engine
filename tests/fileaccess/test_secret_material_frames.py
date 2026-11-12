"""No public engine entry keeps secret material in a frame a failing call's traceback keeps.

`TracebackException(capture_locals=True)` (and error trackers that capture locals) render every
local of every frame on the chain. Each cell seeds a failure after the entry holds or derived the
key and checks every non-test frame on every chain link (engine, library and generated frames)
for the raw secret and every key derived from it: the local's repr and each str/bytes nested in
tuples, lists, sets and dicts. Paths, refs and configs in entry frames are out of this file's
scope (F31 revision 3).
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import context as _context
from decoy_engine import determinism, keyprovider, vault
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.secret_providers import REQUIREMENTS, SecretBuffer
from tests.fileaccess.locals_support import _strings

_TESTS = os.sep + "tests" + os.sep

SECRET = b"R4-SECRET-CANARY-" + b"s" * 23  # 40 bytes, an admissible mask secret
MASTER = b"R4-MASTER-CANARY-" + b"m" * 15  # 32 bytes, a resolver master
VAULT_KEY = b"R4-VAULTKEY-CANARY-" + b"v" * 13  # 32 bytes, a mask root handed to the vault
OTHER_KEY = b"R4-OTHERKEY-CANARY-" + b"o" * 13
SHORT = b"R4-SHORT-CANARY"
ENV = "DECOY_R4_FRAME_SECRET"


def _all_locals(exc: BaseException) -> Iterator[tuple[str, str, str]]:
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        link = stack.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        stack.extend([link.__cause__, link.__context__])
        tb = link.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if _TESTS not in frame.f_code.co_filename:
                for name, value in frame.f_locals.items():
                    try:
                        shown = repr(value)
                    except Exception:
                        shown = ""
                    yield frame.f_code.co_name, name, shown
                    for text in _strings(value):
                        yield frame.f_code.co_name, name, text
            tb = tb.tb_next


def _assert_no_frame_holds(exc: BaseException, *secrets: bytes) -> None:
    # Raw (a str/bytes local), hex, and the escaped form a container's repr prints it in.
    needles = [form for s in secrets for form in (s.decode("latin1"), s.hex(), repr(s)[2:-1])]
    for function, name, text in _all_locals(exc):
        for needle in needles:
            assert needle not in text, f"{function}() local {name!r} holds a secret"


def _mask_root(secret: bytes = SECRET) -> bytes:
    return SecretKeyProvider(secret).mask_key()


def _raise(*_: object, **__: object) -> Any:
    raise RuntimeError("seeded failure")


@pytest.fixture
def env_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(ENV, SECRET.hex())
    return f"env:{ENV}"


def _keyed_config(ref: str, **extra: Any) -> dict[str, Any]:
    return {
        "global_settings": {"seed": 7, "mask_secret_ref": ref},
        "tables": [{"name": "t", "columns": [{"name": "e", "strategy": "hash", "namespace": "e"}]}],
        **extra,
    }


# --- vault ------------------------------------------------------------------------------------


def _vault_derived(key: bytes) -> list[bytes]:
    fernet_key = determinism.derive(key, vault.VAULT_NAMESPACE, vault.VAULT_KEY_LABEL)
    return [key, fernet_key, base64.urlsafe_b64encode(fernet_key)]


def test_load_vault_without_a_handle(tmp_path: Path) -> None:
    with pytest.raises(Exception) as info:
        vault.load_vault(None, VAULT_KEY)
    _assert_no_frame_holds(info.value, *_vault_derived(VAULT_KEY))


def test_load_vault_missing_file(tmp_path: Path) -> None:
    with pytest.raises(vault.VaultError) as info:
        vault.load_vault(str(tmp_path / "absent.vault"), VAULT_KEY)
    _assert_no_frame_holds(info.value, *_vault_derived(VAULT_KEY))


def test_load_vault_wrong_key(tmp_path: Path) -> None:
    path = tmp_path / "v.vault"
    writer = vault.VaultWriter(OTHER_KEY)
    writer.add([("ns", "m", "s")])
    writer.write(str(path))
    with pytest.raises(vault.VaultError) as info:
        vault.load_vault(str(path), VAULT_KEY)
    assert info.value.code == "vault_key_mismatch"
    _assert_no_frame_holds(info.value, *_vault_derived(VAULT_KEY), *_vault_derived(OTHER_KEY))


def test_fernet_construction_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cryptography.fernet

    monkeypatch.setattr(cryptography.fernet, "Fernet", _raise)
    with pytest.raises(RuntimeError) as info:
        vault.load_vault(str(tmp_path / "absent.vault"), VAULT_KEY)
    _assert_no_frame_holds(info.value, *_vault_derived(VAULT_KEY))


def test_vault_writer_construction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault.fileaccess, "capture", _raise)
    with pytest.raises(RuntimeError) as info:
        vault.VaultWriter(VAULT_KEY)
    _assert_no_frame_holds(info.value, VAULT_KEY)


def test_vault_writer_key_mismatch() -> None:
    with pytest.raises(vault.VaultError) as info:
        vault.VaultWriter(VAULT_KEY).assert_keyed_with(OTHER_KEY)
    _assert_no_frame_holds(info.value, VAULT_KEY, OTHER_KEY)


@pytest.mark.parametrize("writer", ["duck", "other_key"])
def test_assert_vault_writer_keyed(writer: str) -> None:
    candidate = object() if writer == "duck" else vault.VaultWriter(OTHER_KEY)
    with pytest.raises(vault.VaultError) as info:
        vault.assert_vault_writer_keyed(candidate, VAULT_KEY)
    _assert_no_frame_holds(info.value, VAULT_KEY, OTHER_KEY)


def test_vault_writer_for_config(env_secret: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault.fileaccess, "capture", _raise)
    with pytest.raises(RuntimeError) as info:
        vault.vault_writer_for_config(_keyed_config(env_secret))
    _assert_no_frame_holds(info.value, SECRET, _mask_root())


# --- unmask -----------------------------------------------------------------------------------


def test_unmask_pipeline_missing_vault(env_secret: str, tmp_path: Path) -> None:
    from decoy_engine.unmask import unmask_pipeline

    with pytest.raises(Exception) as info:
        unmask_pipeline(_keyed_config(env_secret), {}, vault_path=str(tmp_path / "absent.vault"))
    _assert_no_frame_holds(info.value, SECRET, _mask_root(), *_vault_derived(_mask_root()))


def test_unmask_pipeline_fpe_failure(env_secret: str) -> None:
    from decoy_engine.unmask import unmask_pipeline

    config = _keyed_config(env_secret)
    config["tables"] = [{"name": "t", "columns": [{"name": "c", "strategy": "fpe"}]}]
    with pytest.raises(Exception) as info:
        unmask_pipeline(config, {"t": pa.table({"c": ["123456"]})})
    _assert_no_frame_holds(info.value, SECRET, _mask_root())


# --- execution entries ------------------------------------------------------------------------


def test_run_fk_out_of_core(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.out_of_core import _runner
    from tests.parity import _d1a_hash_data as d

    monkeypatch.setattr(_runner, "_stream_table", _raise)
    payload = pa.table({"c": pa.array(["a", "b"], pa.string())})
    with pytest.raises(RuntimeError) as info:
        d.run_chain(payload, d.hash_seeds(["c"]), key_provider=SecretKeyProvider(SECRET))
    _assert_no_frame_holds(info.value, SECRET, _mask_root())


def test_run_sequential() -> None:
    from decoy_engine.execution import PandasExecutionAdapter
    from tests.parity import _d2a_seq_data as s

    with pytest.raises(RuntimeError) as info:
        PandasExecutionAdapter().run_sequential(
            s.chain_plan(s.hash_seeds(["c"])),
            _raise,
            config={},
            registry=s.REGISTRY,
            relationship_graph=s.chain_graph(),
            namespace_registry=s.NAMESPACES,
            key_provider=SecretKeyProvider(SECRET),
        )
    _assert_no_frame_holds(info.value, SECRET, _mask_root())


def test_strategy_context_repr_hides_the_mask_key() -> None:
    from decoy_engine.execution._adapter import StrategyContext

    ctx = StrategyContext(
        registry=None,  # type: ignore[arg-type]
        pool_cache=None,  # type: ignore[arg-type]
        relationship_graph=None,  # type: ignore[arg-type]
        namespace_registry=None,  # type: ignore[arg-type]
        job_seed=b"\x01" * 8,
        mask_key=VAULT_KEY,
    )
    assert "VAULTKEY" not in repr(ctx)


# --- key helpers ------------------------------------------------------------------------------


def test_make_key_resolver_bad_master() -> None:
    with pytest.raises(ValueError) as info:
        _context.make_key_resolver(MASTER[:31], "p")
    _assert_no_frame_holds(info.value, MASTER[:31])


class _BadLabel:
    def __format__(self, spec: str) -> str:
        raise RuntimeError("label failed")


def test_make_key_resolver_label_formatting_fails() -> None:
    with pytest.raises(RuntimeError) as info:
        _context.make_key_resolver(MASTER, _BadLabel())  # type: ignore[arg-type]
    _assert_no_frame_holds(info.value, MASTER)


def test_make_key_resolver_bad_label() -> None:
    resolver = _context.make_key_resolver(MASTER, "p")
    pipeline_key = _context._hkdf_sha256(MASTER, "pipeline:p")
    with pytest.raises(AttributeError) as info:
        resolver(b"not-a-str")  # type: ignore[arg-type]
    _assert_no_frame_holds(info.value, MASTER, pipeline_key)
    assert resolver("col:e") == _context._hkdf_sha256(pipeline_key, "col:e")


class _ShortKey:
    key_version = "v1"

    def mask_key(self) -> bytes:
        return SHORT


def _keyed_plan() -> Any:
    from tests.parity import _d1a_hash_data as d

    return d.chain_plan(d.hash_seeds(["c"]))


@pytest.mark.parametrize("entry", ["require_mask_key", "resolve_mask_key"])
def test_mask_key_gate_with_a_short_key(entry: str) -> None:
    plan = _keyed_plan()
    with pytest.raises(keyprovider.KeyedStrategyRequiresSecret) as info:
        if entry == "require_mask_key":
            keyprovider.require_mask_key(plan, _ShortKey())
        else:
            keyprovider.resolve_mask_key(plan=plan, key_provider=_ShortKey())
    _assert_no_frame_holds(info.value, SHORT)


def test_secret_key_provider_weak_secret() -> None:
    with pytest.raises(keyprovider.WeakMaskSecret) as info:
        SecretKeyProvider(SHORT)
    _assert_no_frame_holds(info.value, SHORT)


class _BoomDomain:
    def from_bytes(self, b: bytes) -> Any:
        raise RuntimeError("domain failed")


DERIVE_CASES: dict[str, Callable[[], Any]] = {
    "derive_empty_namespace": lambda: determinism.derive(VAULT_KEY, "", b"x"),
    "derive_bad_source": lambda: determinism.derive(VAULT_KEY, "ns", "not-bytes"),  # type: ignore[arg-type]
    "derive_index_empty_namespace": lambda: determinism.derive_index(
        VAULT_KEY, "", b"x", pool_size=5
    ),
    "derive_index_bad_pool": lambda: determinism.derive_index(VAULT_KEY, "ns", b"x", pool_size=0),
    "derive_value_domain_fails": lambda: determinism.derive_value(
        VAULT_KEY, "ns", b"x", domain=_BoomDomain()
    ),
}


@pytest.mark.parametrize("case", sorted(DERIVE_CASES))
def test_derive_failures(case: str) -> None:
    with pytest.raises(Exception) as info:
        DERIVE_CASES[case]()
    _assert_no_frame_holds(info.value, VAULT_KEY)


def test_derive_index_checks_the_pool_size_before_the_seed() -> None:
    with pytest.raises(determinism.DeterminismError) as info:
        determinism.derive_index(b"short", "ns", b"x", pool_size=0)
    assert info.value.code == "pool_size_invalid"


# --- secret_providers exports -----------------------------------------------------------------

PREPARE_CASES = {
    "decoy-mask-root-v1": SHORT,
    "fernet-v1": b"R4-FERNET-CANARY-" + b"f" * 27,
    "hmac-sha256-v1": SHORT,
    "aes-gcm-v1": b"R4-AESGCM-CANARY-x",
}


@pytest.mark.parametrize("requirement", sorted(PREPARE_CASES))
def test_requirement_prepare_refusal(requirement: str) -> None:
    material = PREPARE_CASES[requirement]
    with pytest.raises(Exception) as info:
        REQUIREMENTS[requirement].prepare(material, "v1")
    assert info.value.__context__ is None
    _assert_no_frame_holds(info.value, material)


def test_secret_buffer_bad_type() -> None:
    with pytest.raises(TypeError) as info:
        SecretBuffer("R4-BUFFER-CANARY")  # type: ignore[arg-type]
    for function, name, text in _all_locals(info.value):
        assert "R4-BUFFER-CANARY" not in text, f"{function}() local {name!r}"


def test_the_prepared_values_are_unchanged() -> None:
    fernet_key = base64.urlsafe_b64encode(b"k" * 32)
    assert REQUIREMENTS["fernet-v1"].prepare(b"  " + fernet_key + b"\n", "v1") == fernet_key
    assert REQUIREMENTS["aes-gcm-v1"].prepare(b"k" * 16, "v1") == b"k" * 16
    assert REQUIREMENTS["hmac-sha256-v1"].prepare(b"h" * 32, "v1") == b"h" * 32
    assert REQUIREMENTS["decoy-mask-root-v1"].prepare(SECRET, "v1") == _mask_root()
    assert (
        hashlib.sha256(bytes(SecretBuffer(b"abc").view())).hexdigest()
        == hashlib.sha256(b"abc").hexdigest()
    )


# --- secret_providers.vault.client_from_env ---------------------------------------------------

TOKEN = "R4-VAULT-TOKEN-CANARY"

CLIENT_ENV_CASES = {
    "no_address": {"VAULT_TOKEN": TOKEN},
    "skip_verify": {"VAULT_TOKEN": TOKEN, "VAULT_ADDR": "https://v", "VAULT_SKIP_VERIFY": "1"},
    "half_mtls": {"VAULT_TOKEN": TOKEN, "VAULT_ADDR": "https://v", "VAULT_CLIENT_CERT": "c.pem"},
}


@pytest.mark.parametrize("case", sorted(CLIENT_ENV_CASES))
def test_client_from_env_refusal(case: str) -> None:
    from decoy_engine.secret_providers.vault import VaultClientConfigError, client_from_env

    with pytest.raises(VaultClientConfigError) as info:
        client_from_env(dict(CLIENT_ENV_CASES[case]))
    for function, name, text in _all_locals(info.value):
        assert TOKEN not in text, f"{function}() local {name!r} holds the token"


def test_client_from_env_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.secret_providers.vault import VaultClientConfigError, client_from_env

    monkeypatch.setenv("VAULT_TOKEN", TOKEN)
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_SKIP_VERIFY", raising=False)
    with pytest.raises(VaultClientConfigError) as info:
        client_from_env()
    for function, name, text in _all_locals(info.value):
        assert TOKEN not in text, f"{function}() local {name!r} holds the token"

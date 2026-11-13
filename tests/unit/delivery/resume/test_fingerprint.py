"""AT-F1 and AT-F2: the run fingerprint and its key check."""

from __future__ import annotations

import copy
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from decoy_engine.delivery.resume import RunFingerprint, binding_digest, build_fingerprint

_COMPONENTS = (
    ("decoy-cli", "1.0.0"),
    ("decoy-engine", "0.23.0"),
    ("faker", "40.23.0"),
    ("numpy", "2.1.0"),
    ("pandas", "2.2.3"),
    ("pyarrow", "18.0.0"),
)


@dataclass
class _Provider:
    """A provider with a fixed mask key, so the known-answer vector is independent of HKDF."""

    key: bytes
    key_version: str = "v1"

    def mask_key(self) -> bytes:
        return self.key


_KEY = _Provider(bytes(range(32)))
_LABEL = b"decoy-resume-key-check/v1"


def _config() -> dict[str, Any]:
    return {
        "name": "p",
        "global_settings": {"seed": 7},
        "sources": {"t1": {"path": "in/t1.csv", "format": "csv"}},
        "targets": {"t1": {"path": "out/t1.csv", "format": "csv"}},
        "tables": {"t1": {"columns": {"a": {"strategy": "hash"}}}},
    }


def _fp(
    config: dict[str, Any] | None = None,
    *,
    provider: Any = _KEY,
    chunk_size: int = 1000,
    components: tuple[tuple[str, str], ...] = _COMPONENTS,
) -> RunFingerprint:
    return build_fingerprint(
        config or _config(), provider, chunk_size=chunk_size, components=components
    )


def test_fields() -> None:
    base = _fp()
    assert _fp().fingerprint_digest == base.fingerprint_digest

    changed_cfg = _config()
    changed_cfg["tables"]["t1"]["columns"]["a"]["strategy"] = "redact"
    other_key = _Provider(bytes(range(1, 33)))
    variants = {
        "config_hash": _fp(changed_cfg),
        "key_check": _fp(provider=other_key),
        "chunk_size": _fp(chunk_size=500),
    }
    for i in range(len(_COMPONENTS)):
        bumped = list(_COMPONENTS)
        bumped[i] = (bumped[i][0], bumped[i][1] + ".1")
        variants[f"component:{_COMPONENTS[i][0]}"] = _fp(components=tuple(bumped))
    for name, fp in variants.items():
        assert fp.fingerprint_digest != base.fingerprint_digest, name

    # A binding alone is not a run-level field: it changes only that table's digest.
    moved = copy.deepcopy(_config())
    moved["sources"]["t1"]["path"] = "in/other.csv"
    assert _fp(moved).fingerprint_digest == base.fingerprint_digest
    src, tgt = _config()["sources"]["t1"], _config()["targets"]["t1"]
    assert binding_digest(src, tgt) == binding_digest(dict(src), dict(tgt))
    assert binding_digest({**src, "path": "in/other.csv"}, tgt) != binding_digest(src, tgt)
    assert binding_digest(src, {**tgt, "format": "parquet"}) != binding_digest(src, tgt)
    assert binding_digest(src, tgt) != binding_digest(tgt, src)


def test_components_are_sorted_and_order_free() -> None:
    assert _fp(components=tuple(reversed(_COMPONENTS))).fingerprint_digest == (
        _fp().fingerprint_digest
    )


def test_key_check() -> None:
    expected = hmac.new(bytes(range(32)), _LABEL, hashlib.sha256).hexdigest()[:16]
    fp = _fp()
    assert fp.key_check == expected == "8ad0efba9e645120"
    assert len(fp.key_check) == 16
    assert _fp(provider=_Provider(bytes(range(1, 33)))).key_check != fp.key_check


def test_key_check_seed_path() -> None:
    from decoy_engine.plan import job_seed_for_config

    cfg = _config()
    fp = _fp(cfg, provider=None)
    seed = job_seed_for_config(cfg)
    assert fp.key_check == hmac.new(seed, _LABEL, hashlib.sha256).hexdigest()[:16]
    assert fp.key_version == "seed"
    assert _fp(provider=_Provider(bytes(32), "v9")).key_version == "v9"


def test_repr_has_no_key_bytes() -> None:
    text = repr(_fp()) + str(_fp())
    assert bytes(range(32)).hex() not in text
    assert repr(bytes(range(32))) not in text

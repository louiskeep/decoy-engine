"""AT-K2: the resume key check value is frozen; the move to `key_check_value` changed no byte."""

from __future__ import annotations

from dataclasses import dataclass

from decoy_engine.delivery.resume import build_fingerprint

# Computed on ad36687a6, before the extraction: hmac_hex(bytes(range(32)), label)[:16].
_PINNED = "8ad0efba9e645120"


@dataclass
class _Provider:
    key: bytes
    key_version: str = "v1"

    def mask_key(self) -> bytes:
        return self.key


def test_resume_key_check_value_is_pinned() -> None:
    config = {
        "name": "p",
        "global_settings": {"seed": 7},
        "sources": {"t1": {"path": "in/t1.csv", "format": "csv"}},
        "targets": {"t1": {"path": "out/t1.csv", "format": "csv"}},
        "tables": {"t1": {"columns": {"a": {"strategy": "hash"}}}},
    }
    fingerprint = build_fingerprint(
        config, _Provider(bytes(range(32))), chunk_size=10, components=(("decoy-cli", "1.0.0"),)
    )
    assert fingerprint.key_check == _PINNED

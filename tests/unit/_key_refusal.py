"""A key value the join's token functions refuse, for tests of the `key_type_mismatch` path.

No real column value makes `fk_join_key` raise, so the one string `UNENCODABLE` does, in the
key scan only. Every other value takes the real token functions."""

from __future__ import annotations

import pytest

from decoy_engine.internal.fk_tokens import fk_join_key
from decoy_engine.profiling import _key_loading

UNENCODABLE = "unencodable"


def refuse_unencodable(monkeypatch: pytest.MonkeyPatch) -> None:
    def encode(value: object) -> str:
        if value == UNENCODABLE:
            raise ValueError("refused")
        return fk_join_key(value)

    monkeypatch.setattr(_key_loading, "fk_join_key", encode)

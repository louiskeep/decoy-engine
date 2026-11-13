from __future__ import annotations

import pytest

from tests.unit._key_refusal import refuse_unencodable


@pytest.fixture(autouse=True)
def _refuse_unencodable_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    refuse_unencodable(monkeypatch)

"""The seed fingerprint form is byte-frozen: no seed, pool seed or generated output may move.

Every hex below was captured from the engine before `_canonical` existed, with non-ASCII
content in the configs, so a switch of either seed-feeding caller to the UTF-8 digest form
turns these red.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine._canonical import CanonicalJsonError
from decoy_engine.generation.pool._builder import _config_hash, _derive_pool_seed
from decoy_engine.generators.derivation import strategy_config_fingerprint

_COLUMNS: list[tuple[dict[str, Any], str]] = [
    (
        {"type": "categorical", "categories": ["a", "b"], "weights": [1, 2]},
        "0df81fb081e3d993b2d58d90f66d4c31c055df5f4944acf5794dcde7477432dc",
    ),
    (
        {"type": "faker", "provider": "name", "locale": "fr_FR", "note": "café 日本"},
        "14b3b15ec2fd615d262793507a61772d2e35f1a901c6af1d538dbb85d6bac7c6",
    ),
    (
        {
            "type": "faker",
            "provider": "name",
            "extra": {"kéy": "vü", "n": [1, 2.5, None, True]},
        },
        "11f01a881ac80a220dbaeed0632c295365c92007a62a6b4b0644b7dd034f8b91",
    ),
]


def test_strategy_config_fingerprint_keeps_its_bytes_for_non_ascii_configs() -> None:
    for config, expected in _COLUMNS:
        assert strategy_config_fingerprint(config) == expected


def test_the_pool_config_hash_keeps_its_bytes_for_non_ascii_extras() -> None:
    extras = {"préfix": "üé", "n": 3, "l": [1, "x"]}
    assert _config_hash(extras) == (
        "8243128644dc3d11d4ef215c8d94a2201ab33507f65196fee1d7a785e8a1097d"
    )
    assert _config_hash(None) == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert _config_hash({"a": 1}) == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )


def test_the_pool_seed_for_non_ascii_extras_does_not_move() -> None:
    extras = {"préfix": "üé", "n": 3, "l": [1, "x"]}
    seed = _derive_pool_seed(b"\x01" * 32, "faker", "en_US", None, _config_hash(extras))
    assert seed.hex() == "9cd7953010f5e74b"


def test_both_seed_callers_refuse_an_unpaired_surrogate_instead_of_escaping_it() -> None:
    bad = "mid\ud800dle"
    with pytest.raises(CanonicalJsonError) as first:
        strategy_config_fingerprint({"type": "faker", "note": bad})
    with pytest.raises(CanonicalJsonError) as second:
        _config_hash({"note": bad})
    for caught in (first, second):
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "mid" not in str(caught.value)

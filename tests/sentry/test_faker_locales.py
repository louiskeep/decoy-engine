"""The advertised Faker locale table matches the installed Faker.

Re-runs the generator's probe and compares data (not file text), so a Faker
upgrade that changes locale support fails until the table is regenerated.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import faker

from decoy_engine.providers_v2 import _faker_locales, get_default_registry

GENERATOR = Path(__file__).resolve().parents[2] / "scripts" / "gen_faker_locales.py"


def _load_generator():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("gen_faker_locales", GENERATOR)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_table_matches_the_installed_faker() -> None:
    version, locales, unsupported = _load_generator().build_table()
    assert _faker_locales.FAKER_VERSION == faker.VERSION == version
    assert locales == _faker_locales.ALL_LOCALES
    assert {p: frozenset(ls) for p, ls in unsupported.items()} == _faker_locales.UNSUPPORTED


def test_every_faker_row_advertises_the_table() -> None:
    rows = get_default_registry().faker_providers()
    assert rows
    for cap in rows:
        assert cap.supported_locales == _faker_locales.supported_locales(cap.provider)
        assert "en_US" in cap.supported_locales

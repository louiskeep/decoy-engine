"""Dark: configs without entity_key are byte-identical to engine main, and nothing defaults it."""

from __future__ import annotations

from pathlib import Path

import yaml

from decoy_engine import strategy_catalog
from decoy_engine.execution._column_access import SIBLING_REFERENCE_KEYS, column_access
from decoy_engine.providers_v2._registry import get_default_registry
from tests.unit.transforms.date_shift_entity._dark_capture import capture

# Captured by running `_dark_capture.capture` against the src tree of engine main b5b1ad16.
_PINNED_FROM_MAIN_B5B1AD16: dict[str, dict[str, str]] = {
    "group_by": {
        "checks_passed": "ef1baf00e330022f3ade66b2040ee0e84ed38f56d8f6d0b6f77ce25a69b8644e",
        "metrics": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "output": "fff7263a5fe4dd2dc552cd6cde01b11dca5d3fc9b66d62040432cd44440510e7",
        "plan": "9c6fb48bb7135d4d6c88094b67c03cf6e6cffd98c80012ede2c6114765d1a745",
    },
    "mixed": {
        "checks_passed": "ef1baf00e330022f3ade66b2040ee0e84ed38f56d8f6d0b6f77ce25a69b8644e",
        "metrics": "2689ba48fbbcef6a55b3f3df4616e0ea6257e78d449785984cfc0f743614c3df",
        "output": "0020efb5f0d2551e8a3ebcdf02787b3bc97a8f38095dfafb4df4983657b7c7f4",
        "plan": "84634a7e4d88fa8f2d1a341ad75a69a7f8aaed8e12be28ca021aff41a4ae5530",
    },
    "plain": {
        "checks_passed": "ef1baf00e330022f3ade66b2040ee0e84ed38f56d8f6d0b6f77ce25a69b8644e",
        "metrics": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "output": "9a3159cfa242a48924114d86ab242570ccc1db56d164d941cd774af0e3c13db4",
        "plan": "83fd55173da7b0c202782e74fd827fdbe48228bccae9031fc201719b3526977a",
    },
}


def test_configs_without_entity_key_match_main(tmp_path: Path) -> None:
    assert capture(tmp_path) == _PINNED_FROM_MAIN_B5B1AD16


def test_wizard_row_is_unchanged() -> None:
    row = next(r for r in strategy_catalog() if r.name == "date_shift")
    assert [o.name for o in row.options] == ["min_days", "max_days", "date_format"]
    assert row.offer_in_wizard is True


def test_no_shipped_yaml_sets_entity_key() -> None:
    root = Path(__file__).resolve().parents[4] / "src" / "decoy_engine"
    for path in root.rglob("*.yaml"):
        assert "entity_key" not in path.read_text(encoding="utf-8"), path
        yaml.safe_load(path.read_text(encoding="utf-8"))


def test_column_access_reads_the_entity_column_for_date_shift_only() -> None:
    reg = get_default_registry()
    ds = {"name": "d", "strategy": "date_shift", "provider_config": {"entity_key": "pid"}}
    assert column_access(ds, reg).reads == frozenset({"pid"})
    stray = {"name": "h", "strategy": "hash", "provider_config": {"entity_key": "pid"}}
    assert column_access(stray, reg).reads == frozenset()
    bad = {"name": "d", "strategy": "date_shift", "provider_config": {"entity_key": 3}}
    assert column_access(bad, reg).reads == frozenset()
    assert SIBLING_REFERENCE_KEYS == ("group_by", "order_by", "anchor", "reference_column")


def test_transform_source_references_include_the_entity_column() -> None:
    from decoy_engine.execution._transforms import _structured_references

    table = {
        "name": "t",
        "columns": [
            {"name": "d", "strategy": "date_shift", "provider_config": {"entity_key": "pid"}},
            {"name": "e", "strategy": "date_shift", "provider_config": {"group_by": "gid"}},
            {"name": "k", "strategy": "group_key", "provider_config": {"group_by": "hh"}},
        ],
    }
    assert {"pid", "gid", "hh"} <= _structured_references(table, {"tables": [table]})

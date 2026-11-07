"""V1 behaviour unchanged by the Avro/ORC formats (plan V1P-4.1, AT-D1 to AT-D5).

Expected values were captured from main `b5b1ad16` by running `tests/unit/io/_dark_cases.py`
against that tree's source."""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path
from typing import Any

from tests.unit.io import _dark_cases

_MAIN = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "v1plus_p41" / "main-b5b1ad16.json"
    ).read_text()
)


def _captured(tmp_path: Path) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _dark_cases.capture(tmp_path)


def test_configs_profiles_and_plans_unchanged(tmp_path: Path) -> None:
    now = _captured(tmp_path)
    assert now["configs"] == _MAIN["configs"]  # AT-D1
    assert now["profiles"] == _MAIN["profiles"]  # AT-D2
    assert now["plans"] == _MAIN["plans"]  # AT-D3


def test_admission_sets_unchanged() -> None:  # AT-D4
    from decoy_engine.declared_casting._eligibility import BYTE_PRESERVING_TARGETS
    from decoy_engine.execution._unified_slice_admission import _ADMITTED_SOURCE_FORMATS
    from decoy_engine.plan._checks_declared_casting import _BINARY_SOURCE_FORMATS

    assert frozenset({"parquet", "csv", "fixed_width"}) == _ADMITTED_SOURCE_FORMATS
    assert frozenset({"parquet", "memory", None}) == BYTE_PRESERVING_TARGETS
    assert frozenset({"parquet"}) == _BINARY_SOURCE_FORMATS


def _strip_new_formats(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _strip_new_formats(v) for k, v in node.items()}
        if isinstance(out.get("enum"), list):
            out["enum"] = [v for v in out["enum"] if v not in ("avro", "orc")]
        return out
    if isinstance(node, list):
        return [_strip_new_formats(v) for v in node]
    return node


def _enum_paths(node: Any, path: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list) and {"avro", "orc"} <= set(node["enum"]):
            found.add(path)
        for k, v in node.items():
            found |= _enum_paths(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found |= _enum_paths(v, f"{path}/{i}")
    return found


def test_json_schemas_differ_only_in_the_format_enums(tmp_path: Path) -> None:  # AT-D5
    now = _captured(tmp_path)
    for key in ("config_schema", "run_result_schema"):
        assert _strip_new_formats(copy.deepcopy(now[key])) == _MAIN[key], key
    assert _enum_paths(now["config_schema"]) == {
        "/$defs/FileSource/properties/format",
        "/$defs/FileTarget/properties/format",
    }
    assert len(_enum_paths(now["run_result_schema"])) == 1

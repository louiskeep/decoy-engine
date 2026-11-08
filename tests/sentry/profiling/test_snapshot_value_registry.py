"""Every key a distribution snapshot can carry is classified once, and every value rule bites.

The snapshot registry sits beside the profile one in `_value_redaction.py`. A new stats key
or a sixth column kind fails here until a decision is written down, and the structural guard
keeps the snapshot path from growing a second sensitivity rule or a second applier.
"""

from __future__ import annotations

import ast
import enum
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from decoy_engine.profiling import _value_redaction as vr
from decoy_engine.profiling import distribution_snapshot_document
from decoy_engine.profiling._value_redaction import (
    SNAPSHOT_KINDS,
    SNAPSHOT_VALUE_FREE_STATS,
    SNAPSHOT_VALUE_STATS,
    Rule,
)
from decoy_engine.quality import snapshot as snapshot_module
from decoy_engine.quality.snapshot import compute_distribution_snapshot

_SRC = Path(__file__).parents[3] / "src" / "decoy_engine"
_N = 40
_SSNS = [f"{101 + i:03d}-{23 + i:02d}-{4501 + i:04d}" for i in range(25)]
_BALANCE = [31337.25, *[40000.5 + 1000 * i for i in range(_N - 2)], 98765.5]
_PAIRS = [("ssn", "email"), ("status", "balance")]


def _frame() -> pd.DataFrame:
    """One column of each snapshot kind, an HC-5 candidate, and two joints."""
    return pd.DataFrame(
        {
            "ssn": [_SSNS[i % 25] for i in range(_N)],
            "email": [f"user{i % 25}@example.com" for i in range(_N)],
            "balance": _BALANCE,
            "admitted": pd.date_range("1991-06-01", periods=_N, freq="D"),
            "status": ["open", "closed"] * (_N // 2),
            "notes": [f"free text number {i} with a long tail {i * 7919}" for i in range(_N)],
            "blank": [None] * _N,
            "code": [f"c{i:03d}" for i in range(_N)],
        }
    )


def _snapshot(**kw: Any) -> dict[str, Any]:
    return compute_distribution_snapshot(_frame(), joint_columns=_PAIRS, **kw)


def _visited(snap: dict[str, Any]) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()

    def see(scope: str, node: dict[str, Any]) -> dict[str, Any]:
        seen.update((scope, key) for key in node)
        return node

    see("document", snap)
    for col in snap["columns"].values():
        see("column", col)
        stats = see(col["kind"], col["stats"])
        for item in stats.get("year_bins", []):
            see("year_bin", item)
        if "length" in stats:
            see("length", stats["length"])
    for joint in snap["joints"]:
        see("joint", joint)
    return seen


def _kinds_seen(snap: dict[str, Any]) -> set[str]:
    return {col["kind"] for col in snap["columns"].values()}


def _unclassified(snap: dict[str, Any]) -> set[tuple[str, str]]:
    return _visited(snap) - set(SNAPSHOT_VALUE_STATS) - SNAPSHOT_VALUE_FREE_STATS


def _kinds_in_source(source: str) -> set[str]:
    kinds: set[str] = set()
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                fn.name == "_stats_for"
                and isinstance(node, ast.Return)
                and isinstance(node.value, ast.Tuple)
                and isinstance(node.value.elts[0], ast.Constant)
            ):
                kinds.add(node.value.elts[0].value)
            if (
                fn.name == "_column_snapshot"
                and isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "kind" for t in node.targets)
                and isinstance(node.value, ast.Constant)
            ):
                kinds.add(node.value.value)
    return kinds


# AT-10


def test_the_registries_are_disjoint() -> None:
    assert set(SNAPSHOT_VALUE_STATS) & SNAPSHOT_VALUE_FREE_STATS == set()


def test_the_four_snapshot_rules_join_the_one_rule_enum() -> None:
    assert {"coarse_range", "empty_map", "previous_year_start", "next_year_end"} <= {
        r.value for r in Rule
    }
    enums = [v for v in vars(vr).values() if isinstance(v, type) and issubclass(v, enum.Enum)]
    assert enums == [Rule]


def test_every_visited_key_is_classified_and_every_entry_is_visited() -> None:
    snap = _snapshot(high_cardinality_columns=["code"])
    assert _kinds_seen(snap) == SNAPSHOT_KINDS
    assert _unclassified(snap) == set()
    assert _visited(snap) == set(SNAPSHOT_VALUE_STATS) | SNAPSHOT_VALUE_FREE_STATS


def test_the_kinds_equal_the_kinds_the_snapshot_source_can_emit() -> None:
    source = Path(snapshot_module.__file__).read_text(encoding="utf-8")
    assert _kinds_in_source(source) == SNAPSHOT_KINDS


def test_a_new_stats_key_is_reported_unclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    real = snapshot_module.numeric_stats

    def widened(*a: Any, **kw: Any) -> dict[str, Any]:
        return {**real(*a, **kw), "mode": 1.0}

    monkeypatch.setattr(snapshot_module, "numeric_stats", widened)
    assert ("numeric", "mode") in _unclassified(_snapshot())


def test_a_sixth_kind_literal_is_seen_by_the_source_read() -> None:
    source = Path(snapshot_module.__file__).read_text(encoding="utf-8")
    patched = source.replace(
        'return "freetext", _freetext_stats', 'return "geo", _freetext_stats', 1
    )
    assert patched != source
    assert _kinds_in_source(patched) - SNAPSHOT_KINDS == {"geo"}


# AT-11


def _containers(doc: dict[str, Any], scope: str, names: set[str]) -> list[dict[str, Any]]:
    if scope == "joint":
        return list(doc["joints"])
    return [c["stats"] for n, c in doc["columns"].items() if c["kind"] == scope and n in names]


def _numbers(node: Any) -> set[float]:
    if isinstance(node, bool):
        return set()
    if isinstance(node, (int, float)):
        return {float(node)}
    if isinstance(node, dict):
        node = list(node.values())
    if isinstance(node, list):
        return set().union(*map(_numbers, node)) if node else set()
    return set()


@pytest.mark.parametrize("entry", sorted(SNAPSHOT_VALUE_STATS), ids="/".join)
def test_every_value_entry_removes_what_the_raw_snapshot_holds(entry: tuple[str, str]) -> None:
    scope, key = entry
    df = _frame()
    raw = compute_distribution_snapshot(df, joint_columns=_PAIRS[:1])
    doc = distribution_snapshot_document(df, joint_columns=_PAIRS[:1])
    names = set(doc["redacted_columns"])
    assert {"ssn", "email", "balance", "admitted"} <= names
    assert "status" not in names
    before = [c[key] for c in _containers(raw, scope, names) if key in c]
    after = [c[key] for c in _containers(doc, scope, names) if key in c]
    assert before, "the fixture never filled this slot"
    assert len(before) == len(after)
    rule = SNAPSHOT_VALUE_STATS[entry]
    if rule is Rule.NULL:
        assert all(v is not None for v in before)
        assert all(v is None for v in after)
    elif rule in (Rule.EMPTY_LIST, Rule.EMPTY_MAP):
        assert all(v for v in before)
        assert all(not v for v in after)
    elif rule is Rule.COARSE_RANGE:
        raw_numbers = _numbers(_containers(raw, scope, names))
        assert raw_numbers & set(_BALANCE)
        if key == "bin_counts":
            assert after == [[_N]]
        else:
            written = _numbers(after)
            assert written
            assert not written & set(_BALANCE)
            assert not written & raw_numbers
    else:
        assert all(isinstance(v, str) for v in before)
        assert not set(before) & set(after)
        assert not any(b[:10] in json.dumps(doc) for b in before)


# AT-14b


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _call_names(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            out.append(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
    return out


_VALUE_PATH = _SRC / "profiling" / "_snapshot_values.py"
_FORBIDDEN_NAMES = {
    "pii_score",
    "detector_matches",
    "is_likely_unique",
    "value_policy",
    "redacted_columns",
    "[redacted]",
}
_FORBIDDEN_CALLS = {"crosstab", "_joint_snapshot", "joint_snapshot"}


def _guard_findings(source: str) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value in _FORBIDDEN_NAMES:
                found.append(f"string {node.value}")
            if isinstance(node.value, float) and 0.0 <= node.value <= 1.0:
                found.append(f"float {node.value}")
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES | _FORBIDDEN_CALLS:
            found.append(f"attribute {node.attr}")
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_CALLS:
            found.append(f"name {node.id}")
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            plain = (
                isinstance(value, ast.Constant)
                and (value.value is None or value.value == vr.REDACTED)
            ) or (isinstance(value, ast.Dict) and not value.keys)
            if plain and any(isinstance(t, ast.Subscript) for t in targets):
                found.append("a stats key assigned null, {} or the placeholder inline")
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name == "_apply" and not any(
                k.arg == "denied_value"
                and isinstance(k.value, ast.Constant)
                and k.value.value is None
                for k in node.keywords
            ):
                found.append("_apply without denied_value=None")
    return found


def test_the_snapshot_path_has_no_second_rule_or_applier() -> None:
    source = _VALUE_PATH.read_text(encoding="utf-8")
    assert _guard_findings(source) == []
    calls = set(_call_names(ast.parse(source)))
    assert {"value_sensitive_names", "stamp_value_policy", "_apply"} <= calls
    assert "_collapse_counted_items" in calls


def test_other_count_is_written_only_by_the_one_collapse_helper() -> None:
    tree = _tree(_VALUE_PATH)
    owners = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and n.value == "other_count"
    ]
    assert set(owners) == {"_collapse_counted_items"}


@pytest.mark.parametrize(
    "snippet",
    [
        'x = field.get("pii_score", 0) >= 0.5',
        "import pandas as pd\nct = pd.crosstab(a, b)",
        'stats["quantiles"] = {}',
        'stats["mean"] = None',
        'stats["min"] = "[redacted]"',
        '_apply(stats, rules, keep, denied_value="[redacted]")',
        "_apply(stats, rules, keep)",
    ],
)
def test_the_guard_bites_on_each_forbidden_shape(snippet: str) -> None:
    assert _guard_findings(snippet) != []


def test_the_profile_redactor_uses_the_same_selection_stamp_and_applier() -> None:
    tree = _tree(_SRC / "profiling" / "_value_redaction.py")
    by_name = {
        fn.name: set(_call_names(fn)) for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
    }
    assert {"_sensitive_entries", "stamp_value_policy"} <= by_name["redact_profile_dict"]
    assert "_apply" in by_name["redact_profile_dict"] | by_name["_redact_field"]
    assert "_sensitive_entries" in by_name["value_sensitive_names"]

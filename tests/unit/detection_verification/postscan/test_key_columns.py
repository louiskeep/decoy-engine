"""One traversal of the declared keys: the loader tee and the pandas key frames share it."""

from __future__ import annotations

from typing import Any

from decoy_engine.config import parse_relationship_config
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.verification import postmask
from decoy_engine.verification.postmask import fk_preservation


def _rel(parent: tuple[str, list[str]], *children: tuple[str, list[str]]) -> Any:
    return parse_relationship_config(
        {
            "parent": {"table": parent[0], "columns": parent[1]},
            "children": [{"table": t, "columns": c} for t, c in children],
            "orphan_policy": "preserve",
            "namespace": "ns",
        },
        prefix="test",
    )


def test_key_columns_lists_each_column_once_in_first_seen_order() -> None:
    rels = [
        _rel(("p", ["b", "a"]), ("c", ["x", "y"]), ("p", ["a", "z"])),
        _rel(("c", ["x"]), ("d", ["q"])),
    ]
    assert postmask.key_columns(rels) == {
        "p": ("b", "a", "z"),
        "c": ("x", "y"),
        "d": ("q",),
    }


def test_key_columns_of_no_relationships_is_empty() -> None:
    assert postmask.key_columns([]) == {}


def test_both_consumers_use_the_one_helper() -> None:
    """A second copy of the traversal in either consumer fails here."""
    assert _post_scan.key_columns is postmask.key_columns
    assert fk_preservation.key_columns is postmask.key_columns
    assert not hasattr(_post_scan, "_key_columns")

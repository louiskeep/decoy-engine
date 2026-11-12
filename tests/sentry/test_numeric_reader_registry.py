"""Every reader of a generated column's values is classified in the numeric-typing registry.

A column is numeric only if all its readers are numeric-safe, and an unlisted reader defaults to
text. The readers are the resolved generate inputs: `config.iter_generate_input_refs` says which
option of which column reads which column of which table, and `READERS` says whether that read
is numeric-safe. This test builds one realistic column per registered input and fails when its
resolved edge has no classification, so a new input has to be classified before it can slip
past. Numeric typing keeps no option-key table and no resolver of its own.
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.config import iter_generate_input_refs
from decoy_engine.config._generate_options import GENERATE_OPTIONS
from decoy_engine.config._tables import GenerateColumnConfig
from decoy_engine.generation import _numeric_typing as nt

SOURCE = Path(nt.__file__)
# Readers that are not option inputs: a `reference` column copies its parent, and a relationship
# pairs two columns. Both are judged together with the columns they tie.
STRUCTURAL = {"relationship", "reference.reference_column"}
REGISTERED = sorted(
    (type_, key)
    for type_, spec in GENERATE_OPTIONS.items()
    for key, option in spec.keys.items()
    if option.ref_kind is not None
)


def _realistic(type_: str, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A table and one column of `type_` whose option `key` names the column `src`."""
    option = GENERATE_OPTIONS[type_].keys[key]
    value: Any = {"column_list": ["src"], "expression": "src + 1"}.get(option.ref_kind, "src")
    column: dict[str, Any] = {"name": "reader", "type": type_, key: value}
    fk = {"name": "fk", "type": "reference", "reference_table": "other", "reference_column": "id"}
    if option.ref_kind == "column_via_reference":
        column[str(option.ref_context_key)] = "fk"
    elif option.ref_kind == "column_in_named_table":
        column[str(option.ref_context_key)] = "other"
    return {"name": "t", "generate_columns": [fk, column]}, column


def test_every_column_type_is_known_to_the_input_registry() -> None:
    types = set(typing.get_args(GenerateColumnConfig.model_fields["type"].annotation))
    assert types == set(GENERATE_OPTIONS), "a column type the input registry does not list"


@pytest.mark.parametrize(("type_", "key"), REGISTERED)
def test_every_resolved_reader_edge_has_a_classification_and_a_reason(type_: str, key: str) -> None:
    table, column = _realistic(type_, key)
    edges = [e for e in iter_generate_input_refs(table, column) if e.option == key]
    assert edges, f"{type_}.{key}: the realistic column resolved to no edge"
    for edge in edges:
        assert edge.source_column == "src"
        reader = nt.READERS.get(f"{type_}.{edge.option}")
        assert reader is not None, f"classify the reader {type_}.{edge.option} in READERS"
        assert isinstance(reader.safe, bool) and reader.why


def test_every_classification_is_a_registered_input_or_a_structural_reader() -> None:
    registered = {f"{type_}.{key}" for type_, key in REGISTERED}
    assert set(nt.READERS) == registered | STRUCTURAL
    assert all(r.why for r in nt.READERS.values())


def test_numeric_typing_has_no_option_key_table_and_no_resolver_of_its_own() -> None:
    for gone in ("READER_KEYS", "_names_read", "_IDENTIFIER"):
        assert not hasattr(nt, gone), f"{gone} is a second way to find what a column reads"
    tree = ast.parse(SOURCE.read_text())
    called = {
        (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", ""))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "iter_generate_input_refs" in called
    option_keys = {key for _, key in REGISTERED}
    literal_reads = {
        n.args[0].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    }
    assert not literal_reads & option_keys, "an input option is read by its literal key"


def test_an_unlisted_column_type_keeps_numeric_columns_text() -> None:
    cols = [
        {"name": "id", "type": "sequence", "start": 1},
        {"name": "x", "type": "not_a_listed_type"},
    ]
    cfg = {"tables": [{"name": "t", "row_count": 3, "generate_columns": cols}]}
    assert nt.numeric_plan(cfg) == {}


def test_an_unclassified_reader_keeps_the_column_it_reads_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cols = [
        {"name": "id", "type": "sequence", "start": 1},
        {"name": "n", "type": "grouped_series", "group_by": "g", "order_by": "id"},
        {"name": "g", "type": "categorical", "categories": ["a"]},
    ]
    cfg = {"tables": [{"name": "t", "row_count": 3, "generate_columns": cols}]}
    assert ("t", "id") in nt.numeric_plan(cfg)
    monkeypatch.delitem(nt.READERS, "grouped_series.order_by")
    assert nt.numeric_plan(cfg) == {}

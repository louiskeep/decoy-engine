"""Every reader of a generated column's values is classified in the numeric-typing registry.

A column is numeric only if all its readers are numeric-safe, and an unlisted reader defaults to
text. This test fails when a column type or a sibling-reading config key appears that the
registry does not list, so a new reader has to be classified before it can slip past.
"""

from __future__ import annotations

import typing

from decoy_engine.config._tables import GenerateColumnConfig
from decoy_engine.generation import _numeric_typing as nt
from decoy_engine.generation._reproducibility import _SIBLING_KEYS


def test_every_column_type_is_listed() -> None:
    types = set(typing.get_args(GenerateColumnConfig.model_fields["type"].annotation))
    assert types == set(nt.READER_KEYS), "classify the new column type in READER_KEYS"


def test_every_sibling_key_is_a_registered_reader_key() -> None:
    registered = {key for spec in nt.READER_KEYS.values() for key, _ in spec}
    # `references` is the formula key, `expression` is scanned for identifiers.
    missing = (set(_SIBLING_KEYS) | {"expression"}) - registered
    assert not missing, f"sibling-reading keys with no registry entry: {sorted(missing)}"


def test_every_reader_id_has_a_classification_and_a_reason() -> None:
    used = {rid for spec in nt.READER_KEYS.values() for _, rid in spec}
    assert used <= set(nt.READERS)
    assert all(r.why for r in nt.READERS.values())
    assert {"relationship", "reference.reference_column"} <= set(nt.READERS)


def test_an_unlisted_column_type_keeps_numeric_columns_text() -> None:
    cols = [
        {"name": "id", "type": "sequence", "start": 1},
        {"name": "x", "type": "not_a_listed_type"},
    ]
    cfg = {"tables": [{"name": "t", "row_count": 3, "generate_columns": cols}]}
    assert nt.numeric_plan(cfg) == {}

"""AT-9: the `cell_format` block is value-free and survives the redacted fit."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest

from decoy_engine.generation.statistical import cell_format_member_ok
from decoy_engine.profiling import _snapshot_values as sv
from decoy_engine.profiling import distribution_snapshot_document
from decoy_engine.quality.snapshot import compute_distribution_snapshot

MARKER = 31337.25
_N = 40


@pytest.fixture
def force_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sv, "value_sensitive_names", lambda fields: [f["name"] for f in fields])


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "balance": [MARKER, *[40000.5 + 1000 * i for i in range(_N - 1)]],
            "admitted": pd.date_range("1991-06-01", periods=_N, freq="D"),
            "notes": ["", *[f"free text number {i} with a tail {i * 7919}" for i in range(_N - 1)]],
            "status": ["open", "closed"] * (_N // 2),
            "blank": [None] * _N,
        }
    )


def _blocks(doc: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    return {n: (c["kind"], c["cell_format"]) for n, c in doc["columns"].items()}


def _assert_value_free(blocks: dict[str, tuple[str, dict[str, Any]]]) -> None:
    for kind, block in blocks.values():
        assert str(MARKER) not in json.dumps(block)
        for member, value in block.items():
            assert cell_format_member_ok(kind, member, value), (kind, member)


def test_the_raw_block_is_value_free_with_exact_types() -> None:
    blocks = _blocks(compute_distribution_snapshot(_frame(), cell_format=True))
    assert blocks["balance"] == ("numeric", {"decimal_places": 2})
    _assert_value_free(blocks)


def test_the_redacted_document_keeps_the_block_for_every_kind(force_sensitive: None) -> None:
    raw = compute_distribution_snapshot(_frame(), cell_format=True)
    doc = distribution_snapshot_document(_frame())
    assert set(doc["redacted_columns"]) == set(_frame().columns)
    assert _blocks(doc) == _blocks(raw)
    _assert_value_free(_blocks(doc))
    assert {k for k, _ in _blocks(doc).values()} == {
        "numeric",
        "datetime",
        "freetext",
        "categorical",
        "empty",
    }


def test_the_document_passes_the_keyword_for_unflagged_columns_too() -> None:
    doc = distribution_snapshot_document(_frame(), include_values=True)
    assert all("cell_format" in c for c in doc["columns"].values())


def test_nested_default_deny_nulls_unknown_and_mistyped_members(force_sensitive: None) -> None:
    series = pd.Series([MARKER, 1.5])
    col = compute_distribution_snapshot(pd.DataFrame({"v": series}), cell_format=True)["columns"][
        "v"
    ]
    col["cell_format"]["mystery"] = str(MARKER)
    col["cell_format"]["decimal_places"] = str(MARKER)
    sv._redact_column(series, col)
    assert col["cell_format"] == {"decimal_places": None, "mystery": None}
    assert str(MARKER) not in json.dumps(col["cell_format"])


def test_a_non_dict_block_is_withheld(force_sensitive: None) -> None:
    series = pd.Series([MARKER, 1.5])
    col = compute_distribution_snapshot(pd.DataFrame({"v": series}), cell_format=True)["columns"][
        "v"
    ]
    col["cell_format"] = str(MARKER)
    sv._redact_column(series, col)
    assert col["cell_format"] is None

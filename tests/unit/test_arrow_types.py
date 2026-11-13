"""Truth table for the shared Arrow text-type predicates (DUP-DEFECTS A15, AT-E3-4)."""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine._arrow_types import is_text, is_text_or_null

_TABLE = [
    (pa.string(), True, True),
    (pa.large_string(), True, True),
    (pa.null(), False, True),
    (pa.binary(), False, False),
    (pa.large_binary(), False, False),
    (pa.int64(), False, False),
    (pa.dictionary(pa.int32(), pa.string()), False, False),
]


@pytest.mark.parametrize(("typ", "text", "text_or_null"), _TABLE, ids=lambda v: str(v))
def test_text_predicates_truth_table(typ: pa.DataType, text: bool, text_or_null: bool) -> None:
    assert is_text(typ) is text
    assert is_text_or_null(typ) is text_or_null

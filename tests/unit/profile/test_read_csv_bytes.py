"""GEN-TYPES E3: `read_csv_bytes`, the one CSV bytes seam (exact text, value-free refusal)."""

from __future__ import annotations

import pytest

from decoy_engine.errors import PipelineValidationError
from decoy_engine.fileaccess import FileAccessReadError
from decoy_engine.profile import read_csv_bytes


def test_cells_are_exact_text_with_no_na_conversion() -> None:
    frame = read_csv_bytes(b"v,w\nNA,1\nnull,2\n007,\n", table="lookup")
    assert list(frame.columns) == ["v", "w"]
    assert frame["v"].tolist() == ["NA", "null", "007"]
    assert frame["w"].tolist() == ["1", "2", ""]


def test_a_byte_order_mark_is_not_part_of_the_first_header() -> None:
    frame = read_csv_bytes(b"\xef\xbb\xbfv\na\n", table="lookup")
    assert list(frame.columns) == ["v"]


def test_non_utf8_bytes_are_refused_without_quoting_them() -> None:
    sentinel = "SENTINEL"
    raw = b"v\n" + sentinel.encode() + b"\xff\xfe\n"
    with pytest.raises((PipelineValidationError, FileAccessReadError)) as exc:
        read_csv_bytes(raw, table="lookup")
    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None

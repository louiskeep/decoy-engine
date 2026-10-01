"""A5a (2026-09-30): `read_fixed_width` on the public `decoy_engine` surface.

Before this export the reader lived only in the private (`_`-prefixed)
`decoy_engine.profile._fixed_width_reader` module, which the compatibility
contract says may change without a version bump.
This pins the public re-export of `read_fixed_width` and
`FixedWidthParseError`: it does not re-verify the reader's own parsing
logic (that is `test_v2_fixed_width_source.py`'s job), only that each
surface exists, is the SAME object the private module/errors module
defines (no accidental copy/drift), and is listed in `__all__` so a
caller can depend on it across engine versions without reaching into the
private module.
"""

from __future__ import annotations

from pathlib import Path

import decoy_engine
from decoy_engine import FixedWidthParseError, read_fixed_width
from decoy_engine.errors import FixedWidthParseError as _InternalFixedWidthParseError
from decoy_engine.profile._fixed_width_reader import read_fixed_width as _internal_read_fixed_width


def test_read_fixed_width_is_the_same_function_the_private_module_defines():
    # Identity, not just equality: the public re-export must be the exact
    # object the private reader defines, so the public surface can never
    # silently drift from the implementation it fronts.
    assert read_fixed_width is _internal_read_fixed_width


def test_read_fixed_width_is_listed_in_all():
    assert "read_fixed_width" in decoy_engine.__all__


def test_fixed_width_parse_error_is_the_same_class_the_reader_raises():
    assert FixedWidthParseError is _InternalFixedWidthParseError


def test_fixed_width_parse_error_is_listed_in_all():
    assert "FixedWidthParseError" in decoy_engine.__all__


def test_read_fixed_width_parses_a_small_fixture(tmp_path: Path) -> None:
    layout = {
        "columns": [
            {"name": "name", "start": 0, "width": 8, "type": "str"},
            {"name": "age", "start": 8, "width": 3, "type": "int"},
        ]
    }
    data = tmp_path / "people.txt"
    data.write_text("alice    30\nbob      25\n", encoding="utf-8")

    df = read_fixed_width(str(data), layout)

    assert df["name"].tolist() == ["alice", "bob"]
    assert df["age"].tolist() == [30, 25]

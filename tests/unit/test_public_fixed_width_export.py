"""A5a (2026-09-30): `read_fixed_width` on the public `decoy_engine` surface.

The CLI reads a `format: fixed_width` `FileSource` today by importing
`decoy_engine.profile._fixed_width_reader.read_fixed_width` directly --
a private (`_`-prefixed) module the compatibility contract says may
change without a version bump. This pins the public re-export: it does
not re-verify the reader's own parsing logic (that is
`test_v2_fixed_width_source.py`'s job), only that the surface exists, is
the SAME object the private module defines (no accidental copy/drift),
and is listed in `__all__` so the CLI can rely on it across engine
versions.
"""

from __future__ import annotations

from pathlib import Path

import decoy_engine
from decoy_engine import read_fixed_width
from decoy_engine.profile._fixed_width_reader import read_fixed_width as _internal_read_fixed_width


def test_read_fixed_width_is_the_same_function_the_private_module_defines():
    # Identity, not just equality: the public re-export must be the exact
    # object the private reader defines, so the public surface can never
    # silently drift from the implementation it fronts.
    assert read_fixed_width is _internal_read_fixed_width


def test_read_fixed_width_is_listed_in_all():
    assert "read_fixed_width" in decoy_engine.__all__


def test_read_fixed_width_returns_the_same_table_on_a_small_fixture(tmp_path: Path) -> None:
    layout = {
        "columns": [
            {"name": "name", "start": 0, "width": 8, "type": "str"},
            {"name": "age", "start": 8, "width": 3, "type": "int"},
        ]
    }
    data = tmp_path / "people.txt"
    data.write_text("alice    30\nbob      25\n", encoding="utf-8")

    via_public = read_fixed_width(str(data), layout)
    via_private = _internal_read_fixed_width(str(data), layout)

    assert via_public.equals(via_private)
    assert via_public["name"].tolist() == ["alice", "bob"]
    assert via_public["age"].tolist() == [30, 25]

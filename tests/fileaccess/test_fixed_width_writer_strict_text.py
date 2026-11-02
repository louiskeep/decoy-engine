"""Under a strict policy a fixed-width write error names the field position, never the path.

The ambient CLI policy keeps its text (the path, as on main). Every place the error carries text
is checked: str, repr, args, notes, cause, context and the traceback rendering.
"""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest

from decoy_engine.config._fixed_width import FixedWidthLayout
from decoy_engine.errors import ConfigError, FixedWidthWriteError
from decoy_engine.profile._fixed_width_writer import write_fixed_width
from tests.fileaccess.support import RootRelative

LEAF = "tenant-a/private/output-CANARY.txt"
LAYOUT = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[RootRelative, Path]]:
    root = tmp_path / "root"
    (root / "tenant-a" / "private").mkdir(parents=True)
    policy = RootRelative(root)
    try:
        yield policy, root
    finally:
        policy.close()


def _texts(exc: BaseException) -> list[str]:
    out: list[str] = []
    link: BaseException | None = exc
    while link is not None:
        out += [str(link), repr(link), repr(link.args), repr(getattr(link, "__notes__", None))]
        link = link.__cause__ or link.__context__
    out.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return out


CASES = {
    "overflow": (pd.DataFrame({"a": ["toolong"]}), LAYOUT, FixedWidthWriteError),
    "bad_layout": (pd.DataFrame({"a": ["x"]}), {"columns": "nope"}, ConfigError),
}


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("locator", ["absolute", "relative"])
def test_a_strict_write_error_names_only_the_field_position(
    world: tuple[RootRelative, Path], case: str, locator: str
) -> None:
    policy, root = world
    frame, layout, error = CASES[case]
    path = str(root / LEAF) if locator == "absolute" else LEAF
    with pytest.raises(error) as info:
        write_fixed_width(frame, layout, path, file_access=policy)  # type: ignore[arg-type]
    exc = info.value
    assert exc.__cause__ is None and exc.__context__ is None
    assert not hasattr(exc, "__notes__")
    for text in _texts(exc):
        assert "CANARY" not in text and "tenant-a" not in text and str(root) not in text, text
    assert str(exc).startswith("target.path: ")
    assert not (root / LEAF).exists()


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_ambient_write_error_text_is_unchanged(tmp_path: Path, case: str) -> None:
    frame, layout, error = CASES[case]
    path = tmp_path / "out-AMBIENT.txt"
    with pytest.raises(error) as info:
        write_fixed_width(frame, layout, str(path))  # type: ignore[arg-type]
    assert str(info.value).startswith(f"{path}: ")

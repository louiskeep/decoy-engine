"""A malformed file source descriptor is refused without echoing its value under a strict policy.

`build_profile_source` refuses a non-str `path` (a `Path`, say) and a non-dict fixed-width
`layout`. Under the ambient CLI policy the text keeps its repr of the value (unchanged); under a
strict policy it names the config position and never the value, which may be a path.
"""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine.profile._readers import build_profile_source
from tests.fileaccess.support import RootRelative

CANARY_DIR = Path("/tenant-a/private/PROFILE-CANARY-dir")


def _texts(exc: BaseException) -> list[str]:
    out: list[str] = []
    link: BaseException | None = exc
    while link is not None:
        out += [str(link), repr(link), repr(link.args), repr(getattr(link, "__notes__", None))]
        link = link.__cause__ or link.__context__
    out.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return out


@pytest.fixture
def policy(tmp_path: Path) -> Iterator[RootRelative]:
    root = tmp_path / "root"
    root.mkdir()
    access = RootRelative(root)
    try:
        yield access
    finally:
        access.close()


CASES: dict[str, dict[str, Any]] = {
    "path_object": {
        "type": "file",
        "format": "csv",
        "has_header": True,
        "path": CANARY_DIR / "t.csv",
    },
    "path_bytes": {
        "type": "file",
        "format": "csv",
        "has_header": True,
        "path": b"/tenant-a/PROFILE-CANARY.csv",
    },
    "layout_string": {
        "type": "file",
        "format": "fixed_width",
        "path": "t.txt",
        "layout": "/tenant-a/PROFILE-CANARY-layout.yaml",
    },
    "format_path": {"type": "file", "format": "/tenant-a/PROFILE-CANARY.fmt", "path": "t.csv"},
    "type_path": {"type": "/tenant-a/PROFILE-CANARY.type", "path": "t.csv"},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_strict_refusal_names_the_position_not_the_value(policy: RootRelative, case: str) -> None:
    with fileaccess.scope(policy), pytest.raises((ValueError, NotImplementedError)) as info:
        build_profile_source(CASES[case], field_key="sources.t.path")
    for text in _texts(info.value):
        assert "CANARY" not in text and "tenant-a" not in text, text
    assert "sources.t" in str(info.value)


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_ambient_refusal_text_is_unchanged(case: str) -> None:
    descriptor = CASES[case]
    with pytest.raises((ValueError, NotImplementedError)) as info:
        build_profile_source(descriptor)
    shown = {
        "path_object": repr(descriptor["path"]),
        "path_bytes": repr(descriptor["path"]),
        "layout_string": repr(descriptor.get("layout")),
        "format_path": repr(descriptor.get("format")),
        "type_path": repr(descriptor["type"]),
    }[case]
    assert shown in str(info.value)

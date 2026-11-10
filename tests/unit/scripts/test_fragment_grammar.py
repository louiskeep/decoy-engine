"""The fragment heading grammar is exactly `### <Type> (<title>, <slice>, <date>)`.

Also: refusals are raised outside any `except` block, so no exception context rides along.
"""

from __future__ import annotations

import pytest
from scripts.release import stamp_release as sr


@pytest.mark.parametrize(
    "heading",
    [
        "### Added (title)",
        "### Added (title, slice)",
        "### Added (title, slice, 2026-13-45)",
        "### Added (title, slice, 2026-02-30)",
        "### Added (title, slice, 2026-1-1)",
        "### Added (title, slice, not-a-date)",
        "### Added (, slice, 2026-11-10)",
        "### Added (title, , 2026-11-10)",
        "### Added (title, slice, 2026-11-10) trailing",
        "### Added(title, slice, 2026-11-10)",
        "### Bogus (title, slice, 2026-11-10)",
    ],
)
def test_heading_off_grammar_is_refused(heading: str) -> None:
    with pytest.raises(sr.FragmentError, match="heading"):
        sr.parse_fragment(f"{heading}\n\nBody.\n")


@pytest.mark.parametrize(
    "heading",
    [
        "### Added (title, SLICE-E1, 2026-11-10)",
        "### Fixed (a title, with a comma, S1, 2026-02-28)",
        "### Security (t, SLICE E1, 2028-02-29)",
    ],
)
def test_heading_on_grammar_is_accepted(heading: str) -> None:
    assert sr.parse_fragment(f"{heading}\n\nBody.\n").block.startswith(heading)


def test_parse_refusal_has_no_exception_context() -> None:
    with pytest.raises(sr.FragmentError) as info:
        sr.parse_fragment("### Added (t, s, 2026-02-30)\n\nBody.\n")
    assert info.value.__context__ is None


def test_one_validator_owns_the_file_name_rule() -> None:
    assert sr.is_fragment_name("thing-1.md")
    assert not sr.is_fragment_name("Thing.md")
    assert not sr.is_fragment_name("thing.txt")
    assert not sr.is_fragment_name("-thing.md")
    assert not sr.is_fragment_name("README.md")

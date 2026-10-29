"""STORM-split guard sentries (plan test 4).

Two independent regression guards for the `storm` -> `detection`/`profiling`/
`verification` rename:

1. No `decoy_engine.storm` import (or dynamic-import string) survives in the
   engine source. The import-path check catches a missed module reference.
2. No leftover renamed-AWAY symbol name survives. The import-path check alone
   cannot see a symbol like `_run_storm_inner` or `storm_profiles_to_snapshot`
   that moved but kept a `storm`-flavored name, so this is a separate name grep.

Both scan `src/decoy_engine`. The KEEP identifiers (the `run_storm` CONFIG
FIELD, the `"storm.scan"` event, `"storm-post-mask/v1"`, `storm_run_group_id`,
the `/storm/` routes) are product/wire surfaces and are deliberately NOT caught:
the renamed-away set below lists only the symbols the rename removed.
"""

from __future__ import annotations

import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "decoy_engine"

# Symbols the rename REMOVED. Each must have zero occurrences in the engine
# source. `run_storm` is NOT here: it survives as the config-field name (KEEP).
_RENAMED_AWAY = (
    "StormProfile",
    "StormPostMaskReport",
    "run_storm_post_mask",
    "_run_storm_inner",
    "storm_profiles_to_snapshot",
)


def _py_files() -> list[pathlib.Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_decoy_engine_storm_import() -> None:
    """No module imports or references `decoy_engine.storm` (gone package)."""
    offenders = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        if "decoy_engine.storm" in text:
            offenders.append(str(path.relative_to(_SRC)))
    assert offenders == [], (
        "the `decoy_engine.storm` package was removed by the STORM split; "
        f"these files still reference it: {offenders}"
    )


@pytest.mark.parametrize("symbol", _RENAMED_AWAY)
def test_no_leftover_renamed_storm_symbol(symbol: str) -> None:
    """No renamed-away `storm`-flavored symbol name survives in the source."""
    offenders = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        if symbol in text:
            offenders.append(str(path.relative_to(_SRC)))
    assert offenders == [], (
        f"{symbol!r} was renamed by the STORM split but still appears in: {offenders}"
    )

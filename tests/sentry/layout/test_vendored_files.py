"""Sentry: the two layout helpers the CLI vendors are pinned by digest.

This repo is the source of ``_layout.py`` and ``_ratchet_cases.py``. The CLI copies them
byte for byte and pins the same digests (its header names the engine commit it copied from),
so a one-sided edit to either copy turns a sentry red. Changing a helper here means updating
the pin below and re-vendoring in the CLI in the same step. Later vendored files add rows to
``PINS`` instead of a new test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

LAYOUT = Path(__file__).resolve().parent

PINS: dict[str, str] = {
    "_layout.py": "090676684a73bc90bb73240b1a3909434cd15ed8348c15bc365b7d764d94e96c",
    "_ratchet_cases.py": "038d0dff5e370a8238712c365db283b9befefc40135956dc61f05930946bab00",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_vendored_helpers_match_their_pins() -> None:
    for name, pinned in PINS.items():
        assert digest((LAYOUT / name).read_bytes()) == pinned, (
            f"{name} changed: update PINS and re-vendor it in the CLI"
        )


def test_one_changed_byte_changes_the_digest() -> None:
    data = (LAYOUT / "_layout.py").read_bytes()
    assert digest(data) != digest(data + b" ")
    assert digest(data) != digest(b"#" + data[1:])

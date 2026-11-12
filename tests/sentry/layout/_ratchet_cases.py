"""Cases for the numeric ``max`` comparison in the shared baseline ratchet.

``(name, base max, head max, red)``: ``None`` means the entry carries no ``max``. Both repos
parametrize their ratchet meta-test from this one table; the file is vendored byte for byte
and pinned by ``test_vendored_files.py``.
"""

from __future__ import annotations

MAX_VALUE_CASES: tuple[tuple[str, int | None, int | None, bool], ...] = (
    ("no max on either side", None, None, False),
    ("max added", None, 5, False),
    ("max dropped", 5, None, True),
    ("equal", 5, 5, False),
    ("lower", 5, 4, False),
    ("higher", 5, 6, True),
)

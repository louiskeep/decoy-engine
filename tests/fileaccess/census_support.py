"""What a census frame test looks for: every engine frame local on every link of a chain."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from tests.fileaccess.locals_support import _strings

# The census scanner's name pattern (`scripts/secret_material_census.py`).
SECRET_NAME = re.compile(
    r"key|secret|master|signing|password|token|seed|ikm|prk|okm|material|root", re.IGNORECASE
)


def engine_frames(exc: BaseException) -> Iterator[tuple[str, dict[str, Any]]]:
    """(qualname, locals) of every `decoy_engine` frame on every link: cause, context, group."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        link = stack.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        stack.extend([link.__cause__, link.__context__, *getattr(link, "exceptions", ())])
        tb = link.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if str(frame.f_globals.get("__name__", "")).startswith("decoy_engine"):
                name = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
                yield name, dict(frame.f_locals)
            tb = tb.tb_next


def leaks(exc: BaseException, canary: bytes | str) -> list[str]:
    """Every engine frame local that holds the canary, or secret-named bytes."""
    raw = canary.encode() if isinstance(canary, str) else canary
    needles = [raw.decode("latin1"), raw.hex(), repr(raw)[2:-1]]
    found = []
    for function, f_locals in engine_frames(exc):
        for name, value in f_locals.items():
            try:
                shown = repr(value)
            except Exception:
                shown = ""
            texts = [shown, *_strings(value)]
            if any(n in t for t in texts for n in needles):
                found.append(f"{function}() {name!r} holds the canary")
            elif SECRET_NAME.search(name) and isinstance(value, (bytes, bytearray, memoryview)):
                if len(value) >= 8:
                    found.append(f"{function}() {name!r} holds secret-named bytes")
    return found

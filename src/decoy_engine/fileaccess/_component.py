"""The one validator for a single filesystem name component."""

from __future__ import annotations

import os

from decoy_engine.fileaccess._errors import deny

MAX_COMPONENT_BYTES = 255


def is_valid_component(component: object) -> bool:
    """A `str` that is non-empty, not `.` or `..`, has no `/` or NUL, and fits 255 bytes."""
    if not isinstance(component, str) or not component:
        return False
    if component in (".", "..") or "/" in component or "\0" in component:
        return False
    try:
        return len(os.fsencode(component)) <= MAX_COMPONENT_BYTES
    except UnicodeEncodeError:
        return False


def validate_component(component: object, field_key: str) -> str:
    """Return `component` or raise the one denial, before any syscall."""
    if not is_valid_component(component):
        raise deny(field_key)
    return str(component)

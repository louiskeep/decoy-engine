"""The one validator for a single filesystem name component."""

from __future__ import annotations

import os

from decoy_engine.fileaccess._errors import deny

MAX_COMPONENT_BYTES = 255
# The destination lock `.<leaf>.decoy-lock` sits beside the leaf as one component, so a
# publication leaf keeps room for its prefix and suffix: 255 - 12 = 243 bytes (named limit).
LOCK_PREFIX, LOCK_SUFFIX = ".", ".decoy-lock"
MAX_PUBLICATION_LEAF_BYTES = MAX_COMPONENT_BYTES - len(LOCK_PREFIX) - len(LOCK_SUFFIX)


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


def validate_publication_leaf(leaf: str, field_key: str) -> None:
    """Deny, before any syscall, a publication leaf whose lock name would not fit a component."""
    try:
        size = len(os.fsencode(leaf))
    except UnicodeEncodeError:
        size = MAX_COMPONENT_BYTES + 1
    if size > MAX_PUBLICATION_LEAF_BYTES:
        raise deny(field_key)

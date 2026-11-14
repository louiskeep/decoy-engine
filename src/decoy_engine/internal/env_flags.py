"""One reader for boolean environment flags.

The accepted set is compared after ``strip().lower()``: ``1``, ``true``, ``yes``, ``on`` are true
and ``0``, ``false``, ``no``, ``off`` are false. An unset, empty or blank variable gives the
flag's default. Any other spelling raises ``EnvFlagError``, whose text names the variable and
never the value, so a caller chooses its own safe outcome for a value it cannot read.

The variable is read at call time and never cached, so a changed environment is seen by the next
call.
"""

from __future__ import annotations

import os

from decoy_engine.errors import EnvFlagError

__all__ = ["env_flag"]

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def env_flag(name: str, *, default: bool) -> bool:
    """The boolean the environment variable ``name`` holds, or ``default`` when it is empty."""
    return _decode(name, os.environ.get(name), default)


def _decode(name: str, raw: str | None, default: bool) -> bool:
    token = "" if raw is None else raw.strip().lower()
    if not token:
        return default
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    raise EnvFlagError(name)

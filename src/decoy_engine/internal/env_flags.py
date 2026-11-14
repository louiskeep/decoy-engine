"""One decoder for boolean environment flags.

The caller reads its own variable, by a name the secret-material census can resolve, and passes
the raw string here. The accepted set is compared after ``strip().lower()``: ``1``, ``true``,
``yes``, ``on`` are true and ``0``, ``false``, ``no``, ``off`` are false. A missing, empty or blank
value gives the flag's default. Any other spelling raises ``EnvFlagError``, whose text names the
variable and never the value, so a caller chooses its own safe outcome for a value it cannot read.

Decoding never touches the environment, so this module cannot read a secret-named variable.
"""

from __future__ import annotations

from decoy_engine.errors import EnvFlagError

__all__ = ["env_flag"]

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def env_flag(name: str, raw: str | None, *, default: bool) -> bool:
    """The boolean ``raw`` (the value of variable ``name``, ``None`` when unset) spells.

    ``name`` is only used in the refusal text.
    """
    token = "" if raw is None else raw.strip().lower()
    if not token:
        return default
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    raise EnvFlagError(name)

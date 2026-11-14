"""One decoder and one registry for boolean environment flags.

Every flag variable the engine reads is registered in ``FLAGS`` with its policy: the default and
the spellings it accepts. The caller reads its own variable, by a module constant the
secret-material census can resolve, and passes the raw string to ``env_flag``, which decodes it by
the registered policy for that name. A name that is not registered raises ``KeyError``, so a new
flag cannot bypass the registry. Decoding never touches the environment.

Two spelling sets exist. The Decoy set is ``1``, ``true``, ``yes``, ``on`` and ``0``, ``false``,
``no``, ``off`` after ``strip().lower()``; a missing, empty or blank value gives the default and
any other spelling raises ``EnvFlagError``, whose text names the variable and never the value. The
Vault set keeps the spellings the Vault client convention accepts and never raises.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from decoy_engine.errors import EnvFlagError

__all__ = ["FLAGS", "FlagPolicy", "env_flag"]


@dataclass(frozen=True)
class FlagPolicy:
    """The default and the accepted spellings of one flag.

    ``false`` is the set of false spellings; ``None`` means every spelling outside ``true`` is
    false and none is an error.
    """

    default: bool
    true: frozenset[str]
    false: frozenset[str] | None


_DECOY_TRUE = frozenset({"1", "true", "yes", "on"})
_DECOY_FALSE = frozenset({"0", "false", "no", "off"})
# Vault client convention: `t` and `y` count as true and an unknown spelling is false, never an error.
_VAULT_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})


def _decoy(default: bool = False) -> FlagPolicy:
    return FlagPolicy(default, _DECOY_TRUE, _DECOY_FALSE)


FLAGS: Mapping[str, FlagPolicy] = MappingProxyType(
    {
        "DECOY_ML_DISABLED": _decoy(),
        "DECOY_PACK_REQUIRE_SIGNATURE": _decoy(),
        "DECOY_INSIDE_ISOLATED_WORKER": _decoy(),
        "VAULT_SKIP_VERIFY": FlagPolicy(False, _VAULT_TRUE, None),
    }
)


def env_flag(name: str, raw: str | None) -> bool:
    """The boolean ``raw`` (the value of registered flag ``name``, ``None`` when unset) spells."""
    policy = FLAGS[name]
    token = "" if raw is None else raw.strip().lower()
    if not token:
        return policy.default
    if token in policy.true:
        return True
    if policy.false is None or token in policy.false:
        return False
    raise EnvFlagError(name)

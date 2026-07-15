"""Shared runtime `pool_size` coalesce (DE-11).

The runtime analog of `plan._pool_size.resolve_pool_size`: ONE definition of
"read an effective pool size out of a free-form provider-config mapping" so
every runtime reader agrees. Every runtime pool builder reads its size from an
untyped mapping (`ColumnSeed.provider_config` flattened to a dict, or
`ProviderSpec.extra`), where the key may be absent OR present with an explicit
``None`` -- a validated config dumps an unset `pool_size` as an explicit null.

`dict.get(key, default)` only substitutes the default when the key is ABSENT, so
a present ``None`` would slip through and reach `int(None)`, which raises. The
compile-time resolver already treats a nested ``None`` as undeclared, so the
runtime must agree: coalesce both absent-key AND explicit-``None`` to the
default. Centralising it here means a new reader cannot drift from that rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# One default for every runtime pool build; keep the compile-time and runtime
# fallbacks on the same number so a config that omits pool_size sizes the same
# pool no matter which route builds it.
DEFAULT_POOL_SIZE = 10_000


def resolve_runtime_pool_size(
    mapping: Mapping[str, Any], *, default: int = DEFAULT_POOL_SIZE
) -> int:
    """Return the effective pool size from a free-form config mapping.

    An absent key and a key present with value ``None`` both coalesce to
    ``default`` (a validated config dumps an unset `pool_size` as explicit
    null); any other value is coerced to ``int``.
    """
    raw = mapping.get("pool_size")
    return int(raw) if raw is not None else default

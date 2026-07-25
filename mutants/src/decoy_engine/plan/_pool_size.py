"""Single source of truth for a column's effective ``pool_size``.

``pool_size`` has two legal declaration sites: the top-level
``ColumnConfig.pool_size`` field and ``provider_config.pool_size``. Two
compile-time readers consume it -- the UNIQUE-capacity preflight
(``plan/_checks.py``) and the seed-envelope resolver
(``plan/_seed_envelope.py``). If they resolve it differently, a
``provider_config``-only declaration slips past the preflight and fails late
at runtime instead of at compile. Both readers call ``resolve_pool_size`` so
they cannot drift.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def resolve_pool_size(col_entry: dict[str, Any], *, table_name: str, col_name: str) -> int | None:
    """Return a column's effective ``pool_size``, or ``None`` if undeclared.

    Top-level ``pool_size`` wins; ``provider_config.pool_size`` is the
    fallback. Both locations are legal individually, so when both are set and
    DISAGREE the config is ambiguous and raises ``pool_size_location_conflict``
    rather than silently discarding one value (equal values are fine).
    """
    top_pool_size = col_entry.get("pool_size")
    provider_config_raw = col_entry.get("provider_config")
    provider_pool_size = (
        provider_config_raw.get("pool_size") if isinstance(provider_config_raw, dict) else None
    )
    if (
        top_pool_size is not None
        and provider_pool_size is not None
        and top_pool_size != provider_pool_size
    ):
        raise PlanCompileError(
            code="pool_size_location_conflict",
            path=f"tables.{table_name}.columns.{col_name}.pool_size",
            message=(
                f"Column {table_name}.{col_name}: top-level "
                f"pool_size={top_pool_size!r} conflicts with "
                f"provider_config.pool_size={provider_pool_size!r}. Both "
                "locations are legal individually, but declaring two "
                "different values for the same column is ambiguous. "
                "Set one location only, or make both values equal."
            ),
        )
    resolved = top_pool_size if top_pool_size is not None else provider_pool_size
    return int(resolved) if resolved is not None else None

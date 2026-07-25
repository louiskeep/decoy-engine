"""Plan-compile check for faker columns without a provider (DE-03 sibling).

A `strategy: faker` column is a GENERATOR: it needs a `provider` to produce
values. Without one, `_build_seed_envelope` drops the column from the seed
envelope entirely (`plan/_seed_envelope.py`: the `strategy == "faker" and not
provider` guard), so it never becomes a work node and its raw source value
reaches output unmasked -- the same silent-passthrough class DE-03 closes at the
output adapter. This is the compile-time half: reject the shape before a run
starts, regardless of authoring path (CLI, hand-written YAML, or a platform
Studio-emitted config). The runtime output projection remains the backstop for
schema drift compile cannot see.

Follows the Sprint-13 honesty-pack precedent (`check_truncate_config`,
`check_fpe_charset_config`): a masking/synthesis primitive must never silently
emit the source value on a misconfiguration.

This module exports exactly one function: ``check_faker_requires_provider``.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_faker_requires_provider(config: dict[str, Any]) -> None:
    """Reject `strategy: faker` columns that declare no provider.

    A faker column with no provider cannot generate values; the seed-envelope
    builder drops it, leaving the raw source value to pass through unmasked.
    Config-only (no profile, no source data): safe to run in both compile
    branches and in ``run_config_only_checks``. Validation never mutates.

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: a `faker` column has `provider` unset or empty.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "faker":
                continue
            col_name = col_entry.get("name", "?")
            if not col_entry.get("provider"):
                raise PlanCompileError(
                    code="faker_requires_provider",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"faker column {col_name!r} in table {table_name!r} declares "
                        "no provider. `strategy: faker` is a generator and needs a "
                        "provider to produce values; without one the column is dropped "
                        "and its raw source value would reach output unmasked. Add a "
                        "provider, or choose a scalar transform strategy."
                    ),
                )

"""Plan-compile check for truncate columns (Sprint 13 / coercion-13, S3).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling (see the SP-10 comment in tests/sentry/test_module_size.py: the
_checks.py module decomposes into per-strategy sub-modules as new strategies
land; this is the coercion-13 S3 slice for truncate).

Sprint 13 finding 0.4 (CONFIRMED live on main): `TruncateHandler.run`
(`execution/_strategies/_truncate.py`) and its polars twin
(`execution/polars/_strategies/_truncate.py`) each had THREE silent-
passthrough exits: an invalid `length`, an unrecognized `keep`, and a
multi-character or non-string `mask_char`. On any of the three, the
handler returned the source DataFrame unchanged with no warning -- a
masking primitive silently emitting the source PII on a bad config. This
module is the compile-time half of the fix (D6 item 1 of the Sprint 13
implementation guide): reject all three shapes before a run ever starts,
regardless of authoring path (CLI, hand-written YAML, or a platform
Studio-emitted config). The handlers additionally raise `StrategyError`
on the same three shapes as a defense-in-depth backstop (D6 item 2); this
check is the loud, compile-time-visible half.

This module exports exactly one function: ``check_truncate_config``.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_truncate_config(config: dict[str, Any]) -> None:
    """Reject truncate columns whose provider_config cannot mask the column.

    Compile-check ownership table row #22 (Sprint 13 / coercion-13 S3,
    2026-07-03). A masking primitive must never emit source values on
    misconfiguration (Sprint 13 finding 0.4); the three checks below close
    every silent-passthrough exit that existed in ``TruncateHandler.run``:

    1. ``length`` missing or not an ``int >= 1``: the handler cannot compute
       a slice boundary. Raises ``truncate_length_invalid``.
    2. ``keep`` set to anything other than ``"head"`` / ``"tail"`` (when
       present): the handler cannot decide which end of the value to keep.
       Raises ``truncate_keep_invalid``.
    3. ``mask_char`` set to a non-string, or a string that is not exactly one
       character (when present): the handler cannot repeat it to pad the
       dropped span. Raises ``truncate_mask_char_invalid``.

    Config-only (no profile, no source data): safe to run in both compile
    branches and in ``run_config_only_checks``. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: `length` / `keep` / `mask_char` cannot mask the
            column as configured.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "truncate":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config")
            if not isinstance(pc, dict):
                pc = {}

            length = pc.get("length")
            if not isinstance(length, int) or length < 1:
                raise PlanCompileError(
                    code="truncate_length_invalid",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.length",
                    message=(
                        f"truncate column {col_name!r} in table {table_name!r} has "
                        f"invalid length {length!r} ({type(length).__name__}). length "
                        "must be an integer >= 1. A masking strategy must never "
                        "silently pass the source value through on a bad config; fix "
                        "or remove this column's length."
                    ),
                )

            keep = pc.get("keep")
            if keep is not None and keep not in ("head", "tail"):
                raise PlanCompileError(
                    code="truncate_keep_invalid",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.keep",
                    message=(
                        f"truncate column {col_name!r} in table {table_name!r} has "
                        f"invalid keep {keep!r}. keep must be 'head' or 'tail' when set."
                    ),
                )

            mask_char = pc.get("mask_char")
            if mask_char is not None and (not isinstance(mask_char, str) or len(mask_char) != 1):
                raise PlanCompileError(
                    code="truncate_mask_char_invalid",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.mask_char",
                    message=(
                        f"truncate column {col_name!r} in table {table_name!r} has "
                        f"invalid mask_char {mask_char!r} ({type(mask_char).__name__}). "
                        "mask_char must be a single character when set."
                    ),
                )

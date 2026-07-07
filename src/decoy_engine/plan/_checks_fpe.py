"""Plan-compile check for fpe columns (Sprint 2 honesty pack, S6, GATE-1 Q4).

Added as its own module for the same reason as `_checks_bucketize.py`: avoid
growing `plan/_checks.py` past its size ceiling (see the SP-10 comment in
`tests/sentry/test_module_size.py`).

Discovery 0.1 (guide section 0.1, DISCOVERY 2, 2026-07-04): `_fpe.py:70`
(pre-slice) returned `df, []` -- a silent whole-column passthrough (V1
behavior) -- when the resolved charset has fewer than 2 distinct characters.
This is the same fail-open shape #13 closed for truncate/bucketize/
categorical (a masking strategy must never silently pass the source column
through on a bad config). This module rejects the shape at compile time,
before any row is masked; `FpeStrategyHandler.run` additionally raises
`StrategyError` on the same shape as a defense-in-depth backstop.

Reuses `_CHARSETS` from `transforms/fpe.py` as the single source of truth
for named charsets (no duplicated table to drift out of sync).

This module exports exactly one function: `check_fpe_charset_config`.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.fpe import _CHARSETS


def check_fpe_charset_config(config: dict[str, Any]) -> None:
    """Reject fpe columns whose resolved charset has fewer than 2 distinct
    characters.

    Compile-check ownership table row #25 (Sprint 2 honesty pack S6,
    GATE-1 Q4, 2026-07-04). Mirrors `FpeStrategyHandler.run` exactly: the
    charset resolves via `_CHARSETS.get(charset_spec, charset_spec)` (a
    named charset, e.g. "digits", or a literal charset string), then
    dedupes preserving order. Fewer than 2 distinct characters after
    dedup is degenerate: `fpe_encrypt_value`'s Feistel permutation has no
    non-trivial domain to permute over, and the pre-slice handler behavior
    was to skip masking the whole column, which is guaranteed to leave it
    unmasked at run today.

    Config-only (no profile, no source data): safe to run in both compile
    branches and in `run_config_only_checks`. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: the resolved charset has fewer than 2 distinct
            characters.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "fpe":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config")
            if not isinstance(pc, dict):
                pc = {}

            charset_spec = pc.get("charset", "digits")
            resolved = _CHARSETS.get(charset_spec, charset_spec)
            if not isinstance(resolved, str):
                resolved = str(resolved)
            deduped = "".join(dict.fromkeys(resolved))
            if len(deduped) < 2:
                raise PlanCompileError(
                    code="fpe_charset_degenerate",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.charset",
                    message=(
                        f"fpe column {col_name!r} in table {table_name!r} resolves to a "
                        f"charset with {len(deduped)} distinct character(s) "
                        f"({charset_spec!r} -> {resolved!r}). A degenerate charset (<2 "
                        "distinct characters) leaves the column unmasked at run; use a "
                        "named charset (digits/alpha/ALPHA/alphanum/ALPHANUM) or a "
                        "literal charset with at least 2 distinct characters."
                    ),
                )

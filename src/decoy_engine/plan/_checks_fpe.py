"""Plan-compile check for fpe columns (Sprint 2 honesty pack, S6, GATE-1 Q4).

Added as its own module for the same reason as `_checks_bucketize.py`: avoid
growing `plan/_checks.py` past its size ceiling (see the SP-10 comment in
`tests/sentry/test_module_size.py`).

Discovery 0.1 (guide section 0.1, DISCOVERY 2, 2026-07-04): `_fpe.py:70`
(pre-slice) returned `df, []`, a silent whole-column passthrough (V1
behavior), when the resolved charset has fewer than 2 distinct characters.
This is the same fail-open shape #13 closed for truncate/bucketize/
categorical (a masking strategy must never silently pass the source column
through on a bad config). This module rejects the shape at compile time,
before any row is masked; `FpeStrategyHandler.run` additionally raises
`StrategyError` on the same shape as a defense-in-depth backstop.

Task 5.2 (FF1 primitive swap, plan v2 body): a custom charset with a
duplicate symbol used to be silently deduplicated before this length check
ever ran. FF1 requires an ordered, duplicate-free alphabet (a duplicate would
make decode ambiguous); a duplicate is now its own rejected shape
(`fpe_charset_duplicate_symbols`), checked BEFORE the length check, not
folded into it.

Task 5.2 P6-final: a custom charset is also restricted to printable ASCII
(0x21-0x7E), rejected as `fpe_charset_non_ascii`. Every named entry in
`_CHARSETS` already satisfies this, so only a literal charset spec can ever
trip it. Restricting to this range sidesteps Unicode grapheme segmentation
entirely (one code point is one symbol, trivially, within it).

Reuses `_CHARSETS` / `check_charset_unique` / `check_charset_ascii_printable`
from `transforms/fpe.py` as the single source of truth for named charsets and
these two rules (no duplicated table or check to drift out of sync).

This module exports exactly one function: `check_fpe_charset_config`.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.fpe import (
    _CHARSETS,
    check_charset_ascii_printable,
    check_charset_unique,
)


def check_fpe_charset_config(config: dict[str, Any]) -> None:
    """Reject fpe columns whose resolved charset has a duplicate symbol,
    a non-ASCII character, or fewer than 2 distinct characters.

    Compile-check ownership table row #25 (Sprint 2 honesty pack S6,
    GATE-1 Q4, 2026-07-04; duplicate-symbol and ASCII rules added Task 5.2).
    Mirrors `FpeStrategyHandler.run` exactly: the charset resolves via
    `_CHARSETS.get(charset_spec, charset_spec)` (a named charset, e.g.
    "digits", or a literal charset string), taken AS GIVEN (no silent
    dedup). A duplicate symbol is rejected outright (FF1's alphabet must be
    ordered and unique); a non-ASCII character is rejected (custom fpe
    charsets are restricted to printable ASCII, 0x21-0x7E); fewer than 2
    distinct characters is degenerate: `fpe_encrypt_value`'s FF1 permutation
    has no non-trivial domain to permute over, and the pre-slice handler
    behavior was to skip masking the whole column, which is guaranteed to
    leave it unmasked at run today.

    Config-only (no profile, no source data): safe to run in both compile
    branches and in `run_config_only_checks`. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: the resolved charset has a duplicate symbol, a
            non-ASCII character, or fewer than 2 distinct characters.
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
            try:
                check_charset_unique(charset_spec, resolved)
            except FpeUnencryptableError as exc:
                raise PlanCompileError(
                    code="fpe_charset_duplicate_symbols",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.charset",
                    message=(f"fpe column {col_name!r} in table {table_name!r}: {exc}"),
                ) from exc
            try:
                check_charset_ascii_printable(resolved)
            except FpeUnencryptableError as exc:
                raise PlanCompileError(
                    code="fpe_charset_non_ascii",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.charset",
                    message=(f"fpe column {col_name!r} in table {table_name!r}: {exc}"),
                ) from exc
            if len(resolved) < 2:
                raise PlanCompileError(
                    code="fpe_charset_degenerate",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.charset",
                    message=(
                        f"fpe column {col_name!r} in table {table_name!r} resolves to a "
                        f"charset with {len(resolved)} distinct character(s) "
                        f"({charset_spec!r} -> {resolved!r}). A degenerate charset (<2 "
                        "distinct characters) leaves the column unmasked at run; use a "
                        "named charset (digits/alpha/ALPHA/alphanum/ALPHANUM) or a "
                        "literal charset with at least 2 distinct characters."
                    ),
                )

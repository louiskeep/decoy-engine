"""Categorical admission gates for chunked execution (slice C1).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_bucket_perturb.py` / `_chunked_code_set.py`.

A deterministic categorical column is row-local: each row maps from its own
canonicalized source value and `(mask_key, namespace)` through `derive_index`, so
per-chunk masking reproduces whole-column masking value for value. A
non-deterministic one draws a whole-column unseeded vector, which no chunking can
reproduce, so it stays rejected here with its own code (`NONDETERMINISTIC_CODE`,
owned by slice C1b, which lifts it).

"Deterministic" is `is_deterministic_categorical`, the single definition the native
determinism gate and the seed envelope share, so the `allow_collisions: true` alias
counts exactly as `deterministic: true` does.

A deterministic column that the native operator cannot run (numeric categories, a
non-string source, weights the CDF cannot build) is still chunk-safe: it passes this
gate and falls back to the oracle at routing, so it must never get the
non-deterministic code.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from decoy_engine.plan._errors import PlanCompileError

NONDETERMINISTIC_CODE = "categorical_nondeterministic_not_chunk_safe"


def is_nondeterministic(col_entry: dict[str, Any]) -> bool:
    # Lazy import: `execution.native` imports `_chunked`, so a top-level import cycles.
    from decoy_engine.execution.native._operator_config_rejections import (
        is_deterministic_categorical,
    )

    return not is_deterministic_categorical(col_entry)


def conditional_failures(col_entry: dict[str, Any]) -> list[str]:
    """Unmet chunk-safety conditions for a DETERMINISTIC categorical column.

    Determinism itself is checked by `is_nondeterministic` and reported under its own
    code; this covers the value-keyed inputs the mapping needs declared in config."""
    cfg = col_entry.get("provider_config") or {}
    failures: list[str] = []
    if not col_entry.get("namespace"):
        failures.append("requires a namespace (the value-keyed mapping derives from it)")
    if cfg.get("from_profile"):
        failures.append(
            "from_profile derives categories from the profile, which chunked "
            "mode builds from the first chunk only; declare categories "
            "explicitly"
        )
    elif not cfg.get("categories"):
        failures.append("requires explicit provider_config.categories")
    return failures


def reject_nondeterministic(columns: Sequence[str], *, table: str) -> None:
    """Fail closed when any categorical column in `columns` is non-deterministic."""
    if not columns:
        return
    raise PlanCompileError(
        code=NONDETERMINISTIC_CODE,
        path=f"tables.{table}.columns",
        message=(
            f"categorical column(s) {', '.join(columns)} are not deterministic: the "
            "non-deterministic path draws one unseeded vector over the whole column, which "
            "is chunk-variant. Set `deterministic: true` (or `allow_collisions: true`) with "
            "a namespace to run it chunked."
        ),
    )

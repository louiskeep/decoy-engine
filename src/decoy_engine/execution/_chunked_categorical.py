"""Categorical admission gates for chunked execution (slice C1).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_bucket_perturb.py` / `_chunked_code_set.py`.

A deterministic categorical column is row-local: each row maps from its own
canonicalized source value and `(mask_key, namespace)` through `derive_index`, so
per-chunk masking reproduces whole-column masking value for value. A
non-deterministic one is seeded but position-keyed (by the row ordinal of the frame the
handler receives). Its chunked native route (C1b-ii) admits only a config that is complete
before any chunk (`positional_config_of_entry`: namespace, explicit string categories, a
buildable CDF); any other non-deterministic column is rejected here with its own code
(`NONDETERMINISTIC_CODE`) so it can never reach the oracle route by accident, and it may not
carry a `when:` (the filtered enumeration is not the global position).

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


def rejects_nondeterministic(col_entry: dict[str, Any]) -> bool:
    """True for a non-deterministic column the chunked route cannot run: every one that
    is not the config-complete seeded variant."""
    from decoy_engine.execution.native._categorical_positional import positional_config_of_entry

    return is_nondeterministic(col_entry) and positional_config_of_entry(col_entry) is None


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
            "non-deterministic path is position-keyed, and the chunked route (C1b-ii) runs it "
            "only with a namespace, explicit string categories and weights the CDF can build. "
            "Complete the config, or set `deterministic: true` (or `allow_collisions: true`) "
            "with a namespace."
        ),
    )


WHEN_CODE = "chunked_categorical_nondeterministic_when_not_supported"


def reject_nondeterministic_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a config-complete seeded categorical whose `when:` predicate is outside the
    closed grammar (C8-iii-d-2).

    A positional draw under a closed-grammar predicate IS reproducible: d-1 keys each selected
    row on its full-table position, and the chunked oracle leg composes the same positions, so
    a config-complete positional+`when:` column with a parseable predicate runs (string target
    and references are enforced per chunk by `_chunked_when_guard`, where schemas exist). A
    predicate outside the grammar cannot be checked for reference stability, so it is refused
    here. Columns that already fail the config veto keep its code instead.

    Raises:
        PlanCompileError: ``code='chunked_categorical_nondeterministic_when_not_supported'``.
    """
    from decoy_engine.execution._chunked_when_guard import closed_grammar_when
    from decoy_engine.execution.native._categorical_positional import positional_config_of_entry

    cols = sorted(
        str(c.get("name", "?"))
        for c in table_cfg.get("columns") or []
        if isinstance(c, dict)
        and isinstance(c.get("when"), str)
        and c["when"].strip()
        and positional_config_of_entry(c) is not None
        and not closed_grammar_when(c["when"])
    )
    if not cols:
        return
    raise PlanCompileError(
        code=WHEN_CODE,
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(cols)} combine a non-deterministic categorical with a "
            "'when:' predicate outside the closed grammar, which the chunked route cannot run: "
            "a predicate it cannot parse cannot be checked for chunk-stable (string) references, "
            "so reproducing the whole-frame selection per chunk is not guaranteed."
        ),
    )

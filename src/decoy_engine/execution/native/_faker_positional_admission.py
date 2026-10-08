"""Chunked admission of the non-deterministic REUSE Faker: stage A (config only).

Row `g` of a non-deterministic REUSE Faker draws `pool.values[derive_index(job_seed,
selection_namespace, encode_int(g), pool.size)]`, so a chunk reproduces the whole-frame draw
from its global row offset (`execution/_strategies/_faker_positional.py`). Stage A decides from
the column's config alone whether the chunked route may run it, and is read by every chunked
consumer: the compatibility veto, the `when:` gate, the static route decision, the evidence
planner and the output-type pin. Stage B (the first chunk's source dtype) only picks the leg: a
string source runs the native kernel, anything else runs the chunked oracle.

Unlike deterministic Faker, the provider allowlist is a stage-A condition. Before this route
a non-deterministic Faker always ran whole-frame, and a provider outside the string-output
allowlist can infer int in one oracle chunk and float in the next, which would turn a working
job into a `chunked_schema_mismatch`. Those columns keep running whole-frame.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from decoy_engine.execution._operator_registry import OPERATORS
from decoy_engine.execution.native._categorical_positional import positional_config_for_column
from decoy_engine.plan._errors import PlanCompileError

WHEN_CODE = "chunked_faker_nondeterministic_when_not_supported"

_PROVIDER_ALLOWLIST = OPERATORS["faker"].provider_allowlist or frozenset()


@dataclass(frozen=True)
class PositionalFakerConfig:
    """What stage A keeps: the CONFIGURED namespace, which the pool identity uses. The
    selection namespace is derived per table and column by the resolver."""

    namespace: str | None


def is_positional_faker_entry(col_entry: Mapping[str, Any]) -> bool:
    """True for a faker column on the position-keyed path: not deterministic (`allow_collisions`
    is the compile-time alias for it) and REUSE (an absent mode defaults to it)."""
    return (
        col_entry.get("strategy") == "faker"
        and not col_entry.get("deterministic")
        and not col_entry.get("allow_collisions")
        and col_entry.get("cardinality_mode") in (None, "reuse")
    )


def positional_faker_failures(col_entry: Mapping[str, Any]) -> list[str]:
    """Unmet stage-A conditions of a position-keyed faker column; empty when admitted."""
    cfg = col_entry.get("provider_config") or {}
    failures: list[str] = []
    if col_entry.get("pool_size") is None and cfg.get("pool_size") is None:
        failures.append(
            "position-keyed faker requires an explicit pool_size (top-level or "
            "provider_config.pool_size) as the chunked capacity declaration (the non-chunked "
            "default of 10000 is not applied silently here)"
        )
    provider = col_entry.get("provider")
    if provider not in _PROVIDER_ALLOWLIST:
        failures.append(
            f"provider {provider!r} is not in the chunked allowlist "
            f"({', '.join(sorted(_PROVIDER_ALLOWLIST))}); only allowlisted providers run chunked, "
            "so this column runs whole-frame"
        )
    return failures


def positional_faker_config_of_entry(col_entry: Mapping[str, Any]) -> PositionalFakerConfig | None:
    """The stage-A artifact for a raw column entry, or None when it is not a position-keyed
    faker column or its config fails stage A."""
    if not is_positional_faker_entry(col_entry) or positional_faker_failures(col_entry):
        return None
    return PositionalFakerConfig(col_entry.get("namespace") or None)


def positional_faker_config_for_column(
    config: Mapping[str, Any], table: str, column: str
) -> PositionalFakerConfig | None:
    """`positional_faker_config_of_entry` for `column` of `table` in a whole job config."""
    for table_cfg in config.get("tables") or ():
        if not isinstance(table_cfg, Mapping) or table_cfg.get("name") != table:
            continue
        for col in table_cfg.get("columns") or ():
            if isinstance(col, Mapping) and col.get("name") == column:
                return positional_faker_config_of_entry(col)
    return None


def reject_nondeterministic_faker_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a config-complete stage-A faker whose `when:` predicate is outside the closed
    grammar (C8-iii-d-2).

    A position-keyed draw under a closed-grammar predicate IS reproducible: d-1 keys each
    selected row on its full-table position, which the chunked oracle leg composes identically,
    so a config-complete positional faker with a parseable predicate runs (string target and
    references are enforced per chunk by `_chunked_when_guard`, where schemas exist). A predicate
    outside the grammar cannot be checked for reference stability, so it is refused here. Columns
    that already fail stage A keep their own veto code.

    Raises:
        PlanCompileError: ``code='chunked_faker_nondeterministic_when_not_supported'``.
    """
    from decoy_engine.execution._chunked_when_guard import closed_grammar_when

    cols = sorted(
        str(c.get("name", "?"))
        for c in table_cfg.get("columns") or []
        if isinstance(c, dict)
        and isinstance(c.get("when"), str)
        and c["when"].strip()
        and positional_faker_config_of_entry(c) is not None
        and not closed_grammar_when(c["when"])
    )
    if not cols:
        return
    raise PlanCompileError(
        code=WHEN_CODE,
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(cols)} combine a non-deterministic faker with a 'when:' "
            "predicate outside the closed grammar, which the chunked route cannot run: a "
            "predicate it cannot parse cannot be checked for chunk-stable (string) references, "
            "so reproducing the whole-frame selection per chunk is not guaranteed."
        ),
    )


def chunked_positional_column(
    config: dict[str, Any], table: str, strategy: str, column: str
) -> bool:
    """True for a column the chunked route runs as a position-keyed draw: the seeded
    non-deterministic categorical or the non-deterministic REUSE faker. Their full-frame
    `fallback_policy` is not native (the full-frame operators are source-keyed), so this is
    the one place the chunked-only admission is decided."""
    if strategy == "categorical":
        return positional_config_for_column(config, table, column) is not None
    if strategy == "faker":
        return positional_faker_config_for_column(config, table, column) is not None
    return False

"""DE-03: fail-closed output projection.

The seed envelope only carries the columns the plan DECLARES intent for: a
column with a real strategy (or `strategy: passthrough`) lands in a table's
`per_column`, and a composite-FK member lands in its `per_group` coherent set.
A column with no strategy is dropped from the envelope, and a table with an
empty/omitted `columns:` block gets no `TableSeed` at all (see
`plan/_seed_envelope.py`). Because the work list is built only from the
envelope, those columns are never masked -- their raw values reach output
untouched, silently. That silent raw-PII passthrough is the DE-03 bug.

This module is the mandatory, deterministic, PRE-publication schema-closure
gate. `known_output_columns` is the plan's declared output surface for a table;
`enforce_output_projection` fails closed at each emission route when the frame
about to be written carries a column the plan never declared. Compile cannot
see runtime schema drift (a column present in the source but absent from the
profile the plan was built against), so enforcement lives at the adapter, not
the compiler. It composes with -- and never calls into -- the optional,
probabilistic `storm/postmask/residual_pii` advisory (two distinct layers).

Policy fork (Cam, 2026-07-13): `unconfigured_column_policy` defaults to `warn`
while `release.is_pre_ga()` so existing configs keep working through the
migration window (undeclared columns log a structured warning and still pass
through), and to `error` at GA so fail-closed binds automatically with no
manual flip. An explicit `global_settings.unconfigured_column_policy` overrides
the phase default.

The warn path surfaces through the engine's structured warnings channel
(`QualityWarning`, returned so the adapter folds it into
`ExecutionResult.warnings`) -- never stdout/stderr.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.release import is_pre_ga

if TYPE_CHECKING:
    from collections.abc import Iterable

    from decoy_engine.plan._types import Plan

UnconfiguredColumnPolicy = Literal["warn", "error"]

# `provider` is a required field on QualityWarning with no default; this gate is
# not provider-bound, so it stamps a stable sentinel here rather than a real
# provider name.
_PROJECTION_WARNING_SOURCE = "output_projection"

_UNDECLARED_CODE = "undeclared_output_columns"


def known_output_columns(plan: Plan, table: str) -> frozenset[str]:
    """The columns the plan declares as legitimate output for `table`.

    Union of the table's `per_column` keys (every strategied / passthrough
    column, and every scalar FK child, which must be a work node to resolve at
    all) and every composite-FK `per_group` coherent column. A table absent
    from `plan.seed_envelope.per_table` -- an empty/omitted `columns:` block, or
    a generate table, which the mask plan never covers -- returns the empty set:
    fail-closed by construction, so a wholly-undeclared table cannot pass a
    non-empty output frame.
    """
    for name, table_seed in plan.seed_envelope.per_table:
        if name != table:
            continue
        cols: set[str] = {col_name for col_name, _ in table_seed.per_column}
        for _canonical_key, group_seed in table_seed.per_group:
            cols.update(group_seed.coherent_columns)
        return frozenset(cols)
    return frozenset()


def resolve_unconfigured_column_policy(
    config: Mapping[str, object] | None,
) -> UnconfiguredColumnPolicy:
    """Resolve the effective policy: explicit `global_settings` setting wins,
    else the release-phase default (`warn` pre-GA, `error` at GA).

    A single resolver so every emission route reads the same value; the
    phase-coupled default means fail-closed engages automatically at GA with no
    per-config flip to forget.
    """
    settings = (config or {}).get("global_settings")
    if isinstance(settings, Mapping):
        explicit = settings.get("unconfigured_column_policy")
        if explicit == "warn" or explicit == "error":
            return explicit
    return "warn" if is_pre_ga() else "error"


def enforce_output_projection(
    table: str,
    output_columns: Iterable[str],
    plan: Plan,
    policy: UnconfiguredColumnPolicy | None = None,
    *,
    extra_known: frozenset[str] = frozenset(),
) -> list[QualityWarning]:
    """Fail closed on any output column the plan never declared for `table`.

    `undeclared = set(output_columns) - known_output_columns(plan, table) -
    extra_known`. Empty -> return no warnings. Non-empty and `policy == "error"`
    -> raise `ExecutionError(code="undeclared_output_columns")` naming the table
    + offending columns. Non-empty and `policy == "warn"` -> return one
    structured `QualityWarning` (the caller folds it into
    `ExecutionResult.warnings`); the columns still pass through.

    `extra_known` carries route-local legitimate columns not in the seed
    envelope -- the out-of-core runner passes its FK-resolved child columns so
    an FK column can never false-positive there.

    `policy=None` resolves to the release-phase default (`warn` pre-GA) so a
    direct adapter call with no threaded policy still fails closed at GA without
    silently leaking pre-GA.
    """
    resolved = policy if policy is not None else ("warn" if is_pre_ga() else "error")
    declared = known_output_columns(plan, table) | extra_known
    undeclared = sorted(set(output_columns) - declared)
    if not undeclared:
        return []
    if resolved == "error":
        raise ExecutionError(
            code=_UNDECLARED_CODE,
            message=(
                f"table {table!r} would emit column(s) {undeclared} that the plan "
                "does not declare a strategy for (not masked, not generated, not an "
                "FK-resolved column). Raw values would reach output unmasked. Declare "
                "each column with a real strategy or `strategy: passthrough`, or set "
                "`global_settings.unconfigured_column_policy: warn` to allow "
                "passthrough with a warning."
            ),
        )
    return [
        QualityWarning(
            code=_UNDECLARED_CODE,
            provider=_PROJECTION_WARNING_SOURCE,
            column=None,
            detail={"table": table, "undeclared_columns": undeclared},
        )
    ]

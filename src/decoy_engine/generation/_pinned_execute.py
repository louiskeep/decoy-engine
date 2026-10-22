"""Execute a ``PinnedPlan`` with a caller seed.

Output is a pure function of (plan, seed, runtime identity). Every stage that could vary between
processes or days is closed here:

- seed: required and validated; no ``derive_key`` parameter exists, and the column generators
  receive ``None``, so the unkeyed SHA-256 root over (seed, column fingerprint) is the only path;
- RNG: stdlib and NumPy generators are seeded from that root, never from the OS or the clock;
- clock: ``windowed_date`` resolves ``now``/``today`` against the plan's ``reference_date``;
- external bytes: statistical specs come from the plan, and the digest reaches every seed
  derivation (including null injection), so no snapshot path is opened;
- ordering: tables run in a topological order over sorted names and come back sorted by name,
  columns in declared order, independent of ``PYTHONHASHSEED``.

Methodology: reproducible-builds style input pinning (all inputs named by the plan, nothing read
from the environment) over the engine's existing HMAC-SHA256 derivation.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

from decoy_engine.generation._pinned_compile import refuse_live_tokens, reject_unpinnable
from decoy_engine.generation._pinned_plan import (
    PinnedModeError,
    PinnedPlan,
    runtime_identity,
    validate_seed,
)
from decoy_engine.generation.statistical import StatisticalSpec, spec_from_dict


def _order_tables(plan: PinnedPlan) -> list[str]:
    """Parents before children; ties broken by sorted name so the order is hash-seed stable."""
    deps = {t.name: sorted(t.depends_on) for t in plan.tables}
    # Messages name tables by position: names in a deserialized plan are untrusted text.
    pos = {t.name: i for i, t in enumerate(plan.tables)}
    order: list[str] = []
    done: set[str] = set()
    finished: set[str] = set()
    for start in sorted(deps):
        if start in done:
            continue
        done.add(start)
        stack = [(start, iter(deps[start]))]
        while stack:
            node, parents = stack[-1]
            parent = next(parents, None)
            if parent is None:
                order.append(node)
                finished.add(node)
                stack.pop()
                continue
            if parent not in deps:
                raise PinnedModeError(
                    code="pinned_plan_malformed",
                    message=f"table #{pos[node]} depends on a table the plan does not declare",
                )
            if parent in done and parent not in finished:
                raise PinnedModeError(
                    code="pinned_plan_malformed",
                    message=f"tables #{pos[node]} and #{pos[parent]} depend on each other",
                )
            if parent not in done:
                done.add(parent)
                stack.append((parent, iter(deps[parent])))
    return order


def _scan_plan_for_tokens(plan: PinnedPlan) -> None:
    """One pass over the decoded plan (configs and specs included) before anything validates it."""
    try:
        decoded = plan.to_dict()
        for t in decoded["tables"]:
            for c in t["columns"]:
                c["config"] = json.loads(c.pop("config_json"))
        for st in decoded["statistical"]:
            st["spec"] = json.loads(st.pop("spec_json"))
    except (ValueError, TypeError):
        raise PinnedModeError(
            code="pinned_plan_malformed", message="a column config or spec is not valid JSON"
        ) from None
    refuse_live_tokens(decoded, "plan")


def _check_plan(plan: PinnedPlan) -> dict[tuple[str, str], StatisticalSpec]:
    """Re-check a plan that may have been deserialized or hand-built, then decode its specs."""
    if plan.runtime_identity != runtime_identity():
        raise PinnedModeError(
            code="pinned_runtime_changed",
            message="the runtime identity differs from the one the plan was pinned under",
        )
    reject_unpinnable(
        {
            "tables": [
                {
                    "name": t.name,
                    "generate_columns": [json.loads(c.config_json) for c in t.columns],
                }
                for t in plan.tables
            ]
        }
    )
    for ti, t in enumerate(plan.tables):
        for ci, c in enumerate(t.columns):
            if json.loads(c.config_json).get("name") != c.name:
                raise PinnedModeError(
                    code="pinned_plan_malformed",
                    message=f"table #{ti} column #{ci}: name differs from its config",
                )
    specs: dict[tuple[str, str], StatisticalSpec] = {}
    for s in plan.statistical:
        if not s.snapshot_digest:
            raise PinnedModeError(
                code="pinned_statistical_digest_missing",
                message=f"table {s.table!r} column {s.column!r}: no pinned snapshot digest",
            )
        spec = spec_from_dict(json.loads(s.spec_json))
        if spec.snapshot_digest != s.snapshot_digest:
            raise PinnedModeError(
                code="pinned_statistical_digest_missing",
                message=f"table {s.table!r} column {s.column!r}: spec digest does not match",
            )
        specs[(s.table, s.column)] = spec
    for t in plan.tables:
        for c in t.columns:
            if json.loads(c.config_json).get("type") == "statistical" and (
                (t.name, c.name) not in specs
            ):
                raise PinnedModeError(
                    code="pinned_statistical_digest_missing",
                    message=f"table {t.name!r} column {c.name!r}: no pinned snapshot digest",
                )
    return specs


# Process-global pandas options that can change generated values (inf handling in count(),
# float reductions via bottleneck/numexpr, string and downcast behaviour). Pinned to the pandas
# defaults without optional accelerators, so ambient settings cannot move the bytes.
_PANDAS_OPTIONS = (
    "mode.use_inf_as_na",
    False,
    "compute.use_bottleneck",
    False,
    "compute.use_numexpr",
    False,
    "mode.string_storage",
    "python",
    "future.infer_string",
    False,
    "future.no_silent_downcasting",
    False,
    "mode.copy_on_write",
    False,
)


def execute_pinned(plan: PinnedPlan, seed: int) -> dict[str, pa.Table]:
    """Generate every table of ``plan`` under ``seed``; byte-identical for equal inputs."""
    import warnings

    import pandas as pd

    _scan_plan_for_tokens(plan)
    seed = validate_seed(seed)
    specs = _check_plan(plan)
    with warnings.catch_warnings():
        # mode.use_inf_as_na is deprecated; setting it to its default is still the pin.
        warnings.simplefilter("ignore", FutureWarning)
        with pd.option_context(*_PANDAS_OPTIONS):
            return _run(plan, seed, specs)


def _run(
    plan: PinnedPlan, seed: int, specs: dict[tuple[str, str], StatisticalSpec]
) -> dict[str, pa.Table]:
    from decoy_engine.generation.synthesize import _generate_column

    by_name = {t.name: t for t in plan.tables}
    pools: dict[str, pa.Table] = {}
    for name in _order_tables(plan):
        table = by_name[name]
        data: dict[str, list[Any]] = {}
        for pcol in table.columns:
            col = json.loads(pcol.config_json)
            data[pcol.name] = _generate_column(
                col,
                table.row_count,
                seed,
                None,
                pools,
                None,
                data,
                table_name=name,
                statistical_specs=specs,
                reference_date=plan.reference_date,
                pinned=True,
            )
        pools[name] = pa.table(data)
    return {name: pools[name] for name in sorted(pools)}

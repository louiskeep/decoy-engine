"""Shared builders for the C8-iii-c acceptance tests (every `when:` goes through the grammar).

Plan: docs/plans/2026-10-07-c8-iii-c-rawdict-when.md (rev 3). A test compiles a VALID job,
then either injects an out-of-grammar `when` into the raw config dict (the compile tests) or
rebuilds the compiled Plan with `dataclasses.replace` (the plan-level tests), so no guard is
bypassed to reach the code under test.
"""

from __future__ import annotations

import dataclasses
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.plan._types import ColumnSeed, Plan, SeedEnvelope, TableSeed
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_support as mt

SENTINEL = "SENTINEL-4417"


@dataclass
class Job:
    config: dict[str, Any]
    sources: dict[str, pa.Table]
    plan: Plan
    graph: Any
    namespaces: Any
    registry: Any


def compile_job(config: dict[str, Any]) -> tuple[Plan, Any, Any, Any]:
    """Compile a config dict into (plan, graph, ns_registry, registry), as `run_pipeline` does."""
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    profile = profile_source(config, seed=_normalize_job_seed_int(config))
    plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
    ns = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships, namespace_registry=ns, orphan_policy_lookup=lookup
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    return plan, graph, ns, get_default_registry()


def source_table(n: int = 6) -> pa.Table:
    """The columns every compile predicate in the tests reads."""
    return pa.table(
        {
            "s": pa.array([f"v{i}" for i in range(n)]),
            "x": pa.array(list(range(n)), pa.int64()),
            "a": pa.array(list(range(n)), pa.int64()),
            "b": pa.array(list(range(n)), pa.int64()),
            "amount": pa.array([float(i) for i in range(n)]),
            "name": pa.array([f"n{i}" for i in range(n)]),
            "region": pa.array(["US" if i % 2 else "EU" for i in range(n)]),
        }
    )


def plain_columns(extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    cols = [support.redact_col("s")]
    cols += [support.pass_col(c) for c in ("x", "a", "b", "amount", "name", "region")]
    return cols + (extra or [])


def single_table_config(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    return mt.build_job(tmp_path, {"t": (plain_columns(), source_table())})


def two_table_job(tmp_path: Path) -> Job:
    """Tables `a` and `b`, each with a redacted `s` and a passthrough `x`."""
    tables = {
        name: (
            [support.redact_col("s"), support.pass_col("x")],
            pa.table(
                {
                    "s": pa.array([f"{name}{i}" for i in range(6)]),
                    "x": pa.array(list(range(6)), pa.int64()),
                }
            ),
        )
        for name in ("a", "b")
    }
    cfg, sources = mt.build_job(Path(str(tmp_path)), tables)
    plan, graph, ns, registry = compile_job(cfg)
    return Job(cfg, sources, plan, graph, ns, registry)


def with_when(config: dict[str, Any], table: str, column: str, when: Any) -> dict[str, Any]:
    """A deep copy of `config` whose `table.column` carries a raw-dict `when` (no validation)."""
    import copy

    out = copy.deepcopy(config)
    for tbl in out["tables"]:
        if tbl["name"] == table:
            for col in tbl["columns"]:
                if col["name"] == column:
                    col["when"] = when
                    return out
    raise AssertionError(f"no column {table}.{column}")


def plan_with_when(plan: Plan, table: str, column: str, when: Any) -> Plan:
    """The compiled `plan` rebuilt with `dataclasses.replace` so `table.column` carries `when`."""
    new_tables = []
    found = False
    for name, ts in plan.seed_envelope.per_table:
        if name == table:
            cols = []
            for cname, cs in ts.per_column:
                if cname == column:
                    cs = dataclasses.replace(cs, when=when)
                    found = True
                cols.append((cname, cs))
            ts = dataclasses.replace(ts, per_column=tuple(cols))
        new_tables.append((name, ts))
    assert found, f"no seed {table}.{column}"
    env = dataclasses.replace(plan.seed_envelope, per_table=tuple(new_tables))
    return dataclasses.replace(plan, seed_envelope=env)


def plan_with_extra_seed(plan: Plan, table: str, column: str, when: Any) -> Plan:
    """The compiled `plan` plus a seed for a table the caller will not supply a source for."""
    template = plan.seed_envelope.per_table[0][1].per_column[0][1]
    seed = dataclasses.replace(template, when=when)
    extra = (table, TableSeed(per_column=((column, seed),)))
    env: SeedEnvelope = dataclasses.replace(
        plan.seed_envelope, per_table=(*plan.seed_envelope.per_table, extra)
    )
    return dataclasses.replace(plan, seed_envelope=env)


def column_seed(plan: Plan, table: str, column: str) -> ColumnSeed:
    for name, ts in plan.seed_envelope.per_table:
        if name == table:
            for cname, cs in ts.per_column:
                if cname == column:
                    return cs
    raise AssertionError(f"no seed {table}.{column}")


def rendered(exc: BaseException) -> str:
    """Everything an operator could see: str(exc) plus the full chained traceback."""
    return str(exc) + "\n" + "".join(traceback.format_exception(exc))


def logged(exc: BaseException) -> str:
    """The output of a logging handler that calls `logger.error(..., exc_info=True)`."""
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("c8_iii_c_probe")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("run failed", exc_info=exc)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def assert_no_sentinel(exc: BaseException) -> None:
    assert SENTINEL not in str(exc)
    assert SENTINEL not in rendered(exc)
    assert SENTINEL not in logged(exc)

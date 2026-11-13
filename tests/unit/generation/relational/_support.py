"""Shared builders for the GEN-REL acceptance tests (test-side only)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.generation._plan_entry import generate_tables, generate_tables_streamed
from decoy_engine.generation.pinned import compile_pinned_plan, execute_pinned
from decoy_engine.plan import compile_plan
from tests.parity._f0_canonical import table_fingerprint
from tests.unit._dps_helpers import empty_profile
from tests.unit.generation.unique._gu_e2_support import cfg, ref, seq_parent, table

REF = datetime(2026, 10, 22, tzinfo=timezone.utc)
SEED = 4242
ROUTES = ("resident", "streamed", "legacy", "pinned")
POLICIES = ("fail", "warn", "preserve", "remap")
CID = {"name": "cid", "type": "sequence", "start": 1}
# A column the streaming core declines, so the whole plan takes the whole-table route.
LEGACY_ONLY = {"name": "twice", "type": "derived", "expression": "cid * 2"}


def relationship(
    policy: str, parent: str = "parents", children: tuple[tuple[str, str], ...] = (("kids", "pid"),)
) -> dict[str, Any]:
    return {
        "parent": {"table": parent, "columns": ["id"]},
        "children": [{"table": t, "columns": [c]} for t, c in children],
        "orphan_policy": policy,
    }


def family(
    parent_rows: int,
    child_rows: int,
    *,
    policy: str | None = None,
    legacy: bool = False,
    parent: dict[str, Any] | None = None,
    seed: int = SEED,
    **ref_options: Any,
) -> dict[str, Any]:
    """`parents` and `kids`, with `kids.pid` referencing `parents.id`."""
    child_cols = [CID, ref("pid", "parents", **ref_options)]
    if legacy:
        child_cols.append(LEGACY_ONLY)
    config = cfg(
        [parent or seq_parent("parents", parent_rows), table("kids", child_rows, child_cols)],
        seed,
    )
    if policy is not None:
        config["relationships"] = [relationship(policy)]
    return config


def plan_of(config: dict[str, Any]) -> Any:
    return compile_plan(copy.deepcopy(config), empty_profile(), decoy_engine_version="test")


def run_route(
    route: str, config: dict[str, Any], tmp_path: Path, on_warning: Any = None
) -> dict[str, pa.Table]:
    """Generate `config` on one of the four routes and return its tables.

    `legacy` needs a config built with `legacy=True`; the caller asserts the admission verdict.
    """
    if route == "pinned":
        return execute_pinned(compile_pinned_plan(copy.deepcopy(config), REF), SEED)
    plan = plan_of(config)
    if route == "streamed":
        from decoy_engine.execution import ParquetTransactionalSink

        target = tmp_path / "out"
        result = generate_tables_streamed(
            plan,
            sink=ParquetTransactionalSink(target),
            budget_bytes=256 * 1024 * 1024,
            batch_rows=4,
            on_warning=on_warning,
        )
        assert result.mode == "streamed", result.reason
        return {p.stem: pq.read_table(p) for p in sorted(target.glob("*.parquet"))}
    return generate_tables(plan, on_warning=on_warning)


def digest(tbl: pa.Table) -> str:
    """The canonical table digest the Phase F oracle uses (layout independent)."""
    return str(table_fingerprint(tbl.replace_schema_metadata(None))["table"])


def is_safe(exc: BaseException) -> bool:
    return exc.__cause__ is None and exc.__context__ is None

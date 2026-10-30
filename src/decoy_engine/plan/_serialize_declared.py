"""YAML (de)serialization of the Plan's `declared_casting` block (F06, plan_version 3).

A stored plan is untrusted input: loading re-validates every contract through the catalog, so a
plan that names an unknown format or catalog version never reaches a run.
"""

from __future__ import annotations

from typing import Any

from decoy_engine import reasons
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import (
    DeclaredCastingPlan,
    PlanDeclaredColumn,
    freeze_json,
    unfreeze_json,
)


def declared_casting_to_dict(block: DeclaredCastingPlan) -> dict[str, Any]:
    out: dict[str, Any] = {
        "catalog_version": block.catalog_version,
        "columns": [
            {
                "table": c.table,
                "column": c.column,
                "strategy": c.strategy,
                "contract": unfreeze_json(c.contract),
            }
            for c in block.columns
        ],
    }
    if block.quarantine is not None:
        out["quarantine"] = unfreeze_json(block.quarantine)
    return out


def declared_casting_from_dict(data: dict[str, Any]) -> DeclaredCastingPlan:
    """Load and re-validate a stored block: a stored plan is untrusted input, and a plan that
    names an unknown format or catalog version must never reach a run."""
    from decoy_engine.contracts.declared_casting import (
        CATALOG_VERSION,
        DeclaredCastContractError,
        build_contract,
    )

    if data.get("catalog_version") != CATALOG_VERSION:
        raise PlanCompileError(
            code=reasons.plan.DECLARED_CAST_FORMAT_UNKNOWN,
            path="<declared_casting.catalog_version>",
            message="The stored declared-casting catalog version is not supported by this engine.",
        )
    columns: list[PlanDeclaredColumn] = []
    for raw in data.get("columns") or ():
        path = f"<declared_casting {raw.get('table')!s}.{raw.get('column')!s}>"
        try:
            contract = build_contract(dict(raw["contract"]))
        except DeclaredCastContractError as exc:
            raise PlanCompileError(code=exc.code, path=path, message=exc.message) from None
        columns.append(
            PlanDeclaredColumn(
                table=raw["table"],
                column=raw["column"],
                strategy=raw["strategy"],
                contract=freeze_json(contract.to_payload()),
            )
        )
    quarantine = data.get("quarantine")
    return DeclaredCastingPlan(
        catalog_version=data["catalog_version"],
        columns=tuple(columns),
        quarantine=freeze_json(quarantine) if quarantine is not None else None,
    )

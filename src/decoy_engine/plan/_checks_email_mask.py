"""Plan-compile check for email_mask columns.

The one choke point for the strategy's options, run by both `compile_plan` branches and by
`run_config_only_checks`, so every authoring path (CLI, hand-written YAML, platform) is checked
before a source is read. The handler re-checks the same shape as a backstop.

Refuses: `keep` other than the exact strings `domain` / `domain_class`; any other
`provider_config` key (a typo would otherwise be ignored); no namespace (the token is keyed on
it); and `email_mask` as a `nested` child, since nested runs its child per flattened JSON leaf,
outside the parse and dtype rules this strategy is tested under.

Returns ``("email_mask_config",)`` only when the config uses the strategy, so a config without
it reports the same `checks_passed`, serialises to the same plan and hashes the same as before
the strategy existed (the F06 `declared_casting_contract` pattern).

Messages name the table, column and option, never a configured or source value.
"""

from __future__ import annotations

from typing import Any

from decoy_engine import reasons
from decoy_engine.execution._strategies._email_mask._address import KEEP_CHOICES
from decoy_engine.plan._errors import PlanCompileError

_NAME = "email_mask"
_ALLOWED_KEYS = frozenset({"keep"})


def _check_column(table: str, col: dict[str, Any]) -> bool:
    """Raise on a bad email_mask column; True when the column is one."""
    name = col.get("name", "?")
    path = f"tables.{table}.columns.{name}"
    where = f"column {name!r} in table {table!r}"
    pc = col.get("provider_config")
    pc = pc if isinstance(pc, dict) else {}
    if col.get("strategy") == "nested" and pc.get("strategy") == _NAME:
        raise PlanCompileError(
            code=reasons.plan.EMAIL_MASK_UNSUPPORTED_IN_NESTED,
            path=f"{path}.provider_config.strategy",
            message=f"{where} wraps email_mask in nested; move email_mask to a top-level column.",
        )
    if col.get("strategy") != _NAME:
        return False
    unknown = sorted(str(k) for k in pc if k not in _ALLOWED_KEYS)
    if unknown:
        raise PlanCompileError(
            code=reasons.plan.EMAIL_MASK_OPTION_UNKNOWN,
            path=f"{path}.provider_config.{unknown[0]}",
            message=f"email_mask {where} sets an unknown option; the only option is keep.",
        )
    keep = pc.get("keep")
    if not isinstance(keep, str) or keep not in KEEP_CHOICES:
        raise PlanCompileError(
            code=reasons.plan.EMAIL_MASK_KEEP_INVALID,
            path=f"{path}.provider_config.keep",
            message=(
                f"email_mask {where} needs keep set to 'domain' or 'domain_class' "
                f"(got a {type(keep).__name__} that is neither)."
            ),
        )
    if not col.get("namespace"):
        raise PlanCompileError(
            code=reasons.plan.EMAIL_MASK_REQUIRES_NAMESPACE,
            path=f"{path}.namespace",
            message=f"email_mask {where} needs a namespace; the masked local part is keyed on it.",
        )
    return True


def check_email_mask_config(config: dict[str, Any]) -> tuple[str, ...]:
    """Refuse a bad email_mask column; report the check only when one is configured."""
    used = False
    tables = config.get("tables")
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict):
            continue
        for col in table.get("columns") or []:
            if isinstance(col, dict):
                used = _check_column(str(table.get("name", "?")), col) or used
    return ("email_mask_config",) if used else ()

"""validate_plan: the single compile-validation entry point (engine-v2 S10 slice 1).

S1-S9 each shipped their own compile-time checks (rows 1-9 of the ownership
table). The operating model wants one call that runs every compile check and
reports a structured result instead of raising. That consolidation ALREADY
EXISTS: it is `compile_plan`, which runs the nine checks in their load-bearing,
data-flow-dependent order (the orphan-policy lookup feeds the relationship-graph
build, etc.). Re-deriving that orchestration behind a parallel check registry
would duplicate `compile_plan` and inevitably drift from it (the R11 failure mode
inverted), so `validate_plan` is a THIN PUBLIC WRAPPER over `compile_plan`:

- it adds NO check logic and imports NO individual check function;
- on success it returns the `checks_passed` / `checks_skipped` / `warnings`
  straight off the compiled `Plan.plan_compile`;
- on failure it CATCHES `PlanCompileError` / `PoolCapacityError` and returns
  `ok=False` with a structured `PlanCheckError` rather than raising.

Plan-compile validation is ALWAYS ON (it is `compile_plan`); the opt-in
post-execution scan suite lives in `decoy_engine.validation.post` (later slices).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from decoy_engine.generation.pool._errors import PoolCapacityError
from decoy_engine.plan._compile import compile_plan
from decoy_engine.plan._errors import PlanCompileError

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile import Profile


@dataclass(frozen=True)
class PlanCheckError:
    """A single compile-check failure, surfaced instead of raised.

    Mirrors `PlanCompileError` (code + path + message). `path` is None for a
    `PoolCapacityError`, which carries no YAML path. S10 introduces no new compile
    codes; `code` reuses the existing check codes.
    """

    code: str
    path: str | None
    message: str


@dataclass(frozen=True)
class PlanValidationResult:
    """The structured outcome of `validate_plan`.

    On success: `ok=True`, the `Plan` is attached, and `checks_passed` /
    `checks_skipped` / `warnings` come straight off `Plan.plan_compile`. On
    failure: `ok=False`, `error` carries the first failing check, and `plan` is
    None (compile aborted at the failing check, exactly as `compile_plan` does).
    """

    ok: bool
    checks_passed: tuple[str, ...] = ()
    checks_skipped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: PlanCheckError | None = None
    plan: Plan | None = field(default=None)


def validate_plan(
    config: dict[str, Any],
    profile: Profile,
    *,
    decoy_engine_version: str,
    no_profile: bool = False,
) -> PlanValidationResult:
    """Run every compile-time check (via `compile_plan`) and report the result.

    Same inputs as `compile_plan`; the checks run in their existing order inside
    it. Returns a `PlanValidationResult` rather than raising: a failing check
    yields `ok=False` + a `PlanCheckError`; a clean compile yields `ok=True` with
    the `Plan` attached. This is the keystone S11-S13 consume to validate a plan
    without a try/except at every call site.
    """
    try:
        plan = compile_plan(
            config, profile, decoy_engine_version=decoy_engine_version, no_profile=no_profile
        )
    except PlanCompileError as exc:
        return PlanValidationResult(
            ok=False, error=PlanCheckError(code=exc.code, path=exc.path, message=exc.message)
        )
    except PoolCapacityError as exc:
        return PlanValidationResult(
            ok=False, error=PlanCheckError(code=exc.code, path=None, message=exc.message)
        )
    result = plan.plan_compile
    return PlanValidationResult(
        ok=True,
        checks_passed=result.checks_passed,
        checks_skipped=result.checks_skipped,
        warnings=result.warnings,
        plan=plan,
    )

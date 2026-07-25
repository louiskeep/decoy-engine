"""Typed exceptions for FK-aware subsetting (Sprint G, SS1-SS5).

Mirrors the repo's typed-error house style (`decoy_engine.errors.DecoyError`,
`execution._errors.ExecutionError`): every subset error carries a machine-readable
`code` plus a human `message`. `SubsetPreflightError` and `SubsetBudgetExceededError`
additionally carry structured payloads (a report / budget-attribution fields) so
callers can branch on data instead of parsing message text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.subset._types import FkPreflightReport


class SubsetError(DecoyError):
    """Base class for every error raised by `decoy_engine.subset`."""

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class SubsetConfigError(SubsetError):
    """A subset input (policy, seed spec, config block) is malformed."""


class SubsetInternalError(SubsetError):
    """An internal invariant was violated (engine bug, not user input).

    Raised by the closure non-termination guard (section 4.3) and by
    `verify_closure` (section 4.5) when the post-closure re-check finds a
    violation that the fixpoint loop itself should have prevented.
    """


class SubsetPreflightError(SubsetError):
    """`run_subset_preflight` found one or more fail-closed conditions.

    Raised by `plan_subset` / `run_subset` when `report.passed` is False,
    guaranteeing SS2 (seed) and SS3 (closure) never run against a bad
    declaration. `code` is the first failure's code; the full report is
    available on `.report` for callers that want every failure at once.
    """

    def __init__(self, *, code: str, report: FkPreflightReport, message: str = "") -> None:
        self.report = report
        super().__init__(code=code, message=message)


class SubsetBudgetExceededError(SubsetError):
    """The fan-out closure exceeded the configured budget.

    HARD-FAIL CONTRACT (GATE-1 #3): raised before `output_dir` is ever
    created, so there is no partial Parquet to clean up. `code` is always
    `subset_budget_exceeded`. Structured fields let a caller report the
    scope, table, cap, actual count, and the top-contributing edge without
    parsing the message.
    """

    def __init__(
        self,
        *,
        scope: Literal["total", "table"],
        table: str | None,
        cap: int,
        actual: int,
        seed_total: int,
        edge_id: str,
    ) -> None:
        self.scope = scope
        self.table = table
        self.cap = cap
        self.actual = actual
        self.seed_total = seed_total
        self.edge_id = edge_id
        scope_desc = f"table {table!r} has" if scope == "table" else "subset output has"
        message = (
            f"fan-out budget exceeded: {scope_desc} {actual} rows > cap {cap} "
            f"(seed total {seed_total}); top contributing edge: {edge_id}. "
            "No output was written. Raise the budget, disable traversal on the "
            "offending edge, or shrink the seed."
        )
        super().__init__(code="subset_budget_exceeded", message=message)

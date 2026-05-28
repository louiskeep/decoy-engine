"""Exception hierarchy for the execution adapter (engine-v2 S9).

`ExecutionError` covers boundary/runner failures (orphan-FK violations, a cyclic
work ordering, an unsupported strategy). `StrategyError` is a narrower subclass
that wraps a strategy-internal failure so the runner can attribute it to a named
strategy at the boundary.

Codes used in S9:
- `cyclic_work_ordering`: the work-node dependency graph has a cycle (a bug; FK
  + composite ordering constraints should be acyclic).
- `orphan_fk_violation`: an orphan FK row hit an OrphanPolicy of `fail`.
- `unsupported_strategy`: no concrete handler for the named strategy.
- `dtype_unsafe_at_boundary`: an Arrow<->pandas conversion would lose or corrupt dtype.
"""

from __future__ import annotations


class ExecutionError(Exception):
    """Execution-boundary / runner failure. Carries a machine-readable code."""

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class StrategyError(ExecutionError):
    """A strategy-internal failure, attributed to a named strategy."""

    def __init__(self, *, code: str, strategy: str, message: str = "") -> None:
        self.strategy = strategy
        super().__init__(code=code, message=f"strategy={strategy!r}: {message}")

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

from decoy_engine.errors import ConfigError, DecoyError


class ExecutionError(DecoyError):
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


class SeqIneligibleError(ConfigError):
    """An explicit ``execution_mode="sequential"`` for a job that is not eligible for it.

    A typed, engine-authored identity (the governor reroutes on it, never on message text), so a
    general `ConfigError` whose text happens to say the same thing cannot steer routing."""


class OocIneligibleError(ConfigError):
    """An explicit ``execution_mode="out_of_core"`` for a job that is not eligible for it."""

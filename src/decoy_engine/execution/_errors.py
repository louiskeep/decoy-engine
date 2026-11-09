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

from typing import Any

from decoy_engine import reasons
from decoy_engine.errors import ConfigError, DecoyError
from decoy_engine.reason_adapter import tag_exception
from decoy_engine.reasons import Phase, ReasonCode


class ExecutionError(DecoyError):
    """Execution-boundary / runner failure. Carries a machine-readable code."""

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


def lazy_source_changed(table: str, detail: str) -> ExecutionError:
    """The one constructor of the `lazy_source_changed` refusal, for every lazy source backend."""
    return ExecutionError(
        code=reasons.runtime.LAZY_SOURCE_CHANGED,
        message=f"the source of table {table!r} changed between routing and the read: {detail}.",
    )


class StrategyError(ExecutionError):
    """A strategy-internal failure, attributed to a named strategy."""

    def __init__(self, *, code: str, strategy: str, message: str = "") -> None:
        self.strategy = strategy
        super().__init__(code=code, message=f"strategy={strategy!r}: {message}")


class DeclaredCastRouteError(ExecutionError):
    """A route or sink cannot honour a declared casting contract (F06); refused before any
    source is read. Its codes live in the ``route`` reason namespace (the adapter maps the class)."""


class DeclaredCastValueError(ExecutionError):
    """A ``fail``-policy parse or format failure (F06). ``declared_casting_counts`` is value-free
    evidence of how far the run got (`completeness: partial_before_abort`)."""

    def __init__(self, *, code: str, counts: dict[str, Any] | None = None) -> None:
        self.declared_casting_counts = counts
        super().__init__(code=code, message="A declared casting contract rejected a value.")


class QuarantineError(ExecutionError):
    """A protected-quarantine write, seal, access or retention failure (F06).

    ``cleanup_state`` reports how partial-artifact cleanup ended, apart from the original code."""

    def __init__(self, *, code: str, message: str = "", cleanup_state: str = "not_needed") -> None:
        self.cleanup_state = cleanup_state
        super().__init__(code=code, message=message)


def seq_ineligible(message: str) -> ConfigError:
    """An explicit ``execution_mode="sequential"`` for a job that is not eligible for it.

    Exactly a ``ConfigError`` (the public type is pinned) carrying the typed, engine-authored
    ``route.sequential_ineligible`` identity, so the governor reroutes on the identity and never
    on message text: a general `ConfigError` whose text says the same thing cannot steer routing.
    """
    return _tagged(ConfigError(message), reasons.route.SEQUENTIAL_INELIGIBLE)


def ooc_ineligible(message: str) -> ConfigError:
    """An explicit ``execution_mode="out_of_core"`` for a job that is not eligible for it."""
    return _tagged(ConfigError(message), reasons.route.OUT_OF_CORE_INELIGIBLE)


def _tagged(exc: ConfigError, reason: ReasonCode) -> ConfigError:
    tag_exception(exc, reason, phase=Phase.RESOLVE)
    return exc

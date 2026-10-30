"""Run-scoped memory ledger for bounded generation.

One `GenerationBudgetLedger` owns the remaining-bytes counter for a whole generation run and is
the single place memory is charged and released. Every bounded stage (Faker pool construction,
parent-key pool build, per-batch Arrow conversion buffer, resident accumulator, sink buffer) calls
`reserve` with a conservative upper bound and allocates only after it returns. A reservation that
does not fit refuses BEFORE the allocation happens, so the ceiling is enforced by the library
itself and not by an external process watching RSS.

The shape follows the reservation pattern of established memory managers: DuckDB's buffer manager
(reserve against `memory_limit` before pinning a block, refuse rather than over-commit) and the
Spark `MemoryManager` execution-memory pool (acquire before use, release at task end). The
ceiling itself comes from `execution.out_of_core._budget.resolve_budget`, the same figure the
out-of-core route uses.

Messages carry stage names and byte counts only. They never carry data values or expressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from decoy_engine import reasons
from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from types import TracebackType


class GenerationBudgetError(DecoyError):
    """Base for the declared failures of the bounded-generation contract."""

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class GenerationBudgetExceeded(GenerationBudgetError):  # noqa: N818 -- name fixed by the contract
    """A conservative upper bound does not fit the remaining budget.

    Raised for the resident accumulator, a legacy whole-table run, a stage reservation, or a
    produced value wider than its declared bound. It never degrades to different values: the
    run fails and nothing is emitted.
    """


class GenerationUnboundedError(GenerationBudgetError):
    """A column has no defensible upper bound under a bounded ceiling.

    Raised at admission, on every path, before any provider is invoked. An estimate is not a
    bound: a figure that real data can exceed does not protect the ceiling.
    """


@dataclass
class Reservation:
    """A held charge. Release it (or leave the `with` block) when the stage ends."""

    ledger: GenerationBudgetLedger
    stage: str
    nbytes: int
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.ledger._release(self.nbytes)

    def __enter__(self) -> Reservation:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


@dataclass
class GenerationBudgetLedger:
    """Remaining-bytes counter for one run.

    ``ceiling_bytes=None`` is the explicit unbounded mode: charges are still counted (so peak is
    observable) but never refused, and it carries no bounded-memory guarantee.
    """

    ceiling_bytes: int | None
    charged_bytes: int = 0
    peak_bytes: int = 0
    # (stage, nbytes) in reservation order, so a test can prove a stage reserved before it allocated.
    events: list[tuple[str, int]] = field(default_factory=list)

    @property
    def bounded(self) -> bool:
        return self.ceiling_bytes is not None

    @property
    def remaining_bytes(self) -> int | None:
        if self.ceiling_bytes is None:
            return None
        return self.ceiling_bytes - self.charged_bytes

    def reserve(self, stage: str, upper_bound_bytes: int) -> Reservation:
        if upper_bound_bytes < 0:
            raise ValueError(f"reservation for stage {stage!r} must be >= 0")
        if self.ceiling_bytes is not None and self.charged_bytes + upper_bound_bytes > (
            self.ceiling_bytes
        ):
            raise GenerationBudgetExceeded(
                code=reasons.runtime.GENERATION_BUDGET_EXCEEDED,
                message=(
                    f"stage {stage!r} needs {upper_bound_bytes} bytes but only "
                    f"{self.ceiling_bytes - self.charged_bytes} of {self.ceiling_bytes} remain."
                ),
            )
        self.charged_bytes += upper_bound_bytes
        self.peak_bytes = max(self.peak_bytes, self.charged_bytes)
        self.events.append((stage, upper_bound_bytes))
        return Reservation(self, stage, upper_bound_bytes)

    def _release(self, nbytes: int) -> None:
        self.charged_bytes -= nbytes


__all__ = [
    "GenerationBudgetError",
    "GenerationBudgetExceeded",
    "GenerationBudgetLedger",
    "GenerationUnboundedError",
    "Reservation",
]

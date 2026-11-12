"""Outcome carrier shared by every parse/format kernel.

Kernels never raise for data: a failure is a registered ``ReasonCode`` with no value, message or
exception attached, so a hostile cell cannot ride a traceback out of the casting boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decoy_engine.reasons import ReasonCode


@dataclass(frozen=True, slots=True)
class CastOutcome:
    ok: bool
    value: Any = None
    reason: ReasonCode | None = None

    def __repr__(self) -> str:  # never render the value: it may be a cell
        return f"CastOutcome(ok={self.ok}, reason={self.reason!r})"


def succeed(value: Any) -> CastOutcome:
    return CastOutcome(True, value, None)


def fail(reason: ReasonCode) -> CastOutcome:
    return CastOutcome(False, None, reason)

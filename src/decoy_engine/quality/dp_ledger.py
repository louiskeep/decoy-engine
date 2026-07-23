"""Compile-time DP release-ID policy ceiling (NOT a mechanism accountant).

Split out of `quality/dp_budget.py` on a size-cap crossing (CLAUDE.md's
~600 LOC orchestration cap): `ReleaseLedger` sums already-certified,
already-composed release totals across DISTINCT release IDs at
plan-compile time (guide section 3.3: "Wiring OpenDP's certified losses
into dp_accounting's composition and asserting the result against the
request" -- item 3 of what Decoy owns). This is a policy-ceiling
bookkeeping convenience over numbers a fit already produced via
`OpenDpReleaseSession.composed_loss()` (`quality/dp_budget.py`); it is NOT
a mechanism accountant and computes no privacy quantity of its own.
Basic sequential composition (sum of already-composed totals) is the
correct, conservative bound across independent release IDs (guide
section 4.3.5 / `plan/_checks_dp.py`'s release-ID dedup). Its sole
consumer is `plan/_checks_dp.py::verify_dp_snapshots`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class _LedgerCharge:
    label: str
    epsilon: float
    delta: float


@dataclass
class ReleaseLedger:
    """Sums already-certified, already-composed release totals across
    DISTINCT release IDs at plan-compile time. See module docstring."""

    _charges: list[_LedgerCharge] = field(default_factory=list)

    def charge(self, label: str, *, epsilon: float, delta: float = 0.0) -> None:
        if epsilon <= 0:
            raise ValueError(f"epsilon must be > 0, got {epsilon!r}")
        if delta < 0:
            raise ValueError(f"delta must be >= 0, got {delta!r}")
        self._charges.append(_LedgerCharge(label, float(epsilon), float(delta)))

    def total_epsilon(self) -> float:
        # C-H1 (Codex HIGH): plain `sum` accumulates binary floating-point
        # error term by term; ten releases at epsilon 0.1 give
        # `sum == 0.9999999999999999`, a value strictly less than a
        # ceiling of `1.0` even though the conservative (correctly
        # rounded) total is exactly `1.0`. `math.fsum` uses Shewchuk's
        # algorithm to sum with a single final rounding, closing that gap
        # so a ceiling comparison can't be defeated by summation order.
        return math.fsum(c.epsilon for c in self._charges)

    def total_delta(self) -> float:
        return math.fsum(c.delta for c in self._charges)  # C-H1: see total_epsilon

    def breakdown(self) -> list[dict[str, object]]:
        return [{"label": c.label, "epsilon": c.epsilon, "delta": c.delta} for c in self._charges]

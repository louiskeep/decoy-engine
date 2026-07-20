"""Privacy-budget accountant for the DP snapshot pipeline (DPS-2).

Composes every noisy release charged against it into one stated (epsilon,
delta) via BASIC SEQUENTIAL COMPOSITION: epsilon.total = sum(epsilon_i),
delta.total = sum(delta_i) over a sequence of DP mechanisms applied to
the same (or adaptively-chosen) dataset. This is Dwork & Roth, *The
Algorithmic Foundations of Differential Privacy* (2014), Theorem 3.16
(sequential composition) -- the loose but simple composition bound that
holds for ANY DP mechanism, unlike advanced/RDP composition which needs
the mechanism family to be known ahead of time.

`quality/dp.py`'s Laplace releases (per-column histograms, threshold-
released category sets, row_count/distinct_count scalars) all compose
under this basic rule because each is a standalone (epsilon, 0)-DP
release (delta=0 for pure Laplace) on the same underlying dataset.

RDP/zCDP tight composition for a Gaussian mechanism is deferred to DPS-4
(PrivBayes joint-distribution DP), which is why `charge` accepts a
`mechanism` label now even though only "laplace" is charged today: the
accounting shape does not need to change when a Gaussian release is
added, only the (currently unused) composition strategy that reads
`breakdown()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class _Charge:
    label: str
    epsilon: float
    delta: float
    mechanism: str


@dataclass
class PrivacyBudget:
    """Accumulates DP releases and reports the composed (epsilon, delta).

    One `PrivacyBudget` instance tracks every release for ONE snapshot
    (or other single DP artifact). `charge` is called once per release
    (e.g. once per column histogram, once for row_count); `total_epsilon`
    / `total_delta` / `breakdown` report the composed whole-artifact
    guarantee.
    """

    _charges: list[_Charge] = field(default_factory=list)

    def charge(
        self,
        label: str,
        *,
        epsilon: float,
        delta: float = 0.0,
        mechanism: str = "laplace",
    ) -> None:
        """Record one DP release against this budget.

        Args:
            label: Human-readable identifier for the release (e.g.
                "age.histogram", "row_count"). Not required to be
                unique; `breakdown()` preserves charge order.
            epsilon: This release's privacy loss. Must be > 0 -- a
                zero or negative epsilon would be a no-op release
                mislabeled as DP (mirrors `dp.py`'s epsilon gate).
            delta: This release's failure probability. Must be >= 0;
                0.0 (the default) is exact (epsilon, 0)-DP, correct for
                the pure Laplace mechanism.
            mechanism: Label recorded for audit / future RDP composition
                (see module docstring); does not affect the sum today.

        Raises:
            ValueError: `epsilon <= 0` or `delta < 0`.
        """
        if epsilon <= 0:
            raise ValueError(f"epsilon must be > 0, got {epsilon!r}")
        if delta < 0:
            raise ValueError(f"delta must be >= 0, got {delta!r}")
        self._charges.append(_Charge(label, float(epsilon), float(delta), mechanism))

    def total_epsilon(self) -> float:
        """Composed epsilon: sum over all charges (Thm 3.16)."""
        return sum(c.epsilon for c in self._charges)

    def total_delta(self) -> float:
        """Composed delta: sum over all charges (Thm 3.16)."""
        return sum(c.delta for c in self._charges)

    def breakdown(self) -> list[dict[str, object]]:
        """Every charge, in charge order, for the snapshot's `dp.charges` block."""
        return [
            {"label": c.label, "epsilon": c.epsilon, "delta": c.delta, "mechanism": c.mechanism}
            for c in self._charges
        ]

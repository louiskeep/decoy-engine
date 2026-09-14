"""Task 4.4: `ShadowContext` -- the runtime-only carrier for secrets and the
resource budget the shadow coordinator enforces.

Never part of the frozen `PhysicalPlan` / `PhysicalPlanInputs` snapshot (C0):
the resolved `KeyProvider` and mask-key bytes live EXCLUSIVELY here, passed
at execution time, so a serialized or logged plan can never carry them (see
`tests/physical/test_shadow_no_secret_serialization.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan

_DEFAULT_BATCH_SIZE_ROWS = 50_000

__all__ = ["ShadowContext"]


@dataclass(frozen=True)
class ShadowContext:
    """Runtime dependencies for one shadow run: the resolved keyed-mask IKM
    (never the plan or snapshot) plus the batch/thread budget the coordinator
    enforces (TASK-4.4-PLAN.md's Resource policy: hard-gate only a batch-size
    or thread-budget CONTRACT breach, never relative performance).
    """

    mask_key: bytes
    batch_size_rows: int = _DEFAULT_BATCH_SIZE_ROWS
    native_threads: int | None = None

    def __post_init__(self) -> None:
        if self.batch_size_rows < 1:
            raise ValueError(f"batch_size_rows must be >= 1, got {self.batch_size_rows!r}")
        if self.native_threads is not None and self.native_threads < 1:
            raise ValueError(f"native_threads must be >= 1 or None, got {self.native_threads!r}")

    @classmethod
    def from_key_provider(
        cls,
        *,
        plan: Plan,
        key_provider: KeyProvider | None,
        batch_size_rows: int = _DEFAULT_BATCH_SIZE_ROWS,
        native_threads: int | None = None,
    ) -> ShadowContext:
        """Resolve `mask_key` the same way `run_pipeline` does
        (`keyprovider.resolve_mask_key`), so the shadow side and the oracle,
        given the same `key_provider`, draw from byte-identical key material.
        The `KeyProvider` itself is never retained past this call."""
        from decoy_engine.keyprovider import resolve_mask_key

        mask_key = resolve_mask_key(plan=plan, key_provider=key_provider)
        return cls(
            mask_key=mask_key, batch_size_rows=batch_size_rows, native_threads=native_threads
        )

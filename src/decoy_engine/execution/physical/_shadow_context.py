"""Task 4.4: `ShadowContext` -- the runtime-only carrier for secrets and the
resource budget the shadow coordinator enforces.

Never part of the frozen `PhysicalPlan` / `PhysicalPlanInputs` snapshot (C0):
the resolved `KeyProvider` and mask-key bytes live EXCLUSIVELY here, passed
at execution time, so a serialized or logged plan can never carry them (see
`tests/physical/test_shadow_no_secret_serialization.py`).

Task 4.6 slice 3 adds three optional runtime-only carriers (`plan`,
`relationship_graph`, `key_provider`) an OUT_OF_CORE dispatch needs to
delegate through `OutOfCoreAdapter` -- see `_shadow_coordinator.py`'s
`_require_ooc_deps`. All default to `None`, so every pre-existing scalar/
chunked/faker construction (none of which reach the OOC dispatch branch)
stays valid unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships import RelationshipGraph

_DEFAULT_BATCH_SIZE_ROWS = 50_000

__all__ = ["ShadowContext"]


@dataclass(frozen=True)
class ShadowContext:
    """Runtime dependencies for one shadow run: the resolved keyed-mask IKM
    (never the plan or snapshot) plus the batch/thread budget the coordinator
    enforces (TASK-4.4-PLAN.md's Resource policy: hard-gate only a batch-size
    or thread-budget CONTRACT breach, never relative performance).

    `job_seed` (Task 4.6 slice 1) is the plan's `seed_envelope.job_seed`: the
    non-secret pool-BUILD seed a faker node's `PoolBuilder.build` call needs
    (mask_key re-keys the deterministic SELECTION only, per the DE-02 seam --
    see `generation/pool/_builder.py`). Defaults to `b""` so every
    pre-existing direct `ShadowContext(...)` construction across the Task 4.4
    test suite (none of which bind a faker node) stays valid unchanged;
    `from_key_provider` always supplies the real 8-byte value from the plan.

    `plan` / `relationship_graph` / `key_provider` (Task 4.6 slice 3) are the
    OUT_OF_CORE dispatch's own runtime carriers, all optional and defaulting
    to `None`. Every scalar/chunked/faker path leaves them unset and never
    reads them. `key_provider` is the ORIGINAL resolved provider (not just
    the derived `mask_key`): the OOC delegate (`run_fk_out_of_core`) needs it
    to derive byte-identical key material to the oracle for every keyed
    column it masks, not only the one this context's own `mask_key` already
    covers. This is a deliberate exception to the "`KeyProvider` never
    retained past `from_key_provider`" rule the docstring above states for
    the scalar slices -- a relationship-JOB dispatch legitimately keeps the
    provider on the RUNTIME context, never on the frozen plan. `mask_key` and
    `key_provider` are both `repr=False`: the default dataclass repr would
    otherwise render secret bytes (`mask_key`) or, for a custom `KeyProvider`
    implementation that does not redact itself, raw key material
    (`key_provider`).
    """

    mask_key: bytes = field(repr=False)
    job_seed: bytes = b""
    batch_size_rows: int = _DEFAULT_BATCH_SIZE_ROWS
    native_threads: int | None = None
    plan: Plan | None = None
    relationship_graph: RelationshipGraph | None = None
    key_provider: KeyProvider | None = field(default=None, repr=False)

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
        relationship_graph: RelationshipGraph | None = None,
    ) -> ShadowContext:
        """Resolve `mask_key` the same way `run_pipeline` does
        (`keyprovider.resolve_mask_key`), so the shadow side and the oracle,
        given the same `key_provider`, draw from byte-identical key material.

        Unlike the scalar slices, the `KeyProvider` itself IS retained past
        this call (on `key_provider`) -- alongside `plan` and
        `relationship_graph` -- so an OOC dispatch has everything
        `OutOfCoreAdapter.run` needs. A caller that never reaches the OOC
        branch (every scalar/chunked/faker caller today) simply never reads
        these three fields.
        """
        from decoy_engine.keyprovider import resolve_mask_key

        mask_key = resolve_mask_key(plan=plan, key_provider=key_provider)
        return cls(
            mask_key=mask_key,
            job_seed=plan.seed_envelope.job_seed,
            batch_size_rows=batch_size_rows,
            native_threads=native_threads,
            plan=plan,
            relationship_graph=relationship_graph,
            key_provider=key_provider,
        )

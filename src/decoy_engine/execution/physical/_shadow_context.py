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

Task 4.6 slice 5a adds `derive_key` / `instance_default_locale` (the
synthesis dispatch's own runtime deps, mirroring what the harness passes to
the oracle `run_pipeline` call) plus a runtime-admission record: `sink_
requested` / `source_loader_requested` / `vault_writer_requested` /
`fidelity_report`. The generation admission gate (`_shadow_coordinator.
_require_generation_shadowable`) cannot observe any of these from the plan
or snapshot alone -- a non-None `vault_writer`, for instance, changes the
ORACLE's pre-generation key validation (`_pipeline.py`) while the
coordinator itself never sees it -- so the harness mirrors its own oracle
call's settings onto `ctx`, and the gate declines any non-admitted value
before constructing the adapter. The record is PRESENCE-ONLY (booleans, not
the sink/loader/writer objects themselves): `ShadowContext`/`ShadowCoordinator`
must never accept a parameter literally named `sink` (`test_shadow_
disconnection.py`'s "no target descriptor reaches this class" sentry, C1/C7
-- structurally proving publication stays impossible from this seam), so
this record cannot hold, or even be shaped like, an actual publication
channel; `from_key_provider` still takes the real objects positionally and
converts each to `is not None` at construction. All seven default to the
admitted value (`None`/`False`), so every pre-existing construction is
unchanged.

Task 4.6 slice 6 adds the FULL_FRAME dispatch's own runtime carriers:
`namespace_registry` and `unconfigured_column_policy` (the exact resolved
objects the oracle's own `adapter.run(...)` call receives -- neither is
recoverable from `plan` alone, so the harness builds them the same way
`run_pipeline` does and mirrors them onto `ctx`) and `full_frame_adapter`,
the caller-INJECTED, already-selected `ExecutionAdapter` instance (never
constructed by the coordinator itself -- see `_shadow_full_frame.py`).
`validators_requested` / `quarantine_requested` extend the slice 5a
runtime-admission record with the two settings `_pipeline_finalize.
finalize_validators_and_quarantine` reads straight off `config`: unlike
`sink`/`source_loader`/`vault_writer`, `run_pipeline` takes no `validators`/
`quarantine` KEYWORD at all (both come from `config["validators"]`/
`config["quarantine"]`), so `from_key_provider` accepts the same raw values
a caller would read off its own config and reduces them to presence here,
matching the pattern. All five default to the admitted value
(`None`/`False`), so every pre-existing construction is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pyarrow as pa
    from faker import Faker

    from decoy_engine.execution._adapter import ExecutionAdapter
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

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

    `derive_key` / `instance_default_locale` (Task 4.6 slice 5a) are the
    synthesis dispatch's own runtime deps, threaded straight through to
    `SynthesisStageAdapter.run` -> `generate_tables`. `derive_key` is
    `repr=False` for the same reason `key_provider` is: it is a caller
    resolver CALLABLE that could close over key material, so the default
    dataclass repr must never render it. `sink_requested` / `source_loader_
    requested` / `vault_writer_requested` / `fidelity_report` are the
    runtime-admission record (r5 finding-1): PRESENCE-ONLY booleans the
    generation admission gate declines on when set, never the actual sink/
    loader/writer object (which this class must never hold -- see the
    module docstring). The harness sets each to mirror exactly what it
    passes to its own oracle `run_pipeline` call. All seven default to the
    admitted value, so every pre-existing construction is unchanged.

    `provider_snapshot` (5a-faker) is the ONE captured custom-faker-provider
    view (`internal.faker_setup.snapshot_custom_faker_providers`) the
    generation admission gate and the synthesis adapter both read for a
    `faker` column, instead of two independent live-registry reads a
    concurrent register/unregister could straddle. `None` (the default,
    every pre-existing construction) falls back to the live registry at
    both sites, exactly as before this field existed.
    """

    mask_key: bytes = field(repr=False)
    job_seed: bytes = b""
    batch_size_rows: int = _DEFAULT_BATCH_SIZE_ROWS
    native_threads: int | None = None
    plan: Plan | None = None
    relationship_graph: RelationshipGraph | None = None
    key_provider: KeyProvider | None = field(default=None, repr=False)
    derive_key: Any = field(default=None, repr=False)
    instance_default_locale: str | None = None
    sink_requested: bool = False
    source_loader_requested: bool = False
    vault_writer_requested: bool = False
    fidelity_report: bool = False
    namespace_registry: NamespaceRegistry | None = None
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None
    full_frame_adapter: ExecutionAdapter | None = None
    validators_requested: bool = False
    quarantine_requested: bool = False
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None

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
        derive_key: Any = None,
        instance_default_locale: str | None = None,
        sink: TransactionalSink | None = None,
        source_loader: Callable[[str], pa.Table] | None = None,
        vault_writer: Any = None,
        fidelity_report: bool = False,
        namespace_registry: NamespaceRegistry | None = None,
        unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
        full_frame_adapter: ExecutionAdapter | None = None,
        validators: Any = None,
        quarantine_config: Any = None,
        provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
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

        `derive_key` / `instance_default_locale` / `sink` / `source_loader` /
        `vault_writer` / `fidelity_report` (Task 4.6 slice 5a) all default to
        the admitted (`None`/`False`) value; a caller building a generation
        harness passes the SAME values it hands its own oracle `run_pipeline`
        call, so the admission gate's decline stays truthful to what the
        oracle actually saw. `sink` / `source_loader` / `vault_writer` are
        accepted here as the real objects (matching `run_pipeline`'s own
        kwarg names, for a natural call site) but stored on `ShadowContext`
        as PRESENCE-ONLY booleans -- see the class docstring for why the
        objects themselves never land on this frozen carrier.

        `namespace_registry` / `unconfigured_column_policy` /
        `full_frame_adapter` (Task 4.6 slice 6) are the FULL_FRAME dispatch's
        own resolved runtime objects, retained (not reduced to booleans) the
        same way `key_provider` and `relationship_graph` are -- the dispatch
        needs the objects themselves, not just their presence.
        `validators` / `quarantine_config` mirror what a caller read off its
        own `config["validators"]` / `config["quarantine"]` and are reduced
        to presence-only booleans here, the same treatment as `sink` /
        `source_loader` / `vault_writer` above.

        `provider_snapshot` (5a-faker) is retained as-is (not reduced to a
        boolean) -- the generation admission gate and the synthesis adapter
        both need the mapping itself. `None` (the default) matches
        `generate_tables`'s own default: resolve against the live registry.
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
            derive_key=derive_key,
            instance_default_locale=instance_default_locale,
            sink_requested=sink is not None,
            source_loader_requested=source_loader is not None,
            vault_writer_requested=vault_writer is not None,
            fidelity_report=fidelity_report,
            namespace_registry=namespace_registry,
            unconfigured_column_policy=unconfigured_column_policy,
            full_frame_adapter=full_frame_adapter,
            validators_requested=bool(validators),
            quarantine_requested=quarantine_config is not None,
            provider_snapshot=provider_snapshot,
        )

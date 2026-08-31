"""Config-aware `phase3_c1_eligibility` engine predicate (Phase 3 Task 3.3).

Layers ABOVE `native_route_eligibility` (`_plan.py`), which is strategy-only
and therefore rejects EVERY `faker` column with `no_native_kernel:...`
(faker has no compiled Rust kernel; Task 2.6's admitted set never included
it). This module formalizes the JC-5 partition-safe precondition Task 3.1's
`faker_pool_precondition_met` already guards coarsely
(`docs/plans/2026-08-30-part1-phase3-c1-slice.md`), splitting it into
distinct coded reasons, and adds the C1 provider allowlist on top: a
deterministic-reuse faker column over one of the two frozen C1 providers
(`person_first_name`, `person_last_name`) admits through the native POOL
path instead of falling to the oracle.

Pure config/profile predicate: no I/O, no plan compile, no source staging.
Every rejection is reachable from the raw pipeline config alone, checked in
the same field order `plan/_seed_envelope.py` resolves them (deterministic,
cardinality_mode, namespace, pool_size), so this predicate cannot admit a
column `compile_plan` would resolve differently.

One config shape this module rejects that native_route_eligibility's generic
checks cannot see, because it is faker-specific and only visible once a
column's provider/mode combination is otherwise C1-admissible: `vault: true`
(reversible source->masked mapping persistence, a real `ColumnConfig`
field) has no wiring on the native pool route.
`execution.native._dispatch` never imports `decoy_engine.vault`; only the
oracle pipelines (`execution._pipeline`, `execution._chunked`) call
`collect_vault_entries`. Admitting a vaulted faker column natively would
silently drop that persistence, so it is rejected as
`faker_config_shape_unsupported`. `when:` row gating is rejected the same way:
the native pool route never invokes `_when_gate`, so a `when`-gated column would
mask every row instead of only the gate's matches. A `PipelineConfig`-validated
MASK column cannot carry `when` today (`ColumnConfig` has no `when` field and
`extra="forbid"`), but this predicate takes a raw `dict` and shares
`compile_plan`'s `when`-honoring contract, so the guard is real rather than
decorative -- it holds even if a future config surface admits `when`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decoy_engine.execution._runner import provider_is_composite
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.execution.native._provider_class import classify_provider
from decoy_engine.execution.native._requirements import (
    _PARTITION_INDEPENDENT_CARDINALITY_MODES,
)
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._pool_size import resolve_pool_size
from decoy_engine.providers_v2 import get_default_registry

# The frozen C1 recipe's faker providers (`docs/plans/PHASE3-C1-BASELINE.md`):
# FIRST=person_first_name, LAST/MAIDEN=person_last_name. Every other
# pool_native provider (decoy_native identifiers, any other poolable faker
# provider, composites) is out of Decision 5's scope and rejected coded
# rather than silently admitted.
C1_PROVIDER_ALLOWLIST = frozenset({"person_first_name", "person_last_name"})


@dataclass(frozen=True)
class Phase3Eligibility:
    """Result of the Phase 3 C1 admission query for one table.

    ``admitted`` is True only when every column of the table is eligible for
    the deterministic C1 masking route: a faker column through this module's
    JC-5 + allowlist checks, every other column through the unchanged Phase 1
    predicate. ``reasons`` is a tuple of coded reasons naming the column,
    empty on admit.
    """

    admitted: bool
    reasons: tuple[str, ...]


def phase3_c1_eligibility(
    config: dict[str, Any], *, table: str, profile: Any | None = None
) -> Phase3Eligibility:
    """Report whether ``table`` in ``config`` can run the deterministic C1
    masking route: the Phase 1 native-kernel path for non-faker columns,
    plus the Phase 3 bounded-pool path for a deterministic-reuse faker
    column over an allowlisted C1 provider.

    Non-faker columns, composite-provider columns, generation columns, and
    FK-composite-group nodes defer entirely to `native_route_eligibility`
    (unchanged). A scalar `faker` column over a non-composite provider is
    re-classified here: its `no_native_kernel:...:faker` base rejection is
    replaced by this module's own coded verdict (admit, or one of the
    distinct JC-5 / allowlist / config-shape rejections), since the base
    predicate's blanket "no kernel" reason is exactly what Phase 3 exists to
    narrow.
    """
    table_cfg = _find_table(config, table)
    base = native_route_eligibility(config, table=table, profile=profile)
    registry = get_default_registry()

    # An ORDERED LIST, not a name-keyed dict: a table can carry two declarations
    # for the same column name (TableConfig preserves both), and a dict would let
    # a later valid declaration silently overwrite an earlier unsafe one, hiding
    # its rejection. Every faker declaration is evaluated; `faker_names` is the
    # membership set used only to strip the base predicate's kernel rejections.
    faker_columns: list[tuple[str, dict[str, Any]]] = []
    faker_names: set[str] = set()
    if table_cfg is not None:
        for col in table_cfg.get("columns", ()) or ():
            if not isinstance(col, dict):
                continue
            name = col.get("name")
            if not name or col.get("strategy") != "faker":
                continue
            if provider_is_composite(col.get("provider"), registry):
                # Composite fan-out: a different pool_native family, out of
                # Decision 5's scope. native_route_eligibility already
                # rejects it via composite_provider_multi_column; leave it.
                continue
            faker_columns.append((name, col))
            faker_names.add(name)

    reasons = [
        r for r in base.rejections if not _is_reclassified_faker_kernel_rejection(r, faker_names)
    ]
    for name, col in faker_columns:
        reason = _faker_column_rejection(name, col, table=table, registry=registry)
        if reason is not None:
            reasons.append(reason)

    # Stable-dedupe: two identical declarations of the same unsafe column yield
    # the same reason string; report it once (each reason already carries its
    # column name, so distinct columns never collapse). Order preserved.
    reasons = list(dict.fromkeys(reasons))
    return Phase3Eligibility(admitted=not reasons, reasons=tuple(reasons))


def _is_reclassified_faker_kernel_rejection(reason: str, faker_names: set[str]) -> bool:
    """True for the base predicate's `no_native_kernel:<name>:faker` entry
    naming one of `faker_names` -- the ONE base rejection this module replaces
    with its own finer verdict for those columns."""
    if not reason.startswith("no_native_kernel:"):
        return False
    _, _, rest = reason.partition(":")
    # rpartition, not partition: `strategy` is one of a bounded set of
    # identifier tokens that never contains a colon (`native_kernel_rejection`
    # only ever appends "faker" or another strategy name here), but `name` is
    # an unvalidated column name from a raw dict (`ColumnConfig.name: str` has
    # no charset restriction) and MAY itself contain a colon. Splitting from
    # the right always isolates the real strategy token; splitting from the
    # left (the prior bug) misparsed a colon-bearing name as part of the
    # strategy, so `strategy == "faker"` fell through to False and the base
    # rejection for an otherwise-admissible column was never stripped --
    # rejecting a table Phase 3 should have admitted.
    name, _, strategy = rest.rpartition(":")
    return strategy == "faker" and name in faker_names


def _faker_column_rejection(
    name: str, col: dict[str, Any], *, table: str, registry: Any
) -> str | None:
    """The coded JC-5 / allowlist / config-shape reason `name` cannot run on
    the Phase 3 native pool route, or None when it can. Checked in the exact
    order the plan's coded-rejection list gives (Task 3.3 Step 1)."""
    # `allow_collisions: true` compiles to deterministic + reuse (a stable
    # many-to-one map) in `plan/_seed_envelope.py`; mirror that resolution so a
    # column the oracle resolves as deterministic-reuse is not mis-rejected here
    # as non-deterministic. But `_seed_envelope` also REJECTS `allow_collisions`
    # alongside an explicit non-reuse `cardinality_mode` (allow_collisions_mode_
    # conflict); mirror that too rather than silently coercing to reuse and
    # admitting a config `compile_plan` itself would refuse.
    allow_collisions = bool(col.get("allow_collisions", False))
    explicit_mode = col.get("cardinality_mode")
    if allow_collisions and explicit_mode is not None and explicit_mode != "reuse":
        return f"allow_collisions_mode_conflict:{name}:{explicit_mode}"

    deterministic = allow_collisions or bool(col.get("deterministic", False))
    if not deterministic:
        return f"faker_not_deterministic:{name}"

    cardinality_mode = "reuse" if allow_collisions else (explicit_mode or "reuse")
    if cardinality_mode not in _PARTITION_INDEPENDENT_CARDINALITY_MODES:
        return f"faker_cardinality_not_partition_independent:{name}:{cardinality_mode}"

    namespace = col.get("namespace")
    if not namespace:
        return f"faker_namespace_required:{name}"

    try:
        pool_size = resolve_pool_size(col, table_name=table, col_name=name)
    except PlanCompileError as exc:
        # A genuine top-level/provider_config pool_size conflict is a config
        # error `compile_plan` itself would raise; surface its own code
        # rather than mis-attributing it to one of the JC-5 codes above.
        return f"{exc.code}:{name}"
    if pool_size is None:
        return f"faker_pool_size_required:{name}"

    provider = col.get("provider")
    if not isinstance(provider, str) or not provider:
        # A missing/non-string provider is fail-closed the same way
        # classify_provider fails closed on an unregistered provider_id;
        # avoided calling classify_provider directly here so a None/non-str
        # value never reaches its str-typed parameter.
        return f"provider_reject_large:{name}:{provider!r}"

    provider_class = classify_provider(provider, None, registry=registry)
    if provider_class == "python_only":
        return f"provider_not_pool_native:{name}:{provider}"
    if provider_class == "reject_large":
        return f"provider_reject_large:{name}:{provider}"
    # provider_class == "pool_native" from here.
    if provider not in C1_PROVIDER_ALLOWLIST:
        return f"provider_not_in_c1_allowlist:{name}:{provider}"

    if bool(col.get("vault", False)):
        # See the module docstring: the native route has no vault-persistence
        # wiring, so a vaulted faker column would silently lose its
        # reversible mapping if admitted.
        return f"faker_config_shape_unsupported:{name}:vault"

    if col.get("when"):
        # The native pool route never consults `.when` or invokes `_when_gate`
        # (only the oracle adapters do), so a `when`-gated faker column admitted
        # natively would mask EVERY row instead of only the rows the gate
        # matches -- a fidelity divergence from the oracle. Reject it for
        # symmetry with vault. A PipelineConfig-validated MASK column cannot
        # carry `when` today (ColumnConfig's `extra="forbid"`), but this
        # predicate takes a raw dict and shares compile_plan's `when`-honoring
        # contract, so the guard is real rather than decorative.
        return f"faker_config_shape_unsupported:{name}:when"

    return None


def _find_table(config: dict[str, Any], table: str) -> dict[str, Any] | None:
    for tbl in config.get("tables", ()) or ():
        if tbl.get("name") == table:
            return tbl
    return None


__all__ = ["C1_PROVIDER_ALLOWLIST", "Phase3Eligibility", "phase3_c1_eligibility"]

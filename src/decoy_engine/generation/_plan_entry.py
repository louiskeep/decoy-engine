"""Plan-only ``generate_tables`` and ``generate_tables_streamed`` public entry points (DPS
Scope B, guide section 4.8).

Own module for the same size-ceiling reason as the sibling orchestration
splits (`plan/_generation.py`, `plan/_checks_dp.py`): the guide is
explicit that this boundary wrapper must not be added to
`generation/synthesize.py`, which stays at its pre-existing size. This module owns the
Plan-only contract at the public boundary: the runtime type guard, unpacking the compiled
`Plan`'s pinned `GenerationPlan` payload, and handing the recovered config plus the pinned
statistical specs/snapshot artifacts to the generation core (`_streamed.run_generation`).

The core runs ONE algorithm for both routes. Semantically eligible plans (sequence,
categorical, built-in faker, reference without cardinality repair) generate through persistent
per-column streams, into a bounded sink or an in-memory table; any other plan takes the
unchanged whole-table generator (`_generate_tables_from_config` in `synthesize.py`).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.generation._streamed import GenerationResult, run_generation
from decoy_engine.generation.statistical import spec_from_dict
from decoy_engine.plan._types import unfreeze_json

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker


def _config_declares_statistical_column(config: dict[str, Any]) -> bool:
    """D-M7 (round-3 remediation): does ANY table declare a `type:
    statistical` generate column? A `dp`-declared pipeline with ZERO
    statistical columns compiles clean (`verify_dp_snapshots` returns
    `dp_verification=None` since there is nothing to verify) but used to
    hit the unconditional receipt check below UNCONDITIONALLY, so it
    could compile but then never generate -- a config-compiles/generate-
    always-raises trap with no way out. The receipt is only meaningful
    (and only required) when the config actually references a DP-fit
    column; `global_settings.dp` alongside non-statistical generate
    columns (faker, sequence, ...) is not a contradiction."""
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    return any(
        isinstance(col, dict) and col.get("type") == "statistical"
        for table in tables
        if isinstance(table, dict)
        for col in table.get("generate_columns", []) or []
    )


@dataclass(frozen=True)
class _PreparedPlan:
    """A validated Plan unpacked into the pinned inputs the generation core consumes."""

    config: dict[str, Any]
    legacy_kwargs: dict[str, Any]


def _prepare(plan: Any, caller: str) -> _PreparedPlan:
    """Plan-only boundary shared by every public entry: the type guard, the `when` re-check,
    the DP receipt check, and the decode of the pinned statistical specs and snapshots."""
    from decoy_engine.plan._types import Plan

    if not isinstance(plan, Plan):
        raise TypeError(
            f"{caller}(plan) requires a compiled decoy_engine.plan.Plan; got "
            f"{type(plan).__name__!r}. Call compile_plan(config, profile, ...) first."
        )
    from decoy_engine.expressions._when_parser import validate_plan_when

    # Before any provider runs: a deserialized or hand-built Plan can carry a `when` the
    # compile check never saw.
    validate_plan_when(plan)
    if plan.generation is None:
        raise TypeError(
            f"{caller}: this Plan has no generation payload (compiled from a "
            "config with no generate_columns, or an older serialized Plan version "
            "loaded for reporting/diagnostics only)."
        )
    config = json.loads(plan.generation.config_json)
    from decoy_engine.plan._checks_dp import _dp_declared

    if (
        _dp_declared(config)
        and plan.generation.dp_verification is None
        and _config_declares_statistical_column(config)
    ):
        raise TypeError(
            f"{caller}: the embedded configuration declares global_settings.dp "
            "but this Plan carries no reproduced DpVerification receipt. Recompile "
            "through compile_plan (or a trusted plan_from_yaml round trip), which "
            "re-derives the receipt from the embedded artifacts."
        )
    statistical_specs = {
        (spec.table_name, spec.column_name): spec_from_dict(unfreeze_json(spec.spec))
        for spec in plan.generation.statistical_specs
    }
    snapshot_index_for_column = {
        (spec.table_name, spec.column_name): spec.snapshot_index
        for spec in plan.generation.statistical_specs
    }
    # Decoded once here, not reopened from `source_path` (guide section
    # 4.8/F4): every consumer (the statistical sampler via
    # `statistical_specs` above, and the fidelity gate via this list)
    # reads only these already-pinned bytes.
    snapshot_artifacts = [
        json.loads(base64.b64decode(s.payload_b64)) for s in plan.generation.snapshots
    ]
    return _PreparedPlan(
        config,
        {
            "statistical_specs": statistical_specs,
            "snapshot_index_for_column": snapshot_index_for_column,
            "snapshot_artifacts": snapshot_artifacts,
        },
    )


def generate_tables(
    plan: Any,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    *,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
    budget_bytes: int | None = None,
) -> dict[str, pa.Table]:
    """Build one Arrow table per generate table in the compiled ``Plan``.

    DPS Scope B (guide section 4.8/F3): the public entry point is
    Plan-only. A raw configuration mapping is rejected with ``TypeError``
    before any output is produced -- the pre-GA break is accepted rather
    than keeping a compatibility overload (guide section 4.2/9.9). Callers
    must run ``compile_plan`` (or deserialize a trusted Plan via
    ``plan_from_yaml``) first; the engine no longer accepts an unvalidated
    dict at this boundary.

    The resident route runs the SAME core as ``generate_tables_streamed`` into an in-memory
    accumulating consumer (``_streamed.run_generation``), so its bytes equal the streamed
    route's for every semantically eligible plan; the legacy whole-table generator runs only
    for plans with an ineligible column. The pre-re-baseline resident bytes are not preserved
    where the declared schema differs (an all-null column or a zero-row table now has its
    declared type, not Arrow's inferred ``null``).

    ``derive_key`` is the pipeline-bound key resolver V1 ``ColumnGenerator``
    threads through; ``instance_default_locale`` flows the platform's
    default Faker locale into the shared-Faker path. Both stay public
    (``execution/_pipeline.py`` passes them positionally-by-keyword); no
    public ``seed`` parameter is added -- the job seed is read from the
    Plan's embedded configuration exactly as before, never from a second
    public input (guide section 4.8).

    ``provider_snapshot`` (5a-faker, additive): an optional, keyword-only,
    already-captured immutable view of the custom-faker-provider registry
    (see ``internal.faker_setup.snapshot_custom_faker_providers``). Every
    ``faker`` column resolves its custom-override question against this
    mapping instead of the live registry when given. ``None`` (the
    default) reads the live registry exactly as before this parameter
    existed. This exists for a caller (the shadow-parity harness) that runs
    generation twice for the same job and needs both runs to see the
    identical registry state, not two live reads a concurrent
    register/unregister could straddle.

    ``budget_bytes`` (default ``None``) is the opt-in bounded mode: a byte ceiling the core
    enforces itself (see ``generate_tables_streamed``). ``None`` is the explicit unbounded mode
    every pre-existing caller uses, with no bounded-memory guarantee.

    Raises:
        TypeError: ``plan`` is not a compiled ``Plan``, or the Plan has no
            ``generation`` payload (a Plan compiled without any
            ``generate_columns`` at all -- there is nothing to generate).
        GenerationUnboundedError: ``budget_bytes`` is set and a column has no defensible bound.
        GenerationBudgetExceeded: ``budget_bytes`` is set and the output cannot fit it.
    """
    prepared = _prepare(plan, "generate_tables")
    result = run_generation(
        prepared.config,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        budget_bytes=budget_bytes,
        provider_snapshot=provider_snapshot,
        legacy_kwargs=prepared.legacy_kwargs,
    )
    assert result.tables is not None  # noqa: S101 -- sink=None can only run resident
    return result.tables


def generate_tables_streamed(
    plan: Any,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    *,
    sink: Any,
    budget_bytes: int | None = None,
    batch_rows: int | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> GenerationResult:
    """Generate a Plan's tables into a bounded transactional ``sink`` when the plan and budget
    allow, else in memory.

    One decision per plan, made before any sink write. If every column is semantically
    eligible, the sink carries the ``bounded_write_batches`` marker, and the streaming peak fits
    the budget, the tables are written batch by batch under an ``OutputPublish`` commit/abort
    session and the result is metadata only (``mode="streamed"``). Otherwise the sink is left
    untouched and the same batches are accumulated into ``result.tables`` (``mode="resident"``,
    with the reason). The values are identical either way; only the execution mode changes.
    ``GenerationBudgetExceeded`` is raised, never different values, when even the resident
    accumulator cannot fit the ceiling. ``GenerationUnboundedError`` is raised, before any
    provider runs, when a column has no defensible bound under a bounded ``budget_bytes``.
    """
    prepared = _prepare(plan, "generate_tables_streamed")
    return run_generation(
        prepared.config,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        sink=sink,
        budget_bytes=budget_bytes,
        batch_rows=batch_rows,
        provider_snapshot=provider_snapshot,
        legacy_kwargs=prepared.legacy_kwargs,
    )

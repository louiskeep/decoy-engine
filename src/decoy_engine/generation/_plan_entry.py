"""Plan-only ``generate_tables`` public entry point (DPS Scope B, guide
section 4.8).

Own module for the same size-ceiling reason as the sibling orchestration
splits (`plan/_generation.py`, `plan/_checks_dp.py`): the guide is
explicit that this boundary wrapper must not be added to
`generation/synthesize.py`, which stays at its pre-existing size. The
actual per-generator dispatch logic (`_generate_tables_from_config` and
everything it calls) is unchanged and stays in `synthesize.py`; this
module owns only the Plan-only contract at the public boundary: the
runtime type guard, unpacking the compiled `Plan`'s pinned
`GenerationPlan` payload, and handing the recovered config plus the
pinned statistical specs/snapshot artifacts down to the unchanged
internal entry point.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pyarrow as pa

from decoy_engine.generation.statistical import spec_from_dict
from decoy_engine.generation.synthesize import _generate_tables_from_config
from decoy_engine.plan._types import unfreeze_json


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


def generate_tables(
    plan: Any,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
) -> dict[str, pa.Table]:
    """Build one Arrow table per generate table in the compiled ``Plan``.

    DPS Scope B (guide section 4.8/F3): the public entry point is
    Plan-only. A raw configuration mapping is rejected with ``TypeError``
    before any output is produced -- the pre-GA break is accepted rather
    than keeping a compatibility overload (guide section 4.2/9.9). Callers
    must run ``compile_plan`` (or deserialize a trusted Plan via
    ``plan_from_yaml``) first; the engine no longer accepts an unvalidated
    dict at this boundary.

    ``derive_key`` is the pipeline-bound key resolver V1 ``ColumnGenerator``
    threads through; ``instance_default_locale`` flows the platform's
    default Faker locale into the shared-Faker path. Both stay public
    (``execution/_pipeline.py`` passes them positionally-by-keyword); no
    public ``seed`` parameter is added -- the job seed is read from the
    Plan's embedded configuration exactly as before, never from a second
    public input (guide section 4.8).

    Raises:
        TypeError: ``plan`` is not a compiled ``Plan``, or the Plan has no
            ``generation`` payload (a Plan compiled without any
            ``generate_columns`` at all -- there is nothing to generate).
    """
    from decoy_engine.plan._types import Plan

    if not isinstance(plan, Plan):
        raise TypeError(
            "generate_tables(plan) requires a compiled decoy_engine.plan.Plan; got "
            f"{type(plan).__name__!r}. Call compile_plan(config, profile, ...) first."
        )
    if plan.generation is None:
        raise TypeError(
            "generate_tables: this Plan has no generation payload (compiled from a "
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
            "generate_tables: the embedded configuration declares global_settings.dp "
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
    return _generate_tables_from_config(
        config,
        derive_key,
        instance_default_locale,
        statistical_specs=statistical_specs,
        snapshot_index_for_column=snapshot_index_for_column,
        snapshot_artifacts=snapshot_artifacts,
    )

"""Plan-compile check for the `dp` generate contract (DPS-3).

Own module for the same `_checks.py` size-ceiling reason as the sibling
per-strategy check modules (`_checks_top_code.py` et al.).

`global_settings.dp` (`config.DpGenerateSettings`) is the operator's
declaration that this pipeline's `generate` output must be an honest
(epsilon, delta)-DP marginal release. Two existing generate-column knobs
are deliberately ANTI-DP: `allow_real_categories: true` and
`high_cardinality: true` (HC-5) both opt a categorical statistical
column into releasing its REAL observed vocabulary rather than the
threshold-released label set DPS-1 produces (`quality/dp.py`). Setting
either alongside `dp` would silently void the guarantee the operator
just declared, so both are hard-rejected at compile time rather than
left to surface as a data leak in the shipped output.

Config-only (no profile, no snapshot I/O): safe for `decoy validate` /
`run_config_only_checks`. Runs BEFORE `check_statistical_columns` in
`compile_plan` so a DP-contract violation surfaces on its own typed
code even when the referenced snapshot_file / artifact would otherwise
also be invalid -- the two checks are independent verdicts.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_dp_generate_contract(config: dict[str, Any]) -> None:
    """Reject anti-DP generate-column knobs when `global_settings.dp` is set.

    Raises:
        PlanCompileError: a `type: statistical` generate column under a
            `dp`-declared pipeline sets `allow_real_categories: true` or
            `high_cardinality: true`.
    """
    global_settings = config.get("global_settings")
    dp_settings = global_settings.get("dp") if isinstance(global_settings, dict) else None
    if not dp_settings:
        return

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("generate_columns", []) or []:
            if not isinstance(col_entry, dict) or col_entry.get("type") != "statistical":
                continue
            col_name = col_entry.get("name", "?")
            where = f"{col_name!r} in table {table_name!r}"
            # high_cardinality checked first (matches _spec.py's own gate
            # order): it is the LARGER disclosure, so when both knobs are
            # set the operator sees the more serious violation's code.
            if col_entry.get("high_cardinality") is True:
                raise PlanCompileError(
                    code="dp_generate_high_cardinality_unsupported",
                    path=f"tables.{table_name}.generate_columns.{col_name}.high_cardinality",
                    message=(
                        f"statistical column {where} sets high_cardinality: true under "
                        "global_settings.dp: this retains the FULL real vocabulary (no "
                        "top-K collapse, no threshold release), a larger disclosure than "
                        "allow_real_categories alone and incompatible with a DP release. "
                        "Remove high_cardinality, or drop global_settings.dp."
                    ),
                )
            if col_entry.get("allow_real_categories") is True:
                raise PlanCompileError(
                    code="dp_generate_allow_real_categories_unsupported",
                    path=(f"tables.{table_name}.generate_columns.{col_name}.allow_real_categories"),
                    message=(
                        f"statistical column {where} sets allow_real_categories: true "
                        "under global_settings.dp: this releases the REAL observed "
                        "vocabulary instead of the DPS-1 threshold-released label set, "
                        "silently voiding the DP guarantee the pipeline declares. Remove "
                        "allow_real_categories, or drop global_settings.dp if a real-"
                        "category release is intended."
                    ),
                )

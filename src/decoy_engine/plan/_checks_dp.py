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

import json
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


def check_dp_snapshot_provenance(config: dict[str, Any]) -> None:
    """Gate remediation Fix 3 (P1 #2): reject a dp-declared pipeline that
    consumes a snapshot which was never actually put through a DP fit.

    Precondition (a) of `docs/what-we-cannot-prove.md`'s DP claim (a
    dp_mode/numeric_domains fit, then `apply_dp_noise`) was previously
    only DOCUMENTED, not enforced: `check_dp_generate_contract` above only
    rejects two anti-DP generate-column KNOBS and never inspects the
    snapshot artifact itself. An operator could set `global_settings.dp`,
    point every statistical column at a completely ordinary (exact,
    non-noised) snapshot, pass every other compile check, and ship
    non-DP output while the config declares DP. A wrong DP guarantee is
    worse than none, so this closes it fail-closed.

    For every `type: statistical` generate column, when `global_settings.dp`
    is declared, the REFERENCED snapshot column must show:

    - The snapshot carries a `dp` block with a numeric `epsilon_total`
      (proof `quality.dp.apply_dp_noise` actually ran over it -- an exact
      snapshot has no `dp` key at all).
    - If the referenced column's `kind` is `numeric`, its
      `support_origin` is `"caller"` (proof it was fit with
      `dp_mode=True` + a `numeric_domains` entry -- DPS-1 -- rather than
      the real, data-dependent min/max range `apply_dp_noise` alone
      cannot retroactively fix).
    - If the referenced column's `kind` is `categorical`, its
      `support_origin` is `"full_vocabulary"` (Fix 7 marker, stamped by
      `quality/snapshot.py` only under `dp_mode`): proof its candidate
      SET was the full observed vocabulary (Fix 1) rather than a
      top-K-by-true-count truncation, which is itself data-dependent and
      therefore not DP even after `apply_dp_noise` thresholds it. An
      ordinary top-K-fit categorical column has `support_origin` absent
      or `"data"` and is rejected.

    This mirrors `check_statistical_columns` (`plan/_checks.py`, row 12),
    which already loads the same snapshot file at compile time via
    `generation.statistical.load_spec` -- the artifact IS loadable here;
    this check re-reads it directly (rather than importing that private
    loader) because it needs fields (`dp`, `support_origin`) that
    `StatisticalSpec` does not carry through. Runs config + snapshot-
    artifact only (no profile, no source data), same as
    `check_statistical_columns`, so it is safe for `decoy validate` /
    `run_config_only_checks`. Deliberately does NOT re-validate snapshot
    readability/schema (`check_statistical_columns` already owns that
    verdict on its own error codes); an unreadable or malformed snapshot
    here is silently skipped so this check never masks that one's error.

    The per-kind verdict is a fail-closed ALLOW-LIST (default-reject): a
    column is accepted ONLY as numeric-with-`caller` or categorical-with-
    `full_vocabulary`; EVERY other kind (datetime, freetext, empty, any
    unknown/future kind) is rejected. `apply_dp_noise` runs on any snapshot
    -- it noises datetime year_bins and freetext length bins too -- so a
    non-`dp_mode` fit followed by `apply_dp_noise` yields a `dp` block over
    data-dependent support; a block-list of just the two eligible kinds
    would let datetime/freetext fall through and PASS (a PoC-proven
    bypass). The fit-time rejection (Fix 2) only covers snapshots that WERE
    dp_mode-fit; this consume-side allow-list covers the ones that were
    not. This is a self-contained verdict: it does not rely on the ordering
    of the sibling anti-DP-knob check.

    Trust boundary: the check reads the snapshot JSON at face value. It
    defends against honest misconfiguration (consuming a non-DP-fit
    artifact under a DP declaration), NOT against a forged or hand-edited
    snapshot -- the (epsilon, delta) guarantee assumes the consumed
    artifact is the genuine output of `decoy fit`. There is no signature on
    the snapshot today.

    Raises:
        PlanCompileError: ``code='dp_snapshot_not_dp_fit'`` when the
            referenced snapshot carries no `dp` block / `epsilon_total`;
            ``code='dp_snapshot_numeric_support_data_dependent'`` when a
            referenced numeric column's `support_origin` is not
            `"caller"`; ``code='dp_snapshot_categorical_candidacy_data_dependent'``
            when a referenced categorical column's `support_origin` is not
            `"full_vocabulary"`; ``code='dp_snapshot_kind_not_dp_eligible'``
            for any other referenced kind (datetime/freetext/empty/unknown).
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
            snapshot_file = col_entry.get("snapshot_file")
            if not snapshot_file:
                continue  # check_statistical_columns (row 12) already rejects this
            where = f"{col_name!r} in table {table_name!r}"
            try:
                with open(str(snapshot_file), encoding="utf-8") as fh:
                    snap = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue  # check_statistical_columns (row 12) already rejects this
            if not isinstance(snap, dict):
                continue

            dp_block = snap.get("dp")
            epsilon_total = dp_block.get("epsilon_total") if isinstance(dp_block, dict) else None
            if not isinstance(dp_block, dict) or not isinstance(epsilon_total, (int, float)):
                raise PlanCompileError(
                    code="dp_snapshot_not_dp_fit",
                    path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
                    message=(
                        f"statistical column {where} references a snapshot with no `dp` "
                        "block / recorded epsilon_total, but global_settings.dp declares "
                        "this pipeline's output must be DP. The referenced snapshot was "
                        "never run through quality.dp.apply_dp_noise -- fit it with "
                        "`decoy fit --epsilon`, or drop global_settings.dp if a non-DP "
                        "release is intended."
                    ),
                )

            source_column = str(col_entry.get("source_column") or col_name)
            col_snap = (snap.get("columns") or {}).get(source_column)
            if not isinstance(col_snap, dict):
                continue  # check_statistical_columns (row 12) already rejects this
            kind = col_snap.get("kind")
            support_origin = col_snap.get("support_origin")
            # Fail-closed ALLOW-LIST (default-reject). A dp-declared pipeline
            # may consume a referenced column ONLY when its provenance marker
            # proves data-independent support. Every other kind/marker
            # combination -- including datetime, freetext, empty, and any
            # unknown/future kind -- is rejected by default: apply_dp_noise
            # runs on ANY snapshot (it noises datetime year_bins and freetext
            # length bins too), so a non-dp_mode fit + apply_dp_noise yields a
            # `dp` block over data-dependent support. A legitimate dp_mode fit
            # can NEVER produce a datetime/freetext column (rejected at fit
            # time, Fix 2), so their presence in a dp-consumed snapshot is by
            # definition non-DP. A block-list of the two eligible kinds let
            # every other kind fall through and PASS (PoC-proven bypass);
            # this allow-list closes it.
            if kind == "numeric" and support_origin == "caller":
                continue
            if kind == "categorical" and support_origin == "full_vocabulary":
                continue
            # Not eligible -> reject. Pick the most actionable code by kind.
            if kind == "numeric":
                raise PlanCompileError(
                    code="dp_snapshot_numeric_support_data_dependent",
                    path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
                    message=(
                        f"statistical column {where}: numeric source column "
                        f"{source_column!r} in the referenced snapshot has "
                        f"support_origin {support_origin!r}, not "
                        "'caller'. It was fit without dp_mode + a numeric_domains "
                        "entry, so its bin edges come from the real (data-dependent) "
                        "min/max -- apply_dp_noise cannot retroactively make that "
                        "independent, so this column is not covered by the DP "
                        "guarantee global_settings.dp declares. Re-fit with "
                        "dp_mode=True and a numeric_domains entry for this column."
                    ),
                )
            if kind == "categorical":
                raise PlanCompileError(
                    code="dp_snapshot_categorical_candidacy_data_dependent",
                    path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
                    message=(
                        f"statistical column {where}: categorical source column "
                        f"{source_column!r} in the referenced snapshot has "
                        f"support_origin {support_origin!r}, not 'full_vocabulary'. "
                        "It was fit without dp_mode, so its released label SET is a "
                        "top-K-by-true-count truncation -- a data-dependent SELECTION "
                        "that apply_dp_noise's threshold cannot make DP, so this "
                        "column is not covered by the DP guarantee global_settings.dp "
                        "declares. Re-fit with dp_mode=True for this column."
                    ),
                )
            raise PlanCompileError(
                code="dp_snapshot_kind_not_dp_eligible",
                path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
                message=(
                    f"statistical column {where}: source column {source_column!r} in the "
                    f"referenced snapshot has kind {kind!r}, which is not DP-eligible under "
                    "global_settings.dp. Only numeric columns (fit with dp_mode + a "
                    "numeric_domains entry) and categorical columns (fit with dp_mode) carry "
                    "the (epsilon, delta) marginal guarantee; datetime and freetext support "
                    "is data-dependent and a legitimate dp_mode fit rejects them outright "
                    "(so their presence here means the snapshot was not a DP fit). Mask or "
                    "exclude this column, or drop global_settings.dp if a non-DP release is "
                    "intended."
                ),
            )

"""Plan-compile checks for the `dp` generate contract (DPS-3) + Option A
categorical/budget remediation (2026-07-21).

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

Option A (Codex privacy-gate block on `feat/dps-marginal-dp`, 2026-07-21):
the categorical release mechanism does not satisfy its stated (epsilon,
delta) bound (rank leakage, suppressed-mass leak, rounding-before-
threshold, a data-dependent fit-success cliff -- see CHANGELOG.md).
`check_dp_categorical_unsupported` rejects every categorical `type:
statistical` column under `global_settings.dp` up front with one clear
code, closing what was previously a two-error deadlock between this
module's `allow_real_categories` rejection and `generation.statistical.
load_spec`'s consent-gate rejection (neither named the real reason, and no
config compiled either way). `check_dp_snapshot_provenance` also enforces
the DECLARED (epsilon, delta) ceiling against what the consumed artifacts
actually spent (Finding 4): presence of `global_settings.dp` was
previously decorative -- any `epsilon_total` numeric value passed,
`delta_total` was never checked, and multiple artifacts were never
composed.

Config + snapshot artifact only (no profile, no source data): safe for
`decoy validate` / `run_config_only_checks`. `check_dp_categorical_
unsupported` and `check_dp_generate_contract` run BEFORE
`check_statistical_columns` in `compile_plan` so a DP-contract violation
surfaces on its own typed code even when the referenced snapshot_file /
artifact would otherwise also be invalid -- these are independent
verdicts, each correct standing alone regardless of call order (the
categorical rejection is deliberately duplicated in `check_dp_snapshot_
provenance` for exactly this reason, see that function's docstring).
"""

from __future__ import annotations

import math
from typing import Any, NoReturn, cast

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.quality.dp_budget import PrivacyBudget

# `_load_snapshot`/`StatisticalSpecError` (generation.statistical._spec) are
# imported lazily INSIDE the functions that need them, matching `plan._checks.
# check_statistical_columns`'s existing lazy import of the same module: a
# module-level import here reintroduces the plan<->generation load-order
# cycle that pattern was already written to avoid (`decoy_engine/__init__.py`
# eagerly loads `decoy_engine.relationships`, which transitively reaches this
# package before `generation.statistical` has finished initializing).


def _dp_settings(config: dict[str, Any]) -> dict[str, Any] | None:
    """The `global_settings.dp` block if declared (truthy dict), else None.

    Shared by every check in this module so "is DP declared" cannot
    diverge between them.
    """
    global_settings = config.get("global_settings")
    dp_settings = global_settings.get("dp") if isinstance(global_settings, dict) else None
    return dp_settings if dp_settings else None


def _raise_categorical_unsupported(
    *, table_name: Any, col_name: Any, source_column: str
) -> NoReturn:
    """Shared by `check_dp_categorical_unsupported` and `check_dp_snapshot_
    provenance` so both raise the IDENTICAL code + message: one clear
    reason, regardless of which check a caller happens to invoke."""
    raise PlanCompileError(
        code="dp_categorical_not_yet_supported",
        path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
        message=(
            f"statistical column {col_name!r} in table {table_name!r}: source column "
            f"{source_column!r} in the referenced snapshot has kind 'categorical'. "
            "Categorical differential privacy is not yet supported -- the release "
            "mechanism does not satisfy its stated (epsilon, delta) guarantee (see "
            "CHANGELOG.md); global_settings.dp currently covers numeric marginals "
            "only. Remove this column from the dp-declared table (mask/exclude it, "
            "or generate it in a table without global_settings.dp), or drop "
            "global_settings.dp."
        ),
    )


def check_dp_generate_contract(config: dict[str, Any]) -> None:
    """Reject anti-DP generate-column knobs when `global_settings.dp` is set.

    Raises:
        PlanCompileError: a `type: statistical` generate column under a
            `dp`-declared pipeline sets `allow_real_categories: true` or
            `high_cardinality: true`.
    """
    dp_settings = _dp_settings(config)
    if dp_settings is None:
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


def check_dp_categorical_unsupported(config: dict[str, Any]) -> None:
    """Option A (2026-07-21 DPS remediation): categorical DP not yet supported.

    Finding 1 (Codex privacy-gate block on `feat/dps-marginal-dp`): the
    categorical release mechanism (`quality/dp.py::apply_dp_noise`'s
    stable-histogram branch) does not satisfy the (epsilon, delta) bound it
    claims -- unnoised rank leakage, suppressed mass folded into an
    observable `other_count`, rounding before the threshold test, and a
    data-dependent fit-success cliff at 30 distinct values (see
    CHANGELOG.md). Rather than let a categorical `type: statistical` column
    under `global_settings.dp` fall into the prior two-error deadlock --
    `statistical_real_categories_not_allowed` without `allow_real_
    categories` (`generation.statistical.load_spec`),
    `dp_generate_allow_real_categories_unsupported` WITH it
    (`check_dp_generate_contract` above) -- no config compiled either way,
    and neither message named the real reason. This check rejects up front
    with ONE clear code, regardless of `allow_real_categories`.

    Runs BEFORE `check_dp_generate_contract` and `check_statistical_columns`
    in `compile_plan` / `run_config_only_checks`, so a categorical-under-dp
    config only ever sees this error. `check_dp_snapshot_provenance` below
    independently re-raises the identical code for the same condition (see
    that function's docstring) so this is a self-contained verdict, not
    reliant on being called first -- a direct caller of either function
    alone gets the same outcome.

    Numeric-only scope: this closes categorical for EVERY dp-declared
    consumer, including a `dp_mode`-fit BOOL column (kind categorical,
    support_origin `"full_vocabulary"` -- bool's CANDIDATE set is still
    dtype-determined and data-independent at fit time, `quality/
    snapshot.py`, but the RELEASE mechanism it would flow through here is
    the same broken categorical branch). Option A ships numeric marginals
    only; correct categorical DP is a separate, larger follow-up.

    Config + snapshot artifact only (no profile, no source data), the same
    trust/IO scope as `check_dp_snapshot_provenance`: safe for `decoy
    validate` / `run_config_only_checks`. Deliberately silent (skip) on an
    unreadable/malformed snapshot or a source column absent from it --
    `check_statistical_columns` (row 12) owns that verdict.

    Raises:
        PlanCompileError: ``code='dp_categorical_not_yet_supported'`` when
            a referenced snapshot column's `kind` is `"categorical"`.
    """
    from decoy_engine.generation.statistical._spec import StatisticalSpecError, _load_snapshot

    dp_settings = _dp_settings(config)
    if dp_settings is None:
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
            try:
                _digest, snap = _load_snapshot(str(snapshot_file))
            except StatisticalSpecError:
                continue  # check_statistical_columns (row 12) already rejects this
            source_column = str(col_entry.get("source_column") or col_name)
            col_snap = (snap.get("columns") or {}).get(source_column)
            if not isinstance(col_snap, dict) or col_snap.get("kind") != "categorical":
                continue  # not categorical, or check_statistical_columns owns the verdict
            _raise_categorical_unsupported(
                table_name=table_name, col_name=col_name, source_column=source_column
            )


def check_dp_snapshot_provenance(config: dict[str, Any]) -> None:
    """Gate remediation Fix 3 (P1 #2): reject a dp-declared pipeline that
    consumes a snapshot which was never actually put through a DP fit.
    Also enforces Finding 4 (2026-07-21): the DECLARED (epsilon, delta)
    ceiling against what the consumed artifacts actually spent.

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
    - `kind` is NOT `"categorical"` (Option A, 2026-07-21: categorical DP
      is not yet supported at all -- see `check_dp_categorical_unsupported`
      above, whose identical rejection is duplicated here so this check
      stands correct-by-construction on its own, per the self-contained-
      verdict convention this module already follows for the anti-DP-knob
      check).
    - If the referenced column's `kind` is `numeric`, its
      `support_origin` is `"caller"` (proof it was fit with
      `dp_mode=True` + a `numeric_domains` entry -- DPS-1 -- rather than
      the real, data-dependent min/max range `apply_dp_noise` alone
      cannot retroactively fix).

    This mirrors `check_statistical_columns` (`plan/_checks.py`, row 12),
    which already loads the same snapshot file at compile time via
    `generation.statistical.load_spec`. This check now goes through the
    SAME shared, content-addressed loader (`generation.statistical._spec.
    _load_snapshot`, Finding 3) rather than its own `open()` -- the gate
    and the sampler are then provably looking at the same parsed object,
    not just the same path, closing the class of bug where a long-lived
    process's stale cache serves the sampler different bytes than this
    gate just verified. Runs config + snapshot-artifact only (no profile,
    no source data), same as `check_statistical_columns`, so it is safe
    for `decoy validate` / `run_config_only_checks`. Deliberately does NOT
    re-validate snapshot readability/schema (`check_statistical_columns`
    already owns that verdict on its own error codes); an unreadable or
    malformed snapshot here is silently skipped so this check never masks
    that one's error.

    The per-kind verdict (beyond the categorical blanket-reject) is a
    fail-closed ALLOW-LIST (default-reject): a column is accepted ONLY as
    numeric-with-`caller`; EVERY other kind (datetime, freetext, empty, any
    unknown/future kind) is rejected. `apply_dp_noise` runs on any snapshot
    -- it noises datetime year_bins and freetext length bins too -- so a
    non-`dp_mode` fit followed by `apply_dp_noise` yields a `dp` block over
    data-dependent support; a block-list of just the one eligible kind
    would let datetime/freetext fall through and PASS (a PoC-proven
    bypass). The fit-time rejection (Fix 2) only covers snapshots that WERE
    dp_mode-fit; this consume-side allow-list covers the ones that were
    not. This is a self-contained verdict: it does not rely on the ordering
    of the sibling anti-DP-knob check.

    Finding 4 (budget enforcement): for every column whose provenance is
    accepted above, this walk also collects its artifact's declared
    `(epsilon_total, delta_total)`, deduped by CONTENT HASH -- the same
    digest `_load_snapshot` already computes, so "same artifact" can never
    diverge between the cache and this accounting. Two columns pointing at
    byte-identical content are consuming ONE DP release (post-processing of
    a single release, Dwork & Roth Prop. 2.1) and are charged once; two
    distinct artifacts are separate releases and compose by BASIC
    SEQUENTIAL COMPOSITION (Dwork & Roth, *Algorithmic Foundations of DP*,
    Thm 3.16 -- epsilon and delta both sum), the conservative bound that is
    always valid without knowing whether the artifacts share underlying
    data (fail-closed: assume they do). The composed spend is compared
    against `global_settings.dp`'s DECLARED `(epsilon, delta)` ceiling
    AFTER the full walk (so every artifact is counted before judging); spend
    <= declared passes, spend > declared on either axis fails closed. Each
    artifact's OWN `epsilon_total`/`delta_total` is validated finite and in
    range first (a tampered or hand-edited artifact with a missing/NaN/
    negative `delta_total` was previously accepted with no scrutiny at all).
    The declared ceiling itself (`dp_settings["epsilon"/"delta"]`) is
    normally already `DpGenerateSettings`-shaped (`epsilon > 0`,
    `0 < delta < 1`); if it is NOT (a raw-dict caller that skipped schema
    validation: non-dict block, missing epsilon, or an out-of-range/non-
    finite value on either axis), this check FAILS CLOSED with
    `dp_budget_declaration_malformed` rather than silently disabling
    enforcement (dennis MED-1, 2026-07-21: a declared-but-unenforceable
    budget is exactly the decorative-guarantee bug Finding 4 exists to kill,
    and the prior silent skip also let a bad delta disable a well-formed
    epsilon axis).

    Trust boundary: the check reads the snapshot JSON at face value. It
    defends against honest misconfiguration (consuming a non-DP-fit
    artifact under a DP declaration, or under-declaring the spent budget),
    NOT against a forged or hand-edited snapshot -- the (epsilon, delta)
    guarantee assumes the consumed artifact is the genuine output of
    `decoy fit`. There is no signature on the snapshot today.

    Raises:
        PlanCompileError: ``code='dp_snapshot_not_dp_fit'`` when the
            referenced snapshot carries no `dp` block / `epsilon_total`;
            ``code='dp_categorical_not_yet_supported'`` when a referenced
            column's `kind` is `"categorical"`;
            ``code='dp_snapshot_numeric_support_data_dependent'`` when a
            referenced numeric column's `support_origin` is not
            `"caller"`; ``code='dp_snapshot_kind_not_dp_eligible'`` for any
            other referenced kind (datetime/freetext/empty/unknown);
            ``code='dp_snapshot_budget_malformed'`` when an accepted
            artifact's `epsilon_total`/`delta_total` is missing, non-
            numeric, non-finite, or out of range; ``code='dp_budget_
            declaration_malformed'`` when `global_settings.dp` is declared
            but its own `(epsilon, delta)` ceiling is malformed;
            ``code='dp_budget_exceeded'`` when the composed spend across all
            consumed artifacts exceeds the declared ceiling.
    """
    from decoy_engine.generation.statistical._spec import StatisticalSpecError, _load_snapshot

    dp_settings = _dp_settings(config)
    if dp_settings is None:
        return

    # Finding 4 fail-closed (dennis MED-1, 2026-07-21): global_settings.dp is
    # declared (truthy), so its (epsilon, delta) is the ceiling enforcement
    # compares against. A malformed ceiling -- non-dict block, missing epsilon,
    # or an out-of-range/non-finite value on EITHER axis -- must NOT silently
    # disable enforcement (that is the decorative-guarantee bug this whole
    # check exists to kill; the prior `enforce_budget=False` skip also let a
    # bad delta disable the well-formed epsilon axis). A config that declares
    # DP with an unenforceable budget is rejected, not passed. DpGenerateSettings
    # (epsilon>0, 0<delta<1, extra=forbid) makes this unreachable through the
    # validated product path; this is the backstop for a raw-dict caller.
    declared_epsilon = dp_settings.get("epsilon") if isinstance(dp_settings, dict) else None
    declared_delta = dp_settings.get("delta", 1e-6) if isinstance(dp_settings, dict) else None
    eps_declared_ok = (
        isinstance(declared_epsilon, (int, float))
        and not isinstance(declared_epsilon, bool)
        and math.isfinite(declared_epsilon)
        and declared_epsilon > 0
    )
    delta_declared_ok = (
        isinstance(declared_delta, (int, float))
        and not isinstance(declared_delta, bool)
        and math.isfinite(declared_delta)
        and 0.0 <= declared_delta < 1.0
    )
    if not (eps_declared_ok and delta_declared_ok):
        raise PlanCompileError(
            code="dp_budget_declaration_malformed",
            path="global_settings.dp",
            message=(
                "global_settings.dp is declared but its budget ceiling is malformed "
                f"(epsilon={declared_epsilon!r}, delta={declared_delta!r}). epsilon "
                "must be a finite number > 0 and delta a finite number in [0, 1) for "
                "the declared (epsilon, delta) ceiling to be enforceable against the "
                "consumed snapshot artifacts. Fix global_settings.dp to match "
                "DpGenerateSettings (epsilon > 0, 0 < delta < 1), or drop "
                "global_settings.dp if a non-DP release is intended."
            ),
        )
    # content-hash -> (epsilon_total, delta_total), one entry per DISTINCT
    # artifact (see docstring: dedup by content, sum by Thm 3.16).
    seen_artifacts: dict[str, tuple[float, float]] = {}

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
                digest, snap = _load_snapshot(str(snapshot_file))
            except StatisticalSpecError:
                continue  # check_statistical_columns (row 12) already rejects this

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
            # proves data-independent support. Categorical is ALWAYS rejected
            # (Option A); every other kind but numeric-with-`caller` --
            # datetime, freetext, empty, any unknown/future kind -- is
            # rejected by default too: apply_dp_noise runs on ANY snapshot
            # (it noises datetime year_bins and freetext length bins too),
            # so a non-dp_mode fit + apply_dp_noise yields a `dp` block over
            # data-dependent support. A legitimate dp_mode fit can NEVER
            # produce a datetime/freetext column (rejected at fit time, Fix
            # 2), so their presence in a dp-consumed snapshot is by
            # definition non-DP.
            if kind == "categorical":
                _raise_categorical_unsupported(
                    table_name=table_name, col_name=col_name, source_column=source_column
                )
            elif kind == "numeric" and support_origin == "caller":
                pass  # eligible; Finding 4 accounting below
            elif kind == "numeric":
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
            else:
                raise PlanCompileError(
                    code="dp_snapshot_kind_not_dp_eligible",
                    path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
                    message=(
                        f"statistical column {where}: source column {source_column!r} in "
                        f"the referenced snapshot has kind {kind!r}, which is not "
                        "DP-eligible under global_settings.dp. Only numeric columns (fit "
                        "with dp_mode + a numeric_domains entry) carry the (epsilon, "
                        "delta) marginal guarantee (Option A: categorical is not yet "
                        "supported, see dp_categorical_not_yet_supported); datetime and "
                        "freetext support is data-dependent and a legitimate dp_mode fit "
                        "rejects them outright (so their presence here means the snapshot "
                        "was not a DP fit). Mask or exclude this column, or drop "
                        "global_settings.dp if a non-DP release is intended."
                    ),
                )

            # Finding 4: this column's provenance is proven; fold its
            # artifact's declared spend into the running total (dedup by
            # content hash -- see docstring). Enforcement is unconditional once
            # DP is declared: a malformed DECLARATION already raised above, so
            # there is no "declared but unenforced" path.
            if digest not in seen_artifacts:
                # No default: `apply_dp_noise` ALWAYS writes `delta_total`
                # (dp.py, even 0.0 for a delta-free release), so an ABSENT
                # key only happens on a tampered/hand-edited/pre-DPS-2
                # artifact -- defaulting it to 0.0 here would silently
                # accept exactly the malformed case this validation exists
                # to catch.
                delta_total = dp_block.get("delta_total")
                eps_ok = (
                    isinstance(epsilon_total, (int, float))
                    and not isinstance(epsilon_total, bool)
                    and math.isfinite(epsilon_total)
                    and epsilon_total > 0
                )
                delta_ok = (
                    isinstance(delta_total, (int, float))
                    and not isinstance(delta_total, bool)
                    and math.isfinite(delta_total)
                    and delta_total >= 0
                )
                if not (eps_ok and delta_ok):
                    raise PlanCompileError(
                        code="dp_snapshot_budget_malformed",
                        path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
                        message=(
                            f"statistical column {where}: the referenced snapshot's `dp` "
                            f"block has epsilon_total={epsilon_total!r}, "
                            f"delta_total={delta_total!r}. Both must be finite numbers "
                            "(epsilon_total > 0, delta_total >= 0) to enforce "
                            "global_settings.dp's declared budget ceiling against it. "
                            "Re-fit with `decoy fit --epsilon` to produce a well-formed "
                            "`dp` block, or drop global_settings.dp."
                        ),
                    )
                # `delta_ok`/`eps_ok` already proved both are finite,
                # in-range numbers (the raise above returns otherwise);
                # this narrows the static type from the defensive
                # `Any | None` reads above (static-only hint, no runtime
                # cost -- the actual guarantee is eps_ok/delta_ok).
                seen_artifacts[digest] = (
                    float(cast(float, epsilon_total)),
                    float(cast(float, delta_total)),
                )

    if seen_artifacts:
        # `eps_declared_ok`/`delta_declared_ok` already proved both are finite,
        # in-range numbers (the malformed-declaration raise above returns
        # otherwise); these casts narrow the static type from the defensive
        # `Any | None` reads (static-only hint, no runtime cost).
        declared_epsilon = cast(float, declared_epsilon)
        declared_delta = cast(float, declared_delta)
        budget = PrivacyBudget()
        for digest, (eps, delta) in seen_artifacts.items():
            budget.charge(f"artifact:{digest[:12]}", epsilon=eps, delta=delta)
        eps_used = budget.total_epsilon()
        delta_used = budget.total_delta()
        if eps_used > declared_epsilon or delta_used > declared_delta:
            raise PlanCompileError(
                code="dp_budget_exceeded",
                path="global_settings.dp",
                message=(
                    f"global_settings.dp declares a budget ceiling of "
                    f"epsilon={declared_epsilon!r}, delta={declared_delta!r}, but the "
                    f"{len(seen_artifacts)} distinct DP snapshot artifact(s) this pipeline "
                    "consumes compose (Dwork & Roth Thm 3.16, sequential composition) to "
                    f"epsilon_total={eps_used!r}, delta_total={delta_used!r}, which exceeds "
                    "the declared ceiling. Raise global_settings.dp.epsilon/delta to cover "
                    "the artifacts' actual spend, or re-fit with a smaller epsilon/delta so "
                    "the artifacts fit inside the declared budget."
                ),
            )

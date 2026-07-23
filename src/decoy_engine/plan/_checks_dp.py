"""Plan-compile checks for the `dp` generate contract (DPS Scope B).

`global_settings.dp` is the operator's declaration that this pipeline's
`generate` output must be an honest `(epsilon, delta)`-DP marginal
release. Two generate-column knobs are deliberately ANTI-DP:
`allow_real_categories: true` and `high_cardinality: true` (HC-5) both
opt a categorical statistical column into releasing REAL observed
vocabulary rather than the OpenDP-certified label set `fit_dp_snapshot`
produces; setting either alongside `dp` would silently void the
guarantee, so both are hard-rejected at compile time. `condition_on`
(joint/conditional sampling) is out of scope for this build (guide
section 1/9.8) and is rejected the same way.

Scope B (2026-07-22 rebuild) supersedes the Option A categorical
blanket-reject this module previously carried: `fit_dp_snapshot`'s
categorical release is OpenDP-certified end to end, so a `kind:
categorical` column IS accepted here, gated on the SAME provenance
verification as numeric -- the snapshot's own `dp.categorical_columns`
declaration, checked against a reproduced `dps-marginal/v2` schema, not
a truthy `dp` key alone.

`verify_dp_snapshots` is the compile-time half of the guide's release-ID
budget model (section 4.3.5/6 row F5): distinct release IDs always
compose; the same release ID referenced by many columns is charged once;
a release ID reused with different bytes is rejected as a conflicting
artifact. It reads only already-pinned bytes (`plan._generation.
ReadSnapshot`, guide section 4.7) -- it never calls `open()` itself.
"""

from __future__ import annotations

import importlib.metadata
import math
from typing import TYPE_CHECKING, Any, NoReturn

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import DpVerification
from decoy_engine.quality.dp_ledger import ReleaseLedger

if TYPE_CHECKING:
    from decoy_engine.plan._generation import ReadSnapshot

DP_SNAPSHOT_SCHEMA_VERSION = "dps-marginal/v2"


def _dp_declared(config: dict[str, Any]) -> bool:
    """Is `global_settings.dp` PRESENT with real content (key membership
    for everything except the bare-`None` value, not truthiness)?

    A missing or non-mapping `global_settings` means DP was never
    declared -- not an error. A present `dp` key under a valid
    `global_settings` whose value is anything OTHER than `None` (`{}`, a
    list, a scalar, an incomplete mapping) means DP WAS declared and
    fails closed on the malformed shape (guide section 6 row F6).

    `dp: None` specifically is treated as NOT declared, one narrow,
    deliberate deviation from the guide's literal membership-only test:
    `decoy_engine.config._global_settings.GlobalSettings.dp` is an
    existing (pre-DPS-Scope-B) Optional pydantic field defaulting to
    `None`, and the documented production choke point
    (`PipelineConfig.model_validate(cfg).model_dump()`, `decoy_engine/
    __init__.py`) ALWAYS materializes that field -- every config that
    never touched `dp` still dumps `global_settings["dp"] = None`. A pure
    membership test misreads that serialization artifact as "the operator
    declared DP," which fails EVERY ordinary non-DP pipeline compiled
    through the documented choke point (confirmed: `PipelineConfig.
    model_validate({...}).model_dump()["global_settings"]["dp"]` is
    `None` for any config that never set `dp`). `None` carries the same
    "unset" meaning here that it carries for every other GlobalSettings
    optional field, all of which are read via `.get(...)` truthiness
    elsewhere in this compiler, so `dp: None` reads the same as `dp`
    absent. `{}`, a list, or any other non-mapping value is still a hard
    compile error -- there is no way to construct those through ordinary
    PipelineConfig validation, so no equivalent false-positive risk
    exists for them."""
    global_settings = config.get("global_settings")
    if not isinstance(global_settings, dict) or "dp" not in global_settings:
        return False
    return global_settings["dp"] is not None


def _dp_raw(config: dict[str, Any]) -> Any:
    return config["global_settings"]["dp"]


def parse_dp_ceiling(config: dict[str, Any]) -> tuple[float, float] | None:
    """The declared `(epsilon, delta)` budget ceiling, or `None` when DP
    was never declared.

    Raises:
        PlanCompileError: ``code='dp_budget_declaration_malformed'`` when
            `dp` is declared but is not a well-formed, nonempty mapping
            with `epsilon > 0` (finite) and `delta` finite in `[0, 1)`.
    """
    if not _dp_declared(config):
        return None
    dp_settings = _dp_raw(config)
    if not isinstance(dp_settings, dict) or not dp_settings:
        raise PlanCompileError(
            code="dp_budget_declaration_malformed",
            path="global_settings.dp",
            message=(
                f"global_settings.dp is declared but is not a nonempty mapping (got "
                f"{dp_settings!r}). A declared-but-unenforceable budget is rejected, not "
                "silently disabled."
            ),
        )
    epsilon = dp_settings.get("epsilon")
    delta = dp_settings.get("delta", 1e-6)
    eps_ok = (
        isinstance(epsilon, (int, float))
        and not isinstance(epsilon, bool)
        and math.isfinite(epsilon)
        and epsilon > 0
    )
    delta_ok = (
        isinstance(delta, (int, float))
        and not isinstance(delta, bool)
        and math.isfinite(delta)
        and 0.0 <= delta < 1.0
    )
    if not (eps_ok and delta_ok):
        raise PlanCompileError(
            code="dp_budget_declaration_malformed",
            path="global_settings.dp",
            message=(
                "global_settings.dp is declared but its budget ceiling is malformed "
                f"(epsilon={epsilon!r}, delta={delta!r}). epsilon must be a finite number "
                "> 0 and delta a finite number in [0, 1)."
            ),
        )
    # `eps_ok`/`delta_ok` above already proved this at runtime; restate it
    # here so mypy narrows `epsilon`/`delta` away from `Any | None` for the
    # `float()` calls (the boolean variables don't carry that narrowing
    # through the `if not (...)` branch on their own).
    assert isinstance(epsilon, (int, float))  # noqa: S101 - type-narrowing invariant
    assert isinstance(delta, (int, float))  # noqa: S101 - type-narrowing invariant
    return float(epsilon), float(delta)


def check_dp_generate_contract(config: dict[str, Any]) -> None:
    """Reject anti-DP generate-column knobs when `global_settings.dp` is
    declared (any shape -- this check does not need a well-formed
    ceiling, just presence).

    Raises:
        PlanCompileError: a `type: statistical` generate column under a
            `dp`-declared pipeline sets `allow_real_categories: true`,
            `high_cardinality: true`, or `condition_on` (joint sampling,
            out of scope -- guide section 9.8).
    """
    if not _dp_declared(config):
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
            if col_entry.get("condition_on") is not None:
                raise PlanCompileError(
                    code="dp_joint_unsupported",
                    path=f"tables.{table_name}.generate_columns.{col_name}.condition_on",
                    message=(
                        f"statistical column {where} sets condition_on under "
                        "global_settings.dp: conditional/joint sampling has no composition "
                        "accounting in this build and is out of scope (Scope B covers "
                        "single-column marginals only). Remove condition_on, or drop "
                        "global_settings.dp."
                    ),
                )
            # high_cardinality checked first (larger disclosure).
            if col_entry.get("high_cardinality") is True:
                raise PlanCompileError(
                    code="dp_generate_high_cardinality_unsupported",
                    path=f"tables.{table_name}.generate_columns.{col_name}.high_cardinality",
                    message=(
                        f"statistical column {where} sets high_cardinality: true under "
                        "global_settings.dp: this retains the FULL real vocabulary, "
                        "incompatible with a DP release. Remove high_cardinality, or drop "
                        "global_settings.dp."
                    ),
                )
            if col_entry.get("allow_real_categories") is True:
                raise PlanCompileError(
                    code="dp_generate_allow_real_categories_unsupported",
                    path=(f"tables.{table_name}.generate_columns.{col_name}.allow_real_categories"),
                    message=(
                        f"statistical column {where} sets allow_real_categories: true under "
                        "global_settings.dp: this releases the REAL observed vocabulary "
                        "instead of the OpenDP-certified label set, silently voiding the DP "
                        "guarantee. Remove allow_real_categories, or drop global_settings.dp."
                    ),
                )


def _numeric_shape_matches_a_dp_release(
    col_snap: dict[str, Any], *, lower: float, upper: float, numeric_bins: int
) -> bool:
    """BLOCKER 2 item 3 (cheap shape evidence): an exact `compute_
    distribution_snapshot` numeric column carries real `min`/`max`/`mean`/
    `std`/`quantiles` (`quality/snapshot.py:566-575`); a genuine
    `fit_dp_snapshot` numeric column NEVER does (guide section 4.2.1: `min`/
    `max` are the declared domain bounds, `mean`/`std` are always `None`,
    `quantiles` is always `{}`, and `bin_counts` always has exactly
    `numeric_bins` entries). This does NOT stop a determined forger who
    replicates that exact shape from scratch -- at that point they have
    reproduced the whole DP artifact format, not merely attached a `dp`
    block to an otherwise-ordinary snapshot -- but it does stop the
    realistic case this BLOCKER demonstrated: an ordinary EXACT snapshot
    with a fabricated `dp` block bolted on, whose numeric `stats` still
    carries the exact fit's real min/max/mean/quantiles untouched."""
    stats = col_snap.get("stats")
    if not isinstance(stats, dict):
        return False
    bin_counts = stats.get("bin_counts")
    return (
        stats.get("min") == lower
        and stats.get("max") == upper
        and stats.get("mean") is None
        and stats.get("std") is None
        and stats.get("quantiles") == {}
        and isinstance(bin_counts, list)
        and len(bin_counts) == numeric_bins
    )


def _raise(code: str, *, table_name: Any, col_name: Any, message: str) -> NoReturn:
    raise PlanCompileError(
        code=code,
        path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
        message=message,
    )


def verify_dp_snapshots(
    config: dict[str, Any],
    pinned: dict[str, ReadSnapshot],
) -> tuple[frozenset[tuple[str, str]], DpVerification | None]:
    """Verify every `type: statistical` column's DP provenance against the
    declared `global_settings.dp` ceiling, using already-pinned bytes.

    Returns:
        `(dp_verified_columns, receipt)`: the set of `(table_name,
        column_name)` pairs whose snapshot passed full verification (fed
        to `load_spec`'s `dp_verified` argument, which is what exempts a
        categorical column from the `allow_real_categories` consent gate
        -- guide section 5), and the composed `DpVerification` receipt
        (`None` when DP is not declared or no column verified).

    Raises:
        PlanCompileError: ``dp_budget_declaration_malformed`` (malformed
            ceiling); ``dp_snapshot_not_dp_fit`` (no `dps-marginal/v2` `dp`
            block); ``dp_snapshot_library_version_mismatch`` (opendp/
            dp_accounting version mismatch); ``dp_snapshot_query_count_
            mismatch`` (declared columns don't reproduce the artifact's
            own `query_count`); ``dp_snapshot_kind_not_dp_eligible`` (kind
            not in the artifact's own numeric/categorical declaration);
            ``dp_snapshot_missing_release_id``; ``dp_snapshot_budget_
            malformed`` (bad epsilon_total/delta_total);
            ``dp_release_id_conflict`` (same release_id, different
            bytes); ``dp_budget_exceeded`` (composed spend over the
            declared ceiling).
    """
    ceiling = parse_dp_ceiling(config)
    if ceiling is None:
        return frozenset(), None
    declared_epsilon, declared_delta = ceiling

    running_opendp = importlib.metadata.version("opendp")
    running_dp_accounting = importlib.metadata.version("dp-accounting")

    verified: set[tuple[str, str]] = set()
    release_id_to_digest: dict[str, str] = {}
    ledger = ReleaseLedger()

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
                continue  # check_statistical_columns owns this verdict
            read = pinned.get(str(snapshot_file))
            if read is None:
                continue  # unreadable/malformed; check_statistical_columns owns this verdict
            snap = read.parsed
            source_column = str(col_entry.get("source_column") or col_name)
            col_snap = (snap.get("columns") or {}).get(source_column)
            if not isinstance(col_snap, dict):
                continue  # check_statistical_columns owns this verdict

            dp_block = snap.get("dp")
            if (
                not isinstance(dp_block, dict)
                or dp_block.get("schema") != DP_SNAPSHOT_SCHEMA_VERSION
            ):
                _raise(
                    "dp_snapshot_not_dp_fit",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r} references a "
                        f"snapshot with no {DP_SNAPSHOT_SCHEMA_VERSION!r} `dp` block, but "
                        "global_settings.dp declares this pipeline's output must be DP. Fit it "
                        "with fit_dp_snapshot, or drop global_settings.dp."
                    ),
                )

            if (
                dp_block.get("opendp_version") != running_opendp
                or dp_block.get("dp_accounting_version") != running_dp_accounting
            ):
                _raise(
                    "dp_snapshot_library_version_mismatch",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        f"snapshot was fit with opendp={dp_block.get('opendp_version')!r}, "
                        f"dp_accounting={dp_block.get('dp_accounting_version')!r}; this "
                        f"environment runs opendp={running_opendp!r}, "
                        f"dp_accounting={running_dp_accounting!r}. A receipt this build "
                        "cannot reproduce is not a receipt it may accept."
                    ),
                )

            categorical_cols = dp_block.get("categorical_columns")
            numeric_domains = dp_block.get("numeric_domains")
            if not isinstance(categorical_cols, list) or not isinstance(numeric_domains, dict):
                _raise(
                    "dp_snapshot_not_dp_fit",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        "snapshot's `dp` block is missing categorical_columns/numeric_domains."
                    ),
                )
            expected_query_count = 1 + len(numeric_domains) + 2 * len(categorical_cols)
            if dp_block.get("query_count") != expected_query_count:
                _raise(
                    "dp_snapshot_query_count_mismatch",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        f"snapshot declares query_count={dp_block.get('query_count')!r}, but "
                        f"its own categorical_columns/numeric_domains recompute to "
                        f"{expected_query_count!r}. The artifact's declared schedule does not "
                        "match its declared columns."
                    ),
                )

            kind = col_snap.get("kind")
            eligible = (kind == "numeric" and source_column in numeric_domains) or (
                kind == "categorical" and source_column in categorical_cols
            )
            if not eligible:
                _raise(
                    "dp_snapshot_kind_not_dp_eligible",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: source "
                        f"column {source_column!r} has kind {kind!r}, which is not declared "
                        "in the snapshot's own dp.categorical_columns/dp.numeric_domains. "
                        "Only columns the fit itself declared DP-eligible are accepted."
                    ),
                )

            if kind == "numeric":
                domain = numeric_domains.get(source_column)
                numeric_bins = dp_block.get("numeric_bins")
                shape_ok = False
                if (
                    isinstance(domain, list)
                    and len(domain) == 2
                    and isinstance(domain[0], (int, float))
                    and isinstance(domain[1], (int, float))
                    and isinstance(numeric_bins, int)
                    and not isinstance(numeric_bins, bool)
                ):
                    shape_ok = _numeric_shape_matches_a_dp_release(
                        col_snap,
                        lower=float(domain[0]),
                        upper=float(domain[1]),
                        numeric_bins=numeric_bins,
                    )
                if not shape_ok:
                    _raise(
                        "dp_snapshot_numeric_shape_mismatch",
                        table_name=table_name,
                        col_name=col_name,
                        message=(
                            f"statistical column {col_name!r} in table {table_name!r}: source "
                            f"column {source_column!r} is declared numeric, but its stats block "
                            "does not have the shape a genuine fit_dp_snapshot release always "
                            "has (min/max equal to the declared domain bounds, mean/std null, "
                            "quantiles empty, and len(bin_counts) == numeric_bins). This is the "
                            "shape of an exact, non-DP snapshot with a copied or hand-written "
                            "dp block, not a DP release for this column."
                        ),
                    )

            release_id = dp_block.get("release_id")
            if not isinstance(release_id, str) or not release_id:
                _raise(
                    "dp_snapshot_missing_release_id",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        "snapshot's dp block has no release_id."
                    ),
                )

            epsilon_total = dp_block.get("epsilon_total")
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
                _raise(
                    "dp_snapshot_budget_malformed",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        f"snapshot's dp block has epsilon_total={epsilon_total!r}, "
                        f"delta_total={delta_total!r}, which must both be finite "
                        "(epsilon_total > 0, delta_total >= 0)."
                    ),
                )

            if release_id in release_id_to_digest:
                if release_id_to_digest[release_id] != read.sha256:
                    _raise(
                        "dp_release_id_conflict",
                        table_name=table_name,
                        col_name=col_name,
                        message=(
                            f"release_id {release_id!r} was already seen with a different "
                            "artifact digest. Independent releases must mint independent "
                            "release IDs; a reused ID with different bytes is a conflicting "
                            "artifact, not the same release."
                        ),
                    )
                # Same ID, same digest: already charged (guide section 6 row F5).
            else:
                # `eps_ok`/`delta_ok` above already proved this at runtime;
                # restate it so mypy narrows away from `Any | None`.
                assert isinstance(epsilon_total, (int, float))  # noqa: S101 - type-narrowing invariant
                assert isinstance(delta_total, (int, float))  # noqa: S101 - type-narrowing invariant
                release_id_to_digest[release_id] = read.sha256
                ledger.charge(
                    f"release:{release_id[:12]}",
                    epsilon=float(epsilon_total),
                    delta=float(delta_total),
                )

            verified.add((table_name, col_name))

    if release_id_to_digest:
        eps_used = ledger.total_epsilon()
        delta_used = ledger.total_delta()
        if eps_used > declared_epsilon or delta_used > declared_delta:
            raise PlanCompileError(
                code="dp_budget_exceeded",
                path="global_settings.dp",
                message=(
                    f"global_settings.dp declares a budget ceiling of "
                    f"epsilon={declared_epsilon!r}, delta={declared_delta!r}, but the "
                    f"{len(release_id_to_digest)} distinct DP release(s) this pipeline "
                    f"consumes compose to epsilon_total={eps_used!r}, "
                    f"delta_total={delta_used!r}, which exceeds the declared ceiling."
                ),
            )
        receipt = DpVerification(
            scope="single-column-marginals",
            release_ids=tuple(sorted(release_id_to_digest)),
            epsilon_total=eps_used,
            delta_total=delta_used,
        )
        return frozenset(verified), receipt

    return frozenset(verified), None

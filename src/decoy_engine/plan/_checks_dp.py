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
declaration, checked against a reproduced `dps-marginal/v3` schema, not
a truthy `dp` key alone.

`verify_dp_snapshots` is the compile-time half of the guide's release-ID
budget model (section 4.3.5/6 row F5): distinct release IDs always
compose; the same release ID referenced by many columns is charged once;
a release ID reused with different bytes is rejected as a conflicting
artifact. It reads only already-pinned bytes (`plan._generation.
ReadSnapshot`, guide section 4.7) -- it never calls `open()` itself.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, NoReturn

from decoy_engine.plan._checks_dp_carrier import (
    carrier_aware_query_count,
    check_flag_tokens_canonical,
    column_block_matches_schema,
    numeric_shape_matches_a_dp_release,
    schema_matches_legacy,
)
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import DpVerification
from decoy_engine.quality.dp_ledger import ReleaseLedger
from decoy_engine.quality.dp_provenance import ProvenanceError, validate_recorded_provenance
from decoy_engine.quality.snapshot import DP_SNAPSHOT_SCHEMA_VERSION

if TYPE_CHECKING:
    from decoy_engine.plan._generation import ReadSnapshot


def _dp_declared(config: dict[str, Any]) -> bool:
    """Is `global_settings.dp` PRESENT (key membership, not truthiness)?

    A missing or non-mapping `global_settings`, or a `global_settings`
    with no `dp` key at all, means DP was never declared -- not an error.
    A present `dp` key means DP WAS declared and fails closed on any
    malformed shape, including a bare `None` (guide section 6 row F6):
    `parse_dp_ceiling` rejects `dp: null` with `dp_budget_declaration_
    malformed`, the same as `{}`, a list, or an incomplete mapping.

    C-B3 (Codex round-3 blocker, fixed at the root): this used to carve
    out a bare `None` value as "unset", because the documented production
    choke point (`PipelineConfig.model_validate(cfg).model_dump()`)
    materialized `global_settings["dp"] = None` for BOTH an unset field
    and an explicit `dp: null` -- collapsing them made the check
    indistinguishable from a real fail-open: an operator writing `dp:
    null` got the identical (non-DP) treatment as a pipeline that never
    touched `dp`, silently bypassing every DP gate. The fix lives at the
    source of the ambiguity, not here: `config.PipelineConfig.model_dump`
    now omits the `dp` key entirely when `GlobalSettings.model_fields_set`
    shows it was never assigned, and leaves it present (including a bare
    `None`) when it was explicitly written. With that distinction restored
    upstream, this function is a pure membership test again -- exactly
    the guide's original text -- and needs no `None` special case."""
    global_settings = config.get("global_settings")
    return isinstance(global_settings, dict) and "dp" in global_settings


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
            ceiling); ``dp_snapshot_not_dp_fit`` (no `dps-marginal/v3` `dp`
            block); ``dp_snapshot_provenance_uncertified`` (the recorded
            proof-stack identity is not a certified row); ``dp_snapshot_
            query_count_mismatch`` (declared columns/carriers don't
            reproduce the artifact's own `query_count`); ``dp_snapshot_
            column_schema_mismatch`` (column_schema names/carriers agree in
            aggregate count but not per column with the legacy
            categorical_columns/numeric_domains declaration); ``dp_snapshot_
            carrier_invalid`` (a column's recorded kind x carrier pair is
            not allowed); ``dp_snapshot_kind_not_dp_eligible`` (kind not in
            the artifact's own numeric/categorical declaration);
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

            # Provenance gate (guide section 3.8, replacing the old
            # compare-to-local-env at this line). Generation validates the
            # artifact's RECORDED proof-stack identity (platform triple, full
            # CPython version, lock fingerprint) against the static certified
            # set -- it does NOT recompute a fingerprint from its own installed
            # libraries, because the generating host may legitimately differ
            # from the fitting host. The human-readable `opendp_version` /
            # `dp_accounting_version` stay in the artifact as INFORMATIONAL
            # annotation, but the GATE is the recorded identity's membership in
            # the certified set, not a local-version equality (which both
            # over-matched generation-irrelevant libs AND, if merely deleted,
            # would stop validating the proof stack entirely).
            try:
                validate_recorded_provenance(dp_block.get("provenance"))
            except ProvenanceError as exc:
                _raise(
                    "dp_snapshot_provenance_uncertified",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        f"snapshot's recorded proof stack is not a certified row [{exc.code}] "
                        f"{exc.message}. A receipt fit on an uncertified platform/stack is not "
                        "one this build may accept."
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
            # Carrier-aware reconstruction (guide sections 3.4/3.9): a flag
            # column's bool-domain grouped+total pair reconstructs the SAME
            # categorical pair count as the text path. The v3 artifact records a
            # per-column `column_schema`; reconstruct from it when present (which
            # also validates every recorded carrier against the closed set), and
            # require it to agree with the categorical_columns/numeric_domains
            # reconstruction so the two declarations cannot disagree.
            expected_query_count = 1 + len(numeric_domains) + 2 * len(categorical_cols)
            carrier_aware = carrier_aware_query_count(dp_block)
            if carrier_aware is None or carrier_aware != expected_query_count:
                _raise(
                    "dp_snapshot_query_count_mismatch",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        "snapshot's column_schema carriers "
                        f"(reconstructing to {carrier_aware!r}) do not agree with its "
                        f"categorical_columns/numeric_domains ({expected_query_count!r}), or a "
                        "recorded carrier is not one of ('number', 'flag', 'text'). The "
                        "artifact's declared schedule does not match its declared columns."
                    ),
                )
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

            # L-1: the reconstructed COUNT agreeing is necessary but not
            # sufficient -- a name-disjoint or carrier/kind-transposed
            # column_schema can reconstruct to the same count while describing
            # different columns (e.g. one numeric + one categorical whose
            # carriers are swapped between the two names). Pin per-column
            # identity too: the column_schema's names must be exactly the union
            # of the legacy categorical_columns/numeric_domains declaration, and
            # each entry's carrier must match the kind THAT declaration assigns
            # the column (number <-> numeric_domains, text/flag <->
            # categorical_columns). `carrier_aware_query_count` already proved
            # column_schema is a dict of dicts with known carriers (else
            # carrier_aware is None and we raised above).
            column_schema = dp_block["column_schema"]
            schema_consistent = schema_matches_legacy(
                column_schema, categorical_cols, numeric_domains
            )
            if not schema_consistent:
                _raise(
                    "dp_snapshot_column_schema_mismatch",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: the "
                        "snapshot's column_schema names/carriers do not match its own "
                        "categorical_columns/numeric_domains per column (schema names "
                        f"{sorted(column_schema)!r} vs declared "
                        f"{sorted(set(categorical_cols) | set(numeric_domains))!r}). A "
                        "column_schema that only agrees in aggregate count but disagrees "
                        "per column is a corrupted or hand-edited artifact, not a genuine "
                        "release."
                    ),
                )

            # Validate the verified column's own recorded carrier (guide section
            # 3.9): a numeric column must carry `number`, a categorical column
            # `text` or `flag`; any other value fails closed.
            col_carrier = col_snap.get("carrier")
            col_kind = col_snap.get("kind")
            carrier_ok = (col_kind == "numeric" and col_carrier == "number") or (
                col_kind == "categorical" and col_carrier in ("text", "flag")
            )
            if not carrier_ok:
                _raise(
                    "dp_snapshot_carrier_invalid",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: source "
                        f"column {source_column!r} records carrier {col_carrier!r} for kind "
                        f"{col_kind!r}, which is not an allowed kind x carrier pair (numeric->"
                        "number, categorical->text/flag)."
                    ),
                )

            # HIGH-2: the `columns` block's carrier/kind (which `load_spec`
            # reads onto `StatisticalSpec.carrier`, and the sampler dispatches
            # on) must equal the `dp.column_schema` entry for the same column.
            # A `flag` release whose `columns` block is relabelled `text`
            # would otherwise pass here while `dp.column_schema` still
            # declares `flag`, reaching the sampler with the wrong carrier.
            if not column_block_matches_schema(col_snap, column_schema, source_column):
                _raise(
                    "dp_snapshot_column_block_schema_mismatch",
                    table_name=table_name,
                    col_name=col_name,
                    message=(
                        f"statistical column {col_name!r} in table {table_name!r}: source "
                        f"column {source_column!r} records carrier {col_carrier!r}/kind "
                        f"{col_kind!r} in its columns block, which disagrees with the dp "
                        f"block's column_schema entry {column_schema.get(source_column)!r}. "
                        "The two recorded declarations for one column must be identical; a "
                        "divergence is a corrupted or hand-edited artifact."
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
                    shape_ok = numeric_shape_matches_a_dp_release(
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

            # Guide 3.4 shape guard: a flag column's top_values must be only
            # the two canonical tokens the fit emits, so a bogus token fails
            # closed here instead of deep in the sampler's decode.
            check_flag_tokens_canonical(
                col_snap,
                kind=kind,
                col_carrier=col_carrier,
                table_name=table_name,
                col_name=col_name,
                source_column=source_column,
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
            # Zero-epsilon accepted (guide section 4, zero-epsilon finding): the
            # ledger already sums composed totals with `epsilon_total >= 0`
            # (`dp_accounting` legitimately composes to exactly 0 at a large
            # enough delta, e.g. eps=1/delta=0.9), and this reads a COMPOSED
            # total, not a requested ceiling. A negative total is still
            # impossible and fails closed; requested ceilings stay strictly
            # positive at parse_dp_ceiling.
            eps_ok = (
                isinstance(epsilon_total, (int, float))
                and not isinstance(epsilon_total, bool)
                and math.isfinite(epsilon_total)
                and epsilon_total >= 0
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
                        "(epsilon_total >= 0, delta_total >= 0)."
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

"""leak_check: post-mask residual-value verification (Sprint 2 honesty pack,
S2 / p5-j-validators-extended, the load-bearing slice).

Established methodology (CLAUDE.md core rule): commercial masking tools
verify masking by comparing pre/post values per column and reporting the
percentage unchanged against a threshold (Delphix and Informatica "masking
verification" reports; Microsoft Presidio's evaluator counts residual
original values). The column-pair comparison itself is the Great
Expectations `expect_column_pair_values_A_to_not_equal_B` pattern. No tool
asserts "every cell must differ": legitimate masking produces occasional
coincidences (Faker can emit the source value; FPE is a permutation and
permutations have fixed points, non-trivially likely on small domains;
bucketize's lower/range/midpoint format returns the source value for cells
already on a bucket boundary; date_shift with `min_days <= 0 <= max_days` can
legitimately shift by zero). A naive per-cell equality assert WILL
false-positive on legitimately-masked data (trap T1); this module uses a
threshold-per-column design instead.

Two tiers (D3):

1. Column tier (all non-excluded columns): `identical_ratio == 1.0` over
   non-null compared cells (with `min_rows` non-null cells present) means the
   declared strategy had NO effect on the column at all -- the
   forgot-to-mask safety net.
2. Cell tier (TRANSFORMATIVE columns only): `identical_ratio >
   max_identical_ratio` flags residual per-row leaks; `failing_row_indices`
   make the finding quarantinable row-by-row under the existing
   `validation_fail` trigger.

Strategy classification is the false-positive control (trap T1). The three
frozensets below are exhaustive over `SCALAR_HANDLERS`
(`execution/_strategies/__init__.py`); `tests/unit/validators/
test_leak_check.py::TestDriftSentry` is the ratchet that fails the build if a
new strategy is added without a conscious classification decision.

GATE decision beyond the locked defaults (flagged for Cam, not pre-decided by
the guide): composite bundle output columns (identified by `provider` being
one of the known composite-generator names, mirroring the explicit dispatch
in `execution/_strategies/_composite.py`) are classified TRANSFORMATIVE. Every
composite bundle regenerates a fresh coherent value per row (name/address/
provider-identity bundles), the same semantic family as `faker`; there is no
narrower per-member-column classification exposed by the composite generators
today, and treating them as TRANSFORMATIVE (checked at both tiers, not
excluded) is the conservative, honesty-preserving choice.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.validators._types import ValidatorFinding

# ---------------------------------------------------------------------------
# Strategy classification (D3). Exhaustive over SCALAR_HANDLERS; the drift
# sentry test asserts this.
# ---------------------------------------------------------------------------

TRANSFORMATIVE: frozenset[str] = frozenset(
    {"hash", "fpe", "redact", "text_redact", "faker", "code_set"}
)

SHAPE_PRESERVING: frozenset[str] = frozenset(
    {
        "bucketize",
        "bucket_perturb",
        "categorical",
        "date_shift",
        "derived",
        "derived_aggregate",
        "formula",
        "geo_generalize",
        "group_key",
        "grouped_series",
        "joint_mask",
        "nested",
        "shuffle",
        "text_mask",
        "top_code",
        "truncate",
        "windowed_date",
    }
)

EXCLUDED: frozenset[str] = frozenset({"passthrough"})

# Composite bundle generators (execution/_strategies/_composite.py's explicit
# provider dispatch, mirrored here rather than re-derived). A column whose
# `provider` is one of these is a composite-bundle member; see the GATE note
# in the module docstring for the TRANSFORMATIVE classification rationale.
_COMPOSITE_PROVIDERS: frozenset[str] = frozenset(
    {
        "composite_name_email",
        "composite_city_state_zip",
        "composite_person",
        "composite_address",
        "composite_provider",
        "composite_custom",
    }
)


def _is_identical(src: Any, out: Any) -> bool:
    """Canonical cell-identity compare (D3). Caller guarantees `src is not None`."""
    if out is None:
        return False  # value -> null is redaction, not a leak
    if str(src) == str(out):
        return True
    try:
        return float(src) == float(out)
    except (TypeError, ValueError):
        return False


def _classify_table_kinds(config: dict[str, Any]) -> dict[str, str]:
    """Mirror `execution._pipeline.classify_table_kinds` without importing
    the execution package (validators sit below execution in the layering;
    importing it here would invert the dependency direction)."""
    out: dict[str, str] = {}
    for table in config.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("name")
        if not isinstance(name, str):
            continue
        out[name] = "generate" if table.get("generate_columns") else "mask"
    return out


def _fk_child_columns(config: dict[str, Any]) -> set[tuple[str, str]]:
    """(table, column) pairs that are FK children: values come from the
    parent's masked key map, not the declared strategy. A leaking parent is
    caught on the parent column instead."""
    out: set[tuple[str, str]] = set()
    for rel in config.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        for child in rel.get("children") or []:
            if not isinstance(child, dict):
                continue
            table_name = child.get("table")
            if not isinstance(table_name, str):
                continue
            for col in child.get("columns") or []:
                if isinstance(col, str):
                    out.add((table_name, col))
    return out


def _column_entries(config: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(table, column) -> column config entry, for every mask-kind table."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for table in config.get("tables") or []:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str):
            continue
        table_name = table["name"]
        for col in table.get("columns") or []:
            if isinstance(col, dict) and isinstance(col.get("name"), str):
                out[(table_name, col["name"])] = col
    return out


def validate_leak_check(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
    *,
    sources: dict[str, pa.Table] | None = None,
) -> tuple[ValidatorFinding, ...]:
    """leak_check: flag residual source values surviving into masked output.

    Default scope (no `columns:` on the entry): every non-excluded column of
    every mask-kind table. Exclusions (D3): `passthrough` strategy, FK-child
    columns, and any column carrying a `when:` predicate (rows not matching
    the predicate are legitimately identical by construction; there is no
    way to distinguish them post-hoc). `params.exempt` is the documented
    operator escape hatch on top of these.

    Missing-source rule (D2, fail-loud): every mask-kind table in scope MUST
    have a corresponding entry in `sources`; a leak check that silently
    didn't run is worse than none. An EXPLICIT `columns:` entry naming a
    generate-kind table is a config error (no source can ever exist for it),
    raised rather than skipped.

    Args:
        outputs: Read-only pipeline outputs (post-mask).
        entry: Config block; optional `columns` (explicit scope) and
            `params` (`exempt`, `max_identical_ratio` default 0.02,
            `min_rows` default 1).
        config: Full pipeline config dict (`tables`, `relationships`).
        sources: Read-only pre-mask source tables (D2). Required for every
            table in scope.

    Returns:
        Tuple of ValidatorFinding (column-tier and/or cell-tier per column).

    Raises:
        ValueError: A table in scope has no source table, or an explicit
            `columns:` entry names a generate-kind table.
    """
    params: dict[str, Any] = entry.get("params") or {}
    max_identical_ratio = float(params.get("max_identical_ratio", 0.02))
    min_rows = int(params.get("min_rows", 1))
    exempt: dict[str, list[str]] = params.get("exempt") or {}
    exempt_set: set[tuple[str, str]] = {
        (table, col) for table, cols in exempt.items() for col in cols
    }

    table_kinds = _classify_table_kinds(config)
    fk_children = _fk_child_columns(config)
    col_entries = _column_entries(config)
    sources = sources or {}

    explicit_columns: dict[str, list[str]] = entry.get("columns") or {}

    if explicit_columns:
        scope: dict[str, list[str]] = {}
        for table_name, cols in explicit_columns.items():
            if table_kinds.get(table_name) == "generate":
                raise ValueError(
                    f"leak_check: columns: explicitly names {table_name!r}, which is a "
                    "generate-kind table (no source column exists to compare against). "
                    "Remove it from leak_check's columns: scope."
                )
            scope[table_name] = list(cols)
    else:
        scope = {
            table_name: [c["name"] for c in table.get("columns") or [] if isinstance(c, dict)]
            for table in (config.get("tables") or [])
            if isinstance(table, dict)
            and isinstance(table.get("name"), str)
            and (table_name := table["name"]) in table_kinds
            and table_kinds[table_name] == "mask"
        }

    findings: list[ValidatorFinding] = []

    for table_name, col_names in scope.items():
        if table_name not in outputs:
            continue  # table not produced this run; nothing to check
        if table_name not in sources:
            raise ValueError(
                f"leak_check configured for table {table_name!r} but no source table was "
                "provided; leak verification cannot run. Pass the source table via "
                "run_pipeline(sources=...) or exclude this table from leak_check's scope."
            )
        out_table = outputs[table_name]
        src_table = sources[table_name]

        for col_name in col_names:
            if col_name not in out_table.schema.names or col_name not in src_table.schema.names:
                continue
            if (table_name, col_name) in exempt_set:
                continue
            if (table_name, col_name) in fk_children:
                continue

            col_entry = col_entries.get((table_name, col_name), {})
            strategy = col_entry.get("strategy")
            provider = col_entry.get("provider")
            when = col_entry.get("when")
            if isinstance(when, str) and when.strip():
                continue
            if strategy in EXCLUDED:
                continue

            is_transformative = strategy in TRANSFORMATIVE or provider in _COMPOSITE_PROVIDERS
            is_shape_preserving = strategy in SHAPE_PRESERVING
            if not is_transformative and not is_shape_preserving:
                # LOW L1 (dennis review 2026-07-04): fail CLOSED, not open.
                # An unclassified strategy reaching here means the drift
                # sentry (test_leak_check.py) was bypassed and this is a
                # privacy product: silently skipping the column would
                # manufacture false confidence that it was leak-checked. The
                # sentry guarantees every strategy is classified, so this can
                # only fire on a genuine gap -- raise so it is fixed, never
                # shipped unchecked.
                raise ValueError(
                    f"leak_check: strategy {strategy!r} on {table_name}.{col_name} is not "
                    "classified as TRANSFORMATIVE, SHAPE_PRESERVING, or EXCLUDED. leak_check "
                    "cannot verify a strategy it does not know how to interpret; classify it "
                    "in decoy_engine.validators._leak_check (and its drift sentry) before use."
                )

            src_vals = src_table.column(col_name).to_pylist()
            out_vals = out_table.column(col_name).to_pylist()
            compared = 0
            identical: list[int] = []
            for i, (src_val, out_val) in enumerate(zip(src_vals, out_vals, strict=True)):
                if src_val is None:
                    continue  # null source: not a leak either way
                compared += 1
                if _is_identical(src_val, out_val):
                    identical.append(i)

            if compared < min_rows or compared == 0:
                continue
            ratio = len(identical) / compared

            if ratio == 1.0:
                findings.append(
                    ValidatorFinding(
                        validator="leak_check",
                        table=table_name,
                        column=col_name,
                        failing_row_indices=tuple(identical),
                        detail=(
                            f"{len(identical)} of {compared} non-null cells identical to "
                            f"source (ratio {ratio:.2f}); strategy {strategy!r} produced no "
                            "change"
                        ),
                    )
                )

            if is_transformative and ratio > max_identical_ratio:
                findings.append(
                    ValidatorFinding(
                        validator="leak_check",
                        table=table_name,
                        column=col_name,
                        failing_row_indices=tuple(identical),
                        detail=(
                            f"{len(identical)} of {compared} non-null cells identical to "
                            f"source (ratio {ratio:.2f} > max_identical_ratio "
                            f"{max_identical_ratio:.2f}) under strategy {strategy!r}"
                        ),
                    )
                )

    return tuple(findings)

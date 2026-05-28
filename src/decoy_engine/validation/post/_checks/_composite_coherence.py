"""composite_coherence scan (engine-v2 S10): composite outputs stay coherent post-mask.

Composite-backed columns are detected via `CapabilityMatrix.backend_type ==
"composite"` (the literal the S8 adapters declare). For each composite generator
the scan audits the row-level coherence contract over the masked output:

- `composite_name_email`: the `email` local-part equals `first_name.last_name`
  (lowercased) for every non-null row.
- `composite_city_state_zip`: the `(city, state, zip)` triple is a verbatim row of
  the locality table.

Any incoherent row is a hard job failure (composite contract violation). One
`CompositeCoherenceReport` per (table, generator). Closes the post-mask half of
the Session 33 JC3 / Session 34 M2 deferral at the validator layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.generation.composite import load_locality_table
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome, column_values
from decoy_engine.validation.post._types import CompositeCoherenceReport

if TYPE_CHECKING:
    import pyarrow as pa

_NAME = "composite_coherence"
_NAME_EMAIL = "composite_name_email"
_CITY_STATE_ZIP = "composite_city_state_zip"


def run_composite_coherence(ctx: ScanContext) -> ScanOutcome:
    reports: dict[str, CompositeCoherenceReport] = {}
    failed = False
    groups: dict[tuple[str, str], set[str]] = {}
    for table_name, table_seed in ctx.plan.seed_envelope.per_table:
        for col_name, seed in table_seed.per_column:
            if not ctx.registry.has(seed.provider):
                continue
            if ctx.registry.get_capabilities(seed.provider).backend_type == "composite":
                groups.setdefault((table_name, seed.provider), set()).add(col_name)

    for (table_name, provider), _cols in sorted(groups.items()):
        out_table = ctx.outputs.get(table_name)
        if out_table is None:
            continue
        report = _audit(provider, out_table)
        if report is None:
            continue
        reports[f"{table_name}:{provider}"] = report
        if report.incoherent_rows > 0:
            failed = True

    return ScanOutcome(name=_NAME, failed=failed, composite_coherence=reports)


def _audit(provider: str, out_table: pa.Table) -> CompositeCoherenceReport | None:
    if provider == _NAME_EMAIL:
        return _audit_name_email(out_table)
    if provider == _CITY_STATE_ZIP:
        return _audit_city_state_zip(out_table)
    return None  # unknown composite: no coherence contract to audit here


def _audit_name_email(out_table: pa.Table) -> CompositeCoherenceReport:
    cols = ("first_name", "last_name", "email")
    firsts, lasts, emails = (column_values(out_table, c) for c in cols)
    total = 0
    coherent = 0
    for first, last, email in zip(firsts, lasts, emails, strict=False):
        if first is None or last is None or email is None:
            continue  # null row preserved; not a coherence violation
        total += 1
        local = str(email).split("@", 1)[0]
        if local == f"{first}.{last}".lower():
            coherent += 1
    return CompositeCoherenceReport(
        generator=_NAME_EMAIL,
        columns=cols,
        total_rows=total,
        coherent_rows=coherent,
        incoherent_rows=total - coherent,
    )


def _audit_city_state_zip(out_table: pa.Table) -> CompositeCoherenceReport:
    cols = ("city", "state", "zip")
    cities, states, zips = (column_values(out_table, c) for c in cols)
    table = set(load_locality_table())
    total = 0
    coherent = 0
    for city, state, zip_code in zip(cities, states, zips, strict=False):
        if city is None or state is None or zip_code is None:
            continue
        total += 1
        if (city, state, zip_code) in table:
            coherent += 1
    return CompositeCoherenceReport(
        generator=_CITY_STATE_ZIP,
        columns=cols,
        total_rows=total,
        coherent_rows=coherent,
        incoherent_rows=total - coherent,
    )

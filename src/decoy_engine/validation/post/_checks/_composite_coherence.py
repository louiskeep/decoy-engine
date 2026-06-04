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
# MG-4 (2026-05-31) additions.
_PERSON = "composite_person"
_ADDRESS = "composite_address"
_PROVIDER = "composite_provider"


def run_composite_coherence(ctx: ScanContext) -> ScanOutcome:
    reports: dict[str, CompositeCoherenceReport] = {}
    failed = False
    groups: dict[tuple[str, str], set[str]] = {}
    for table_name, table_seed in ctx.plan.seed_envelope.per_table:
        for col_name, seed in table_seed.per_column:
            provider = seed.provider
            if provider is None or not ctx.registry.has(provider):
                continue
            if ctx.registry.get_capabilities(provider).backend_type == "composite":
                groups.setdefault((table_name, provider), set()).add(col_name)

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
    # MG-4 (2026-05-31): per-composite audits for the 3 new fixed-output
    # composites. composite_custom is variable-length + its coherence
    # contract is identity-stability (not statistical), so it has no
    # post-mask audit -- generation-time guarantees stand on their own.
    if provider == _PERSON:
        return _audit_person(out_table)
    if provider == _ADDRESS:
        return _audit_address(out_table)
    if provider == _PROVIDER:
        return _audit_provider(out_table)
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


def _audit_person(out_table: pa.Table) -> CompositeCoherenceReport:
    """MG-4 (2026-05-31): composite_person coherence audit.

    Same email-format check as composite_name_email (email local-part
    equals "{first}.{last}" lowercased). dob has no coherence contract
    relative to the name fields (it is an independent pool draw), so
    it is not audited here -- the generation-time identity stability
    is the contract.
    """
    cols = ("first_name", "last_name", "email", "dob")
    firsts, lasts, emails, _dobs = (column_values(out_table, c) for c in cols)
    total = 0
    coherent = 0
    for first, last, email in zip(firsts, lasts, emails, strict=False):
        if first is None or last is None or email is None:
            continue
        total += 1
        local = str(email).split("@", 1)[0]
        if local == f"{first}.{last}".lower():
            coherent += 1
    return CompositeCoherenceReport(
        generator=_PERSON,
        columns=cols,
        total_rows=total,
        coherent_rows=coherent,
        incoherent_rows=total - coherent,
    )


def _audit_address(out_table: pa.Table) -> CompositeCoherenceReport:
    """MG-4 (2026-05-31): composite_address coherence audit.

    The (city, state, zip) triple must be a verbatim locality-table row.
    street_address is independent (no coherence contract) and is not
    audited.
    """
    cols = ("street_address", "city", "state", "zip")
    _streets, cities, states, zips = (column_values(out_table, c) for c in cols)
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
        generator=_ADDRESS,
        columns=cols,
        total_rows=total,
        coherent_rows=coherent,
        incoherent_rows=total - coherent,
    )


def _audit_provider(out_table: pa.Table) -> CompositeCoherenceReport:
    """MG-4 (2026-05-31): composite_provider coherence audit.

    NPI must pass the CMS Luhn validator. provider_name is independent.
    practice_address is a flat string; checking its city/state/zip
    components against the locality table is a tighter contract than
    composite_address can offer, but we skip it here to keep the audit
    simple -- the generation-time path emits "<city>, <state> <zip>"
    by construction, so the only way it goes out of band is a future
    change to the joining logic; that change is a regression and the
    composite_provider unit cells catch it.
    """
    from decoy_engine.storm.detectors import _npi_valid

    cols = ("npi", "provider_name", "practice_address")
    npis, _names, _addresses = (column_values(out_table, c) for c in cols)
    total = 0
    coherent = 0
    for npi in npis:
        if npi is None:
            continue
        total += 1
        if _npi_valid(str(npi)):
            coherent += 1
    return CompositeCoherenceReport(
        generator=_PROVIDER,
        columns=cols,
        total_rows=total,
        coherent_rows=coherent,
        incoherent_rows=total - coherent,
    )

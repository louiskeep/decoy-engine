"""format_rules scan (engine-v2 S10): masked output matches the provider format.

For every masked column whose provider declares a `CapabilityMatrix.format_regex`
(the regulated identifiers: synthetic_ssn/ein/npi/ndc/mrn; faker/mimesis declare
None and are skipped), 100% of non-null outputs must fullmatch the regex. A
violation is a HARD job failure for a regulated identifier, a WARN otherwise.

"Regulated" is read from the data, not a hardcoded name list: a provider with a
non-empty `blocklist_validators` tuple is a regulated identifier (SSN/EIN/NPI/NDC/MRN
carry one; faker/mimesis carry `()`). Per Dennis's S10 slice-1-2 review ruling (c),
the blocklist CHECK itself is DEFERRED -- there is no name->validator resolver in
providers_v2, and reaching the S6 domain privates is out of scope. We use the
presence of `blocklist_validators` only as the regulated/severity marker; the
`format_regex` check is the shipped guard (and the generation-time derive already
guards blocklist exhaustion).
"""

from __future__ import annotations

import re

from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome, column_values

_NAME = "format_rules"


def run_format_rules(ctx: ScanContext) -> ScanOutcome:
    failed = False
    warnings: list[QualityWarning] = []
    for table_name, table_seed in ctx.plan.seed_envelope.per_table:
        out_table = ctx.outputs.get(table_name)
        if out_table is None:
            continue
        for col_name, seed in table_seed.per_column:
            if col_name not in out_table.column_names or not ctx.registry.has(seed.provider):
                continue
            caps = ctx.registry.get_capabilities(seed.provider)
            if not caps.format_regex:
                continue
            pattern = re.compile(caps.format_regex)
            violations = sum(
                1
                for v in column_values(out_table, col_name)
                if v is not None and pattern.fullmatch(str(v)) is None
            )
            if violations == 0:
                continue
            regulated = bool(caps.blocklist_validators)
            if regulated:
                failed = True
            warnings.append(
                QualityWarning(
                    code="format_rule_violation",
                    provider=seed.provider,
                    column=col_name,
                    detail={
                        "table": table_name,
                        "violations": violations,
                        "regulated": regulated,
                    },
                )
            )
    return ScanOutcome(name=_NAME, failed=failed, warnings=tuple(warnings))

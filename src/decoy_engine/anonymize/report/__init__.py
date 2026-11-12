"""The risk and utility report of a release chosen by the enforcement search.

Pattern: ARX sample risk summary (El Emam's prosecutor, journalist and marketer risk) documented per the HHS OCR Expert Determination guidance.
  See ``_build`` for the method and its sources, and
  ``decoy_engine.quality.anonymity`` (``measure_reidentification_risk``) for
  the formulas.

``build_risk_utility_report(released, lattice, result, *, risk_threshold,
population=None)`` returns a frozen ``RiskUtilityReport``; ``to_payload()``
gives the ``risk-utility-report/v1`` JSON document and
``RiskUtilityReportRead`` reads one back (another major, a missing tag, free
text in a token field and wrong-typed numbers are refused; added fields and
tokens of a later minor are read).

What the document states:

- the method, the quasi-identifiers, the lattice heights and the chosen node;
- each privacy model with its thresholds, all met by the released rows;
- the search's loss figures and suppression counts, copied from the result;
- prosecutor risk of the released rows (highest, average, records at risk
  above ``risk_threshold``);
- journalist and marketer risk only against a supplied population (a table
  the release was drawn from); without one they are ``null`` and the document
  says ``population: "not_supplied"``;
- fixed tokens for the assumptions made and what the analysis does not
  establish (an expert determination among them).

What it does not cover: the suppressed rows, attackers who know columns not
named as quasi-identifiers, linkage with outside data, and whether a risk is
"very small" (an expert's judgement; see ``docs/what-we-cannot-prove.md``).

The document holds no cell value. Nothing in config, the planner or a run
reaches this package; callers import it. Pre-GA and unstable as a Python API;
the document format follows the compatibility contract.
"""

from __future__ import annotations

from decoy_engine.anonymize.report._build import build_risk_utility_report
from decoy_engine.anonymize.report._model import (
    RISK_UTILITY_REPORT_SCHEMA,
    ModelKind,
    RiskUtilityReport,
    RiskUtilityReportRead,
)

__all__ = [
    "RISK_UTILITY_REPORT_SCHEMA",
    "ModelKind",
    "RiskUtilityReport",
    "RiskUtilityReportRead",
    "build_risk_utility_report",
]
